"""Bramka portfelowa (R3, spec wielu pasm §4): wspólne limity wszystkich pasm PONAD bramką pasma (`risk.RiskGate`).

Bramka (`PortfolioGate`) ocenia wyłącznie WEJŚCIA; wyjść nie blokuje nigdy (`check_exit` zawsze przechodzi, nawet przy
zepsutym dzienniku). Jest tylko do odczytu (nic nie zapisuje) i nigdy nie rzuca: `Verdict.reasons` zawiera WSZYSTKIE
niespełnione kontrole w kolejności `REASON_ORDER` (tabela spec §4.2), bez duplikatów. Wołana PO bramce pasma; werdykt
wejścia to suma powodów obu bramek (podłączenie: zadanie 12). Kill switch zostaje w bramce pasma (`RiskGate`, KILL).

Zasada nadrzędna (jak w `risk.py`): kontrola, której nie da się ocenić (błąd odczytu dziennika, brak kursu, nieczytelny
kontekst wejścia, brak konta, zepsuty zegar), BLOKUJE swoim powodem. Gdy zawiedzie sama bramka (nieoczekiwany wyjątek
poza pojedynczą kontrolą), wejście blokuje jedyny powód PORTFOLIO_GATE_ERROR, a wyjątek trafia do logu.

Kwoty są w walucie rachunku (EUR). Kwoty w walucie instrumentu innej niż EUR przelicza wstrzyknięty `FxView`
(spec §7.3: kurs od brokera, po stronie niekorzystnej); brak kursu blokuje. Iloczyny i sumy są dokładne (pułapka
Inexact): zaokrąglenie nie może zamienić „o grosz więcej” w „równo”.

Zajętość (podstawa PORTFOLIO_MAX_POSITIONS): instrumenty z pozycją w stanie innym niż FLAT (każdy stan, także ENTERING
i BLOCKED) oraz instrumenty z nierozstrzygniętym zleceniem (`journal.unresolved_orders()`), każdy instrument raz.
Przeglądane są instrumenty pasm i instrumenty nierozstrzygniętych zleceń (dziennik nie ma listy wszystkich pozycji).

Ryzyko zajętego instrumentu (spec §4.3), w EUR:
- znane `size`, `open_level`, `stop_level` pozycji i wartość punktu z JEJ kontekstu wejścia (`entry_ctx`, przez
  `entry_by_deal:<deal_id>` albo wskaźnik `entry_last:<epic>` ze zgodnym zleceniem, jak `Executor._entry_client_id`):
  `size × |open_level − stop_level| × value_per_point`, przeliczone na EUR (waluta z kontekstu, inaczej z pasma);
- ENTERING, samo nierozstrzygnięte zlecenie bez pozycji, BLOCKED bez pól albo nieznany stop, wielkość, wartość punktu:
  pełne `max_loss_per_trade` pasma instrumentu, a dla instrumentu bez pasma `unknown_position_risk` z konfiguracji;
- brak kontekstu wejścia to „nieznane” (wartość zastępcza wyżej), ale kontekst NIECZYTELNY (zły JSON, cudzy
  identyfikator, zła wartość punktu) to błąd: kontrole, które go potrzebują, blokują.

Nominał zajętego instrumentu (PORTFOLIO_LEVERAGE): `size × open_level × value_per_point` pozycji, a dla ENTERING lub
samego zlecenia `size zlecenia × środek notowania decyzji × value_per_point` z kontekstu tego zlecenia. Nominału,
którego nie da się policzyć (brak kontekstu, nieznana waluta), nie zgadujemy: dźwignia blokuje.

Kontrole (próg, granica, powód), kolejność `REASON_ORDER`:

- PORTFOLIO_MAX_POSITIONS: liczba zajętych instrumentów `>= max_open_positions`.
- PORTFOLIO_OPEN_RISK: Σ ryzyka zajętych + ryzyko kandydata `> max_open_risk` (równo przechodzi).
- PORTFOLIO_DAILY_LOSS: `strata = max(0, −realized_pnl(dzień)) + Σ ryzyka zajętych`; blokuje, gdy `strata >=
  max_daily_loss` ALBO `strata + ryzyko kandydata > max_daily_loss`. Dzień portfela to lokalna data kalendarza bramki
  (`calendar.to_local(clock.now_utc()).date()`; dla DE40 XETRA, jak `RiskGate`). Zysk niczego nie kompensuje.
- PORTFOLIO_TOTAL_DRAWDOWN: `realized_pnl_total() <= −max_total_drawdown` (równo blokuje).
- PORTFOLIO_LEVERAGE: `Σ nominałów zajętych + nominał kandydata > max_effective_leverage × min(virtual_capital,
  equity)`; blokuje też, gdy kapitał `<= 0`, konta brak albo jest w innej walucie niż rachunek.
- PORTFOLIO_MARGIN: depozyt kandydata (`size × entry_price × value_per_point × margin_rate`, w EUR) `> available ×
  (1 − margin_reserve_fraction)`; `available` od brokera obejmuje już depozyt wszystkich pozycji rachunku.
- PORTFOLIO_PNL_PENDING: dziennik ma transakcję bez rozliczonego wyniku w EUR (`journal.pnl_pending_deals()`).
- PORTFOLIO_FX_UNAVAILABLE: kandydat albo zajęty instrument w walucie innej niż EUR i brak ważnego kursu.
  Gdy zajętości nie da się odczytać, blokują już kontrole od niej zależne; FX ocenia wtedy tylko kandydata.
- PORTFOLIO_UNMANAGED_EXPOSURE (opcjonalna, pytanie 5): tylko z wstrzykniętym `ExposureView`; blokuje, gdy ostatni
  odczyt brokera pokazał pozycję na instrumencie bez pasma albo odczytu nie da się ocenić.

Kandydat (`risk.EntryCandidate`) na instrumencie bez pasma albo z polem, którego nie da się ocenić (zły typ, wartość
`<= 0`), blokuje kontrole, które go potrzebują (OPEN_RISK, DAILY_LOSS, LEVERAGE, MARGIN); FORMAT i NO_STOP daje bramka
pasma. Przy jednym paśmie DE40 i limitach równych `risk_v1.toml` bramka nie dodaje powodu, którego nie dałaby bramka
pasma (test równoważności w `tests/unit/test_portfolio.py`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow
from typing import Any, Protocol, TypeVar, runtime_checkable

from gwstock.calendar import ExchangeCalendar
from gwstock.clock import Clock
from gwstock.config import PortfolioConfig
from gwstock.journal import Journal, OrderRecord, PositionRecord
from gwstock.risk import Verdict
from gwstock.types import AccountInfo, PositionState

_LOG = logging.getLogger("gwstock.portfolio")

# Powody blokady (`Verdict.reasons`), w kolejności tabeli spec wielu pasm §4.2.
PORTFOLIO_MAX_POSITIONS = "PORTFOLIO_MAX_POSITIONS"
PORTFOLIO_OPEN_RISK = "PORTFOLIO_OPEN_RISK"
PORTFOLIO_DAILY_LOSS = "PORTFOLIO_DAILY_LOSS"
PORTFOLIO_TOTAL_DRAWDOWN = "PORTFOLIO_TOTAL_DRAWDOWN"
PORTFOLIO_LEVERAGE = "PORTFOLIO_LEVERAGE"
PORTFOLIO_MARGIN = "PORTFOLIO_MARGIN"
PORTFOLIO_PNL_PENDING = "PORTFOLIO_PNL_PENDING"
PORTFOLIO_FX_UNAVAILABLE = "PORTFOLIO_FX_UNAVAILABLE"
PORTFOLIO_UNMANAGED_EXPOSURE = "PORTFOLIO_UNMANAGED_EXPOSURE"
REASON_ORDER = (
    PORTFOLIO_MAX_POSITIONS, PORTFOLIO_OPEN_RISK, PORTFOLIO_DAILY_LOSS, PORTFOLIO_TOTAL_DRAWDOWN, PORTFOLIO_LEVERAGE,
    PORTFOLIO_MARGIN, PORTFOLIO_PNL_PENDING, PORTFOLIO_FX_UNAVAILABLE, PORTFOLIO_UNMANAGED_EXPOSURE,
)  # fmt: skip
# Spoza tabeli: sama bramka zawiodła (nieoczekiwany wyjątek), więc blokuje bez oceny kandydata.
PORTFOLIO_GATE_ERROR = "PORTFOLIO_GATE_ERROR"

ACCOUNT_CURRENCY = "EUR"

# Klucze kontekstu wejścia zapisywane przez wykonawcę (`execution.py`); zgodność sprawdza test.
META_ENTRY_CTX = "entry_ctx:{}"
META_ENTRY_BY_DEAL = "entry_by_deal:{}"
META_ENTRY_LAST = "entry_last:{}"
_ENTRY_ROLES = ("-entry", "-smoke")  # role zleceń otwierających pozycję (ostatni człon identyfikatora)

_EXACT = Context(prec=80, traps=[InvalidOperation, Overflow, Inexact])
_ZERO = Decimal(0)
_ONE = Decimal(1)
_T = TypeVar("_T")


class _Unevaluable(Exception):
    """Kwoty nie da się policzyć (brak pola kandydata, nieznana waluta, brak kursu)."""


@dataclass(frozen=True)
class PortfolioLane:
    """Co bramka portfelowa wie o paśmie: instrument, jego waluta i limit straty na transakcję (EUR)."""

    epic: str
    currency: str
    max_loss_per_trade: Decimal

    def __post_init__(self) -> None:
        for name in ("epic", "currency"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"PortfolioLane.{name}: expected a non-empty string, got {value!r}")
        limit = self.max_loss_per_trade
        if not isinstance(limit, Decimal):
            raise TypeError(f"PortfolioLane.max_loss_per_trade: expected Decimal, got {type(limit).__name__}")
        if not limit.is_finite() or limit <= 0:
            raise ValueError(f"PortfolioLane.max_loss_per_trade: expected a finite Decimal > 0, got {limit}")


@runtime_checkable
class FxView(Protocol):
    """Przeliczenie kwoty z waluty instrumentu na EUR (spec §7.3): kurs od brokera, nie starszy niż dozwolony, po
    stronie niekorzystnej dla nas (wynik nie mniejszy od rzeczywistego). None albo wyjątek: kursu brak."""

    def to_eur(self, amount: Decimal, currency: str) -> Decimal | None: ...


@runtime_checkable
class ExposureView(Protocol):
    """Instrumenty bez pasma z pozycją według ostatniego udanego odczytu listy brokera (spec §5.1)."""

    def unmanaged_epics(self) -> Collection[str]: ...


@dataclass(frozen=True)
class _Exposure:
    """Kwoty zajętego instrumentu PRZED przeliczeniem na EUR (brak kursu nie może ukryć waluty przed FX)."""

    epic: str
    currency: str | None  # waluta instrumentu; None: nieznana
    risk: Decimal  # w `risk_currency`
    risk_currency: str  # waluta instrumentu albo EUR (wartość zastępcza z konfiguracji)
    notional: Decimal | None  # w `currency`; None: nie da się policzyć


def _finite(value: object) -> Decimal | None:
    return value if isinstance(value, Decimal) and value.is_finite() else None


def _positive(value: object) -> Decimal | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _product(*factors: Decimal) -> Decimal:
    result = factors[0]
    for factor in factors[1:]:
        result = _EXACT.multiply(result, factor)
    return result


def _total(values: Iterable[Decimal]) -> Decimal:
    result = _ZERO
    for value in values:
        result = _EXACT.add(result, value)
    return result


def _ctx_decimal(ctx: dict[str, Any], key: str) -> Decimal | None:
    """Dodatnia liczba z kontekstu wejścia (zapisana jako napis); brak klucza: None; zła wartość: ValueError."""
    if key not in ctx:
        return None
    text = ctx[key]
    if not isinstance(text, str):
        raise ValueError(f"entry context {key}: expected a decimal string, got {text!r}")
    number = _positive(Decimal(text))  # zły napis: InvalidOperation (ArithmeticError), czyli też błąd
    if number is None:
        raise ValueError(f"entry context {key}: expected a finite decimal > 0, got {text!r}")
    return number


def _memo(fn: Callable[[], _T]) -> Callable[[], _T]:
    """Jedno wywołanie na ocenę; wyjątek też jest zapamiętany i rzucany każdemu, kto pyta (każda kontrola blokuje)."""
    box: list[Any] = []

    def get() -> _T:
        if not box:
            try:
                box.append((True, fn()))
            except Exception as exc:
                box.append((False, exc))
        ok, value = box[0]
        if not ok:
            raise value
        return value

    return get


class PortfolioGate:
    """Bramka portfelowa wejść (`check_entry`); wyjścia `check_exit` (zawsze przechodzą). Szczegóły w module."""

    def __init__(
        self,
        cfg: PortfolioConfig,
        lanes: Iterable[PortfolioLane],
        journal: Journal,
        calendar: ExchangeCalendar,
        clock: Clock,
        *,
        fx: FxView | None = None,
        exposure: ExposureView | None = None,
    ) -> None:
        by_epic: dict[str, PortfolioLane] = {}
        for lane in lanes:
            if not isinstance(lane, PortfolioLane):
                raise TypeError(f"lanes: expected PortfolioLane, got {type(lane).__name__}")
            if lane.epic in by_epic:
                raise ValueError(f"lanes: duplicate lane for {lane.epic} (R1: at most one lane per instrument)")
            by_epic[lane.epic] = lane
        self._cfg = cfg
        self._lanes = by_epic
        self._journal = journal
        self._calendar = calendar
        self._clock = clock
        self._fx = fx
        self._exposure = exposure

    def check_exit(self) -> Verdict:
        """Wyjścia nigdy nie są blokowane: nie zależy od dziennika, zegara, kursu ani konta."""
        return Verdict(True, [])

    def check_entry(self, c: object, account: object) -> Verdict:
        """Werdykt portfela dla kandydata (`risk.EntryCandidate`) i konta (`AccountInfo`); nigdy nie rzuca."""
        try:
            return self._check_entry(c, account)
        except Exception:  # siatka bezpieczeństwa: bramka nie rzuca, a niepewny kandydat nie przechodzi
            _LOG.exception("portfolio gate failed unexpectedly, blocking the entry")
            return Verdict(False, [PORTFOLIO_GATE_ERROR])

    # --- ocena ---

    def _check_entry(self, c: object, account: object) -> Verdict:
        cfg = self._cfg
        epic, size, stop, price, vpp, rate = self._fields(c)
        size_d, stop_d, price_d, vpp_d, rate_d = (_positive(v) for v in (size, stop, price, vpp, rate))
        lane = self._lanes.get(epic) if isinstance(epic, str) else None
        currency = None if lane is None else lane.currency
        day = self._today()

        def candidate_amount(*factors: Decimal | None) -> Decimal:
            if currency is None or any(f is None for f in factors):
                raise _Unevaluable("candidate amount cannot be evaluated")
            return self._to_eur(_product(*factors), currency)  # type: ignore[arg-type]

        candidate_risk = _memo(lambda: candidate_amount(size_d, stop_d, vpp_d))
        occupied = _memo(self._occupied)
        exposures = _memo(lambda: [self._exposure_of(epic_, pos, orders) for epic_, pos, orders in occupied()])
        open_risk = _memo(lambda: _total(self._to_eur(e.risk, e.risk_currency) for e in exposures()))

        def account_ok() -> AccountInfo:
            if not isinstance(account, AccountInfo) or account.currency != ACCOUNT_CURRENCY:
                raise _Unevaluable("no account in the account currency")
            return account

        def max_positions() -> bool:
            return len(occupied()) >= cfg.max_open_positions

        def open_risk_check() -> bool:
            return _EXACT.add(open_risk(), candidate_risk()) > cfg.max_open_risk

        def daily_loss() -> bool:
            if day is None:
                return True
            lost = _EXACT.add(max(_ZERO, _EXACT.minus(self._journal.realized_pnl(day))), open_risk())
            if lost >= cfg.max_daily_loss:
                return True
            return _EXACT.add(lost, candidate_risk()) > cfg.max_daily_loss

        def total_drawdown() -> bool:
            return self._journal.realized_pnl_total() <= _EXACT.minus(cfg.max_total_drawdown)

        def leverage() -> bool:
            acc = account_ok()
            capital = min(cfg.virtual_capital, acc.equity)
            if capital <= 0:
                return True
            notionals = []
            for e in exposures():
                if e.notional is None or e.currency is None:
                    return True  # nominału zajętego instrumentu nie znamy: nie zgadujemy
                notionals.append(self._to_eur(e.notional, e.currency))
            used = _EXACT.add(_total(notionals), candidate_amount(size_d, price_d, vpp_d))
            return used > _product(cfg.max_effective_leverage, capital)

        def margin() -> bool:
            acc = account_ok()
            required = candidate_amount(size_d, price_d, vpp_d, rate_d)
            usable = _product(acc.available, _EXACT.subtract(_ONE, cfg.margin_reserve_fraction))
            return required > usable

        def pnl_pending() -> bool:
            return len(self._journal.pnl_pending_deals()) > 0

        def fx_unavailable() -> bool:
            currencies = {currency}
            try:
                currencies |= {e.currency for e in exposures()}
            except Exception:  # zajętość nieczytelna: blokują już kontrole od niej zależne (zob. moduł)
                pass
            for code in sorted(c for c in currencies if c is not None and c != ACCOUNT_CURRENCY):
                self._to_eur(_ONE, code)
            return False

        def unmanaged() -> bool:
            if self._exposure is None:
                return False
            epics = self._exposure.unmanaged_epics()
            if isinstance(epics, (str, bytes)) or not isinstance(epics, Collection):
                raise _Unevaluable(f"unmanaged_epics: expected a collection of epics, got {epics!r}")
            return len(epics) > 0

        checks: list[tuple[str, Callable[[], bool]]] = [
            (PORTFOLIO_MAX_POSITIONS, max_positions),
            (PORTFOLIO_OPEN_RISK, open_risk_check),
            (PORTFOLIO_DAILY_LOSS, daily_loss),
            (PORTFOLIO_TOTAL_DRAWDOWN, total_drawdown),
            (PORTFOLIO_LEVERAGE, leverage),
            (PORTFOLIO_MARGIN, margin),
            (PORTFOLIO_PNL_PENDING, pnl_pending),
            (PORTFOLIO_FX_UNAVAILABLE, fx_unavailable),
            (PORTFOLIO_UNMANAGED_EXPOSURE, unmanaged),
        ]
        reasons = [reason for reason, check in checks if self._blocks(reason, check)]
        return Verdict(not reasons, reasons)

    @staticmethod
    def _fields(c: object) -> tuple:
        values = []
        for name in ("epic", "size", "stop_distance", "entry_price", "value_per_point", "margin_rate"):
            try:
                values.append(getattr(c, name, None))
            except Exception:  # właściwość, która rzuca: pole jak brakujące
                values.append(None)
        return tuple(values)

    @staticmethod
    def _blocks(reason: str, check: Callable[[], bool]) -> bool:
        try:
            return bool(check())
        except Exception as exc:  # nie da się ocenić = blokada
            _LOG.warning("portfolio check %s could not be evaluated, blocking: %s: %s", reason, type(exc).__name__, exc)
            return True

    def _today(self) -> date | None:
        try:
            return self._calendar.to_local(self._clock.now_utc()).date()
        except Exception as exc:
            _LOG.warning("portfolio gate cannot determine the trading day: %s: %s", type(exc).__name__, exc)
            return None

    def _to_eur(self, amount: Decimal, currency: str) -> Decimal:
        if currency == ACCOUNT_CURRENCY:
            return amount
        if self._fx is None:
            raise _Unevaluable(f"no FX view for {currency}")
        converted = self._fx.to_eur(amount, currency)
        # zero za dodatnią kwotę to też brak kursu: ryzyko kandydata w EUR zawsze > 0, gdy da się je ocenić
        usable = isinstance(converted, Decimal) and converted.is_finite() and converted >= 0
        if not usable or (amount > 0) != (converted > 0):
            raise _Unevaluable(f"no usable {currency}->EUR rate (got {converted!r})")
        return converted

    # --- zajętość i ryzyko otwarte ---

    def _occupied(self) -> list[tuple[str, PositionRecord | None, list[OrderRecord]]]:
        """Zajęte instrumenty: (epic, pozycja != FLAT albo None, nierozstrzygnięte zlecenia), w kolejności epic."""
        orders: dict[str, list[OrderRecord]] = {}
        for order in self._journal.unresolved_orders():
            orders.setdefault(order.epic, []).append(order)
        result = []
        for epic in sorted(set(self._lanes) | set(orders)):
            position = self._journal.current_position(epic)
            if position is not None and position.state is PositionState.FLAT:
                position = None
            if position is not None or epic in orders:
                result.append((epic, position, orders.get(epic, [])))
        return result

    def _exposure_of(self, epic: str, position: PositionRecord | None, orders: list[OrderRecord]) -> _Exposure:
        lane = self._lanes.get(epic)
        fallback = self._cfg.unknown_position_risk if lane is None else lane.max_loss_per_trade
        settled = position is not None and position.state is not PositionState.ENTERING
        ctx: dict[str, Any] | None = None
        entry: OrderRecord | None = None
        if settled:
            ctx = self._deal_context(position.deal_id, epic)  # type: ignore[union-attr]
        else:
            entries = [o for o in orders if o.order_id.endswith(_ENTRY_ROLES)]
            if entries:
                entry = entries[-1]
                ctx = self._context(entry.order_id, epic)
        currency = lane.currency if lane is not None else None
        if ctx is not None and "currency" in ctx:
            currency = ctx["currency"]
            if not isinstance(currency, str) or not currency.strip():
                raise ValueError(f"entry context of {epic}: bad currency {currency!r}")
        vpp = None if ctx is None else _ctx_decimal(ctx, "value_per_point")

        risk, risk_currency = fallback, ACCOUNT_CURRENCY
        notional: Decimal | None = None
        if settled and currency is not None and vpp is not None:
            assert position is not None
            size, level, stop = position.size, position.open_level, position.stop_level
            if size is not None and level is not None and stop is not None:
                distance = _EXACT.abs(_EXACT.subtract(level, stop))
                risk, risk_currency = _product(size, distance, vpp), currency
            if size is not None and level is not None:
                notional = _product(size, _EXACT.abs(level), vpp)
        elif entry is not None and ctx is not None and currency is not None and vpp is not None:
            bid, ask = _ctx_decimal(ctx, "decision_bid"), _ctx_decimal(ctx, "decision_ask")
            if bid is not None and ask is not None:
                mid = _EXACT.divide(_EXACT.add(bid, ask), Decimal(2))
                notional = _product(entry.size, mid, vpp)
        return _Exposure(epic, currency, risk, risk_currency, notional)

    def _deal_context(self, deal_id: str | None, epic: str) -> dict[str, Any] | None:
        """Kontekst wejścia pozycji `deal_id` (jak `Executor._entry_client_id`). None: brak; nieczytelny: ValueError."""
        if deal_id is None:
            return None
        client_id = self._journal.get_meta(META_ENTRY_BY_DEAL.format(deal_id))
        if not client_id:
            last = self._journal.get_meta(META_ENTRY_LAST.format(epic))
            order = self._journal.get_order(last) if last else None
            if order is None or order.deal_id != deal_id or order.epic != epic:
                return None
            client_id = last
        return self._context(client_id, epic)

    def _context(self, client_id: str, epic: str) -> dict[str, Any] | None:
        text = self._journal.get_meta(META_ENTRY_CTX.format(client_id))
        if text is None:
            return None
        ctx = json.loads(text)
        if not isinstance(ctx, dict) or ctx.get("client_id") != client_id or ctx.get("epic", epic) != epic:
            raise ValueError(f"entry context of {client_id!r} is malformed")
        return ctx
