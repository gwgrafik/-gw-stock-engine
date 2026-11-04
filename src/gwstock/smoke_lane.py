"""Próba akceptacyjna pasma z celem (`python -m gwstock smoke --epic US500 [--hold SECONDS]`, zadanie 17).

Jedna krótka próba na prawdziwym rachunku, która rozstrzyga niepotwierdzone założenia o brokerze dla pasma z celem
zysku (projekt wielu pasm §3.1, §7, §7.5): C5 (nazwy i obecność kluczy celu w `GET /positions` i `GET /confirms` oraz
czy `profitDistance` liczy się od ceny wykonania), C5d (czy `PUT` z samym stopem kasuje cel), C7 (przyczyna zamknięcia
w historii brokera) i C8 (waluta i kwota rozliczenia wyniku w USD na rachunku w EUR).

Warunki: proces live (cztery warunki uzbrojenia procesu, jak każda próba `smoke`) ORAZ pasmo włączone w
`config/lanes.toml`. Zamrożony odcisk pasma NIE jest wymagany (próba poprzedza zamrożenie), ale próba nie zmienia trybu
pasma i go nie uzbraja: dostaje osobnego wykonawcę (`Engine.smoke_executor`) i jedno przepuszczenie wejścia roli `smoke`
(`Engine.lane_smoke_entry`); niczego nie zapisuje poza dziennikiem (żadnych plików uzbrojenia, `live_allowed`).

Kroki (PASS/FAIL/INFO w stylu próby DE40, bez identyfikatorów deali i rachunków w wydruku):
 [1] rachunek; [2] wejście minimalną wielkością pasma z celem i stopem (`TargetedEntryRequest`; stop = ATR świec M5 pasma
 (`atr_period`), nie mniej niż minimum brokera i `min_stop_fraction` ceny; cel = `target_r` × stop w dół do kroku, czyli
 kilka razy dalej niż typowy ruch w kilkanaście sekund), rola `smoke` (nie zużywa wejść dnia ani sesji L1), bramka pasma
 `check_smoke` i bramka portfelowa jak przy wejściu pasma (brak kursu pary walutowej = brak próby, nic nie wysłano);
 [3] pozycja widoczna ze stopem i celem, potem wstrzymanie `--hold` (domyślnie `LANE_HOLD_DEFAULT_S`, najmniej
 `LANE_HOLD_MIN_S`) pod zwykłym nadzorem; [4] surowe pola celu z `GET /positions/{dealId}` i `GET /confirms` (C5);
 [5] `set_protection` z obu poziomami (cel o krok bliżej, gdy wolno) i ich weryfikacja; [6] `set_stop` z samym stopem,
 odczyt, czy cel przetrwał (C5d), i przywrócenie `set_protection`; [7] spłaszczenie przez silnik; [8] pozycja znikła,
 transakcja zaksięgowana w walucie instrumentu, przyczyna zamknięcia z historii (C7); [9] faza 2 w EUR (szacunek
 `ESTIMATED_FX`); [10] drugie zamknięcie odrzucone `POSITION_NOT_FOUND`; [11] historia transakcji i saldo (C8). Na
 końcu blok `USTALENIA O BROKERZE` z POTWIERDZONE / ROZBIEŻNE / NIE USTALONO dla C5, C5d, C7, C8.

Bezpieczeństwo: każda awaria po wejściu (wyjątek, anulowanie, Ctrl-C, sygnał) kończy się spłaszczeniem procedurą
awaryjną wykonawcy próby (`cli._close_after_interrupt`), a pozycja, której nie da się zamknąć (np. zamknięty rynek),
dostaje głośną linię z tym, co zostało u brokera; każda wątpliwość w trakcie (brak stopu po zmianie ochrony, pozycja
znikła) przerywa diagnostykę i spłaszcza od razu. Wyjść nie blokuje żadna bramka.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from gwstock import cli
from gwstock.app import ACCOUNT_CURRENCY, Engine, Mode
from gwstock.broker.diagnostics import FieldsRead, SettlementRow, TargetFieldsReport, show
from gwstock.fx import ESTIMATED_FX
from gwstock.lanes import _true_range_sum
from gwstock.strategy import round_down_to_step, round_up_to_step
from gwstock.strategy_bars import BarEntryPlan
from gwstock.types import BrokerRejected, ClosedDeal, OrderState, PositionState, Side

__all__ = ["smoke_lane", "probe_distances", "LANE_HOLD_DEFAULT_S", "LANE_HOLD_MIN_S"]

CONFIRMED = "POTWIERDZONE"
DIVERGENT = "ROZBIEŻNE"
UNKNOWN = "NIE USTALONO"
_RANK = {CONFIRMED: 0, UNKNOWN: 1, DIVERGENT: 2}

# Stałe procedury próby (nie parametry strategii ani ryzyka).
LANE_HOLD_DEFAULT_S = 20  # domyślne wstrzymanie: pola celu muszą być widoczne w API i w aplikacji
LANE_HOLD_MIN_S = 15
CLOSURE_LOOKBACK = timedelta(minutes=2)  # historia zamknięć i transakcji od chwili przed wejściem
TX_ATTEMPTS = 5  # odczyty historii transakcji, zanim wiersz TRADE się pojawi (opóźnienie C8)
TX_PAUSE_S = 3
FLATTEN_REASON = "SMOKE"
POSITION_NOT_FOUND = "POSITION_NOT_FOUND"
ADOPTED_AFTER_UNKNOWN = "ADOPTED_AFTER_UNKNOWN"
LANE_LEFT_OPEN_HINT = (
    "left open with its stop (and target); start the engine (it reconciles the lane and flattens it at the lane's "
    "flatten time) or close it in the Capital.com app"
)
_KNOWN_TARGET_KEYS = ("profitLevel", "profitDistance")  # klucze, które czyta adapter (`parse_position`)


@dataclass
class Finding:
    status: str = UNKNOWN
    summary: str = "nie sprawdzono (próba zatrzymała się przed tym krokiem)"
    details: list[str] = field(default_factory=list)


def probe_distances(
    rules: Any, quote: Any, atr: Decimal | None, bar: Any
) -> tuple[Decimal, Decimal]:
    """(odległość stopu, odległość celu) próby pasma, wyłącznie z reguł brokera i pliku pasma.

    Stop: ATR świec M5 (`bar.atr_period`), nie mniej niż minimum brokera i `bar.min_stop_fraction` ceny środkowej; bez
    ATR: 2 × to minimum; w górę do kroku stopu. Cel: `bar.target_r` × stop w dół do kroku (cel bliżej, nigdy dalej),
    nie mniej niż minimum brokera (wspólne dla stopu i celu)."""
    floor = max(rules.min_stop_distance, bar.min_stop_fraction * quote.mid)
    stop = max(atr, floor) if atr is not None and atr > 0 else 2 * floor
    target = bar.target_r * stop
    if rules.stop_step > 0:
        stop = round_up_to_step(stop, rules.stop_step)
        target = round_down_to_step(bar.target_r * stop, rules.stop_step)
        if target < rules.min_stop_distance:
            target = round_up_to_step(rules.min_stop_distance, rules.stop_step)
    return stop, max(target, rules.min_stop_distance)


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: _RANK[s]) if statuses else UNKNOWN


def _shown_target(read: FieldsRead | None, side: Side, open_level: Decimal) -> tuple[bool, Decimal | None]:
    """(czy broker pokazał cel, poziom celu albo None dla jawnego null) z surowych kluczy: najpierw klucz poziomu
    (`*Level`), potem odległość (`*Distance`, od poziomu otwarcia po stronie zysku)."""
    if read is None or not read.found or not read.target:
        return False, None
    for key, value in read.target.items():
        if key.lower().endswith("level"):
            return True, value if isinstance(value, Decimal) else None
    for key, value in read.target.items():
        if key.lower().endswith("distance"):
            if not isinstance(value, Decimal):
                return True, None
            return True, open_level + value if side is Side.BUY else open_level - value
    return False, None


class _Probe:
    def __init__(self, engine: Engine, epic: str, out: Callable[[str], None]) -> None:
        self.engine = engine
        self.epic = epic
        self.out = out
        self.journal = engine.journal
        self.clock = engine.clock
        self.lane = engine.lane_for(epic)
        self.results: list[bool] = []
        self.findings = {"C5": Finding(), "C5d": Finding(), "C7": Finding(), "C8": Finding()}
        self.executor: Any = None
        self.deal_id: str | None = None
        self.reference: str | None = None
        self.plan: BarEntryPlan | None = None
        self.open_level: Decimal | None = None
        self.side = Side.BUY
        self.entry_ts: datetime | None = None
        self.balance_before: Decimal | None = None
        self.position_other: dict[str, Any] = {}
        self.estimate_eur: Decimal | None = None
        self.target_now: Decimal | None = None
        self.adopted = False
        self.gone = False  # pozycja próby potwierdzona jako zniknięta u brokera (krok 8)

    # --- wydruk ---

    def step(self, n: int, ok: bool, text: str) -> bool:
        self.out(f"{'PASS' if ok else 'FAIL'} [{n}] {self.scrub(text)}")
        self.results.append(ok)
        return ok

    def info(self, n: int, text: str) -> None:
        self.out(f"INFO [{n}] {self.scrub(text)}")

    def scrub(self, text: str) -> str:
        """Bez identyfikatorów deala i referencji (także w tekstach błędów brokera)."""
        for secret, mark in ((self.deal_id, "<deal>"), (self.reference, "<ref>")):
            if secret:
                text = text.replace(secret, mark)
        return text

    def error(self, exc: BaseException) -> str:
        reason = exc.reason if isinstance(exc, BrokerRejected) else str(exc)
        return f"{type(exc).__name__}: {show(self.scrub(reason))}" if reason else type(exc).__name__

    def finding(self, key: str, status: str, summary: str, details: list[str] | None = None) -> None:
        self.findings[key] = Finding(status, summary, list(details or []))

    def print_findings(self) -> None:
        currency = self.lane.loaded.cfg.currency if self.lane is not None else "?"
        self.out(f"USTALENIA O BROKERZE ({self.epic}, waluta instrumentu {currency}, rachunek {ACCOUNT_CURRENCY}; "
                 "bez identyfikatorów; wklej cały blok)")  # fmt: skip
        for key, f in self.findings.items():
            self.out(f"{key:<3} {f.status}: {self.scrub(f.summary)}")
            for line in f.details:
                self.out(f"    - {self.scrub(line)}")

    # --- przebieg ---

    async def run(self, hold_s: int) -> int:
        engine, epic = self.engine, self.epic
        if engine.mode is not Mode.LIVE:
            self.out("FAIL smoke --epic runs only in live mode (live_allowed, data/ARM_LIVE, bound account, "
                     "pre-registration); the engine is in dry_run, nothing was sent")  # fmt: skip
            return 1
        if epic == engine.strat.instrument:
            self.out(f"FAIL smoke --epic {epic}: {epic} is the v1 lane; run smoke without --epic")
            return 1
        if self.lane is None:
            self.out(f"FAIL smoke --epic {epic}: no enabled lane for {epic} in config/lanes.toml; nothing was sent")
            return 1
        engine.check_wiring()
        self.executor = engine.smoke_executor(epic)
        mode = "live" if self.lane.executor is not None and self.lane.executor.live else "dry_run"
        why = f" ({self.lane.arming_problem})" if self.lane.arming_problem else ""
        self.info(0, f"lane {epic} mode {mode}{why}: the probe neither arms the lane nor changes its mode")
        if hold_s < LANE_HOLD_MIN_S:
            self.info(0, f"hold raised from {hold_s} s to {LANE_HOLD_MIN_S} s so the target fields can be seen")
            hold_s = LANE_HOLD_MIN_S
        try:
            return await self._run(hold_s)
        finally:
            self.print_findings()

    async def _run(self, hold_s: int) -> int:
        if not await self._account():
            return 1
        if not await self._enter():
            return 1
        try:
            await self._after_entry(hold_s)
        except GeneratorExit:  # zamykana korutyna nie może już czekać
            raise
        except BaseException as exc:  # wyjątek, anulowanie, Ctrl-C, sygnał: pozycja próby nie może zostać bez słowa
            self.info(7, f"probe interrupted ({type(exc).__name__}); closing the probe position now")
            await cli._close_after_interrupt(
                self.engine, self.deal_id, self.out, executor=self.executor, step=7, name="the probe position",
                hint=LANE_LEFT_OPEN_HINT,
            )  # fmt: skip
            if not isinstance(exc, Exception):
                raise
            self.results.append(False)
        return self._summary()

    def _summary(self) -> int:
        passed = all(self.results)
        note = " (WARN [2]: entry adopted after UNKNOWN, read it above)" if self.adopted else ""
        self.out(f"{'PASS' if passed else 'FAIL'} smoke --epic {self.epic}: {sum(self.results)}/{len(self.results)} "
                 f"checks passed{note}")  # fmt: skip
        return 0 if passed else 1

    # --- [1] rachunek ---

    async def _account(self) -> bool:
        engine = self.engine
        if not await engine.login():
            return self.step(1, False, "login failed")
        try:
            problem = await engine.verify_account()
        except Exception as exc:
            return self.step(1, False, f"account unreadable ({type(exc).__name__})")
        if problem is not None:
            return self.step(1, False, f"account check: {problem}")
        return self.step(1, True, "bound account found, ENABLED, EUR, balance > 0, active in this session")

    # --- [2] wejście ---

    async def _enter(self) -> bool:
        engine, epic, journal = self.engine, self.epic, self.journal
        engine.kill.scan_files()
        result = await engine.reconcile_lane_once(epic)
        position = journal.current_position(epic)
        unresolved = [o for o in journal.unresolved_orders() if o.epic == epic]
        busy = (position is not None and position.state is not PositionState.FLAT) or bool(unresolved)
        if result is None or result.skipped or busy:
            return self.step(2, False, f"smoke refuses: reconciliation of {epic} failed or a position/unresolved "
                                       f"order exists on {epic}")  # fmt: skip
        try:
            rules = await engine.broker.instrument_rules(epic)
            quote = await engine.broker.quote(epic)
        except Exception as exc:
            return self.step(2, False, f"rules/quote unavailable ({self.error(exc)})")
        if self.executor.foreign and await engine.refresh_fx() is None:
            return self.step(2, False, "no valid quote of the portfolio FX pair (config/portfolio.toml fx_quote_epic):"
                                       " no rate, no probe sent")  # fmt: skip
        atr = await self._atr()
        stop, target = probe_distances(rules, quote, atr, self.lane.bar)
        if atr is None:
            self.info(2, "M5 ATR unavailable: stop = 2 x the larger of the broker minimum and min_stop_fraction")
        self.plan = BarEntryPlan(self.side, rules.min_size, stop, target, quote)
        self.balance_before = await self._balance()
        self.entry_ts = self.clock.now_utc()
        day = engine.trading_day()
        try:
            with engine.lane_smoke_entry(epic):
                rec = await self.executor.enter(self.plan, rules, day, smoke=True)
        except Exception as exc:
            self.step(2, False, f"entry failed ({self.error(exc)})")
            await self._resolve_unknown()
            return False
        if rec is None or rec.state is not OrderState.CONFIRMED:
            blocks = journal.events("gate_block")
            why = json.loads(blocks[-1].payload).get("reasons") if rec is None and blocks else None
            note = f" ({show(rec.note)})" if rec is not None and rec.note else ""
            self.step(2, False, f"entry not confirmed: {rec.state.value if rec else 'not sent'}{note}"
                                + (f" (gate: {', '.join(why)})" if why else ""))  # fmt: skip
            if rec is not None and rec.state not in (OrderState.REJECTED, OrderState.NOT_PLACED):
                await self._resolve_unknown()
            return False
        self.deal_id, self.reference = rec.deal_id, rec.deal_reference
        self.adopted = ADOPTED_AFTER_UNKNOWN in rec.note.split()
        return True

    async def _atr(self) -> Decimal | None:
        bar = self.lane.bar
        span = timedelta(minutes=bar.bar_minutes)
        now = self.clock.now_utc()
        need = bar.atr_period + 1
        try:
            candles = await self.engine.broker.candles_5m(self.epic, now - span * (3 * need), now)
        except Exception:
            return None
        done = sorted((c for c in candles if c.start + span <= now), key=lambda c: c.start)
        if len(done) < need:
            return None
        return _true_range_sum(done, bar.atr_period) / bar.atr_period

    async def _balance(self) -> Decimal | None:
        try:
            return (await self.engine.broker.account()).equity
        except Exception:
            return None

    async def _resolve_unknown(self) -> None:
        """Wynik wejścia nieznany: uzgadnianie pasma (do `SMOKE_SETTLE_ATTEMPTS` przebiegów) przyjmuje albo blokuje
        pozycję; nasza otwarta → spłaszczenie; nadal nierozstrzygnięta → głośna linia dla właściciela."""
        for attempt in range(cli.SMOKE_SETTLE_ATTEMPTS):
            if attempt:
                await self.clock.sleep(cli.SMOKE_SETTLE_PAUSE_S)
            await self.engine.reconcile_lane_once(self.epic)
            position = self.journal.current_position(self.epic)
            if position is not None and position.state in (PositionState.OPEN, PositionState.EXITING):
                self.deal_id = position.deal_id
                if await self._flatten_position():
                    self.info(2, "the entry was adopted by reconciliation and closed again; the probe stops here")
                break
            unresolved = any(o.epic == self.epic for o in self.journal.unresolved_orders())
            if (position is None or position.state is PositionState.FLAT) and not unresolved:
                break
        position = self.journal.current_position(self.epic)
        if position is not None and position.state is not PositionState.FLAT:
            self.out(f"FAIL [2] LOUD: the entry outcome is unresolved ({position.state.value}); a position may exist "
                     "with its broker stop: check the Capital.com app and start the engine, whose startup "
                     "reconciliation adopts or blocks it before anything else")  # fmt: skip

    # --- po wejściu ---

    async def _after_entry(self, hold_s: int) -> None:
        engine, epic, journal, plan = self.engine, self.epic, self.journal, self.plan
        assert plan is not None
        if self.adopted:
            self.out("WARN [2]: entry adopted by reconciliation, not by the confirmation; read the order note in the "
                     "journal (broker.confirm) before arming")  # fmt: skip
        position = journal.current_position(epic)
        at_broker = await cli._find_position(engine, self.deal_id)
        self.open_level = position.open_level if position is not None else None
        stop_src = self._source(at_broker, "stop")
        target_src = self._source(at_broker, "target")
        self.step(
            2, position is not None and position.state is PositionState.OPEN,
            f"entry accepted: size {position.size if position else '?'}, open level "
            f"{position.open_level if position else '?'}, stop level {position.stop_level if position else '?'} "
            f"({stop_src}), target level {at_broker.target_level if at_broker else '?'} ({target_src}); requested "
            f"stop distance {plan.stop_distance}, target distance {plan.target_distance}",
        )  # fmt: skip
        visible = at_broker is not None and at_broker.stop_level is not None and at_broker.target_level is not None
        self.step(3, visible, "position visible in list_positions with a stop and a target"
                  + ("" if at_broker is None or at_broker.target_level is not None else ": NO TARGET"))  # fmt: skip
        if at_broker is not None and (at_broker.stop_derived or at_broker.target_derived):
            self.info(3, "the API did not show the stop or the target: verify both in the Capital.com app")
        self.target_now = at_broker.target_level if at_broker is not None else None
        if at_broker is not None and position is not None and position.state is PositionState.OPEN:
            await cli._hold(
                engine, self.deal_id, hold_s, self.info, epic=epic,
                reconcile=lambda: engine.reconcile_lane_once(epic), what="its stop and target",
            )  # fmt: skip
        if await self._still_open():
            await self._diagnostics()
        else:
            self.info(4, "diagnostics skipped: the position is no longer open (stop or target during the hold?)")
        await self._flatten()
        trade = await self._booked()
        await self._phase_two(trade)
        await self._second_close()
        await self._settlement()

    @staticmethod
    def _source(p: Any, what: str) -> str:
        if p is None:
            return "not listed"
        level = p.stop_level if what == "stop" else p.target_level
        derived = p.stop_derived if what == "stop" else p.target_derived
        if level is None:
            return "none"
        return "derived from our request" if derived else "reported by the API"

    async def _still_open(self) -> bool:
        position = self.journal.current_position(self.epic)
        if position is None or position.state is not PositionState.OPEN or position.deal_id != self.deal_id:
            return False
        at_broker = await cli._find_position(self.engine, self.deal_id)
        return at_broker is not None and at_broker.stop_level is not None

    # --- [4]-[6] diagnostyka ---

    async def _read_fields(self) -> TargetFieldsReport | None:
        fn = getattr(self.engine.raw_broker, "diagnose_target_fields", None)
        if fn is None:
            return None
        return await self.engine.probe(fn, self.deal_id, self.reference)

    def _print_fields(self, n: int, where: str, read: FieldsRead) -> None:
        if not read.found:
            self.info(n, f"{where}: not found" + (f" ({read.error})" if read.error else ""))
            return
        keys = ", ".join(f"{k}={show(v)}" for k, v in read.target.items()) or "no target keys"
        other = ", ".join(f"{k}={show(v)}" for k, v in read.other.items()) or "none"
        self.info(n, f"{where}: target fields: {keys}; other fields: {other}")

    async def _diagnostics(self) -> None:
        try:
            report = await self._read_fields()
        except Exception as exc:
            self.step(4, False, f"broker target fields unreadable ({self.error(exc)})")
            self.finding("C5", UNKNOWN, f"odczyt pól celu nieudany ({self.error(exc)})")
            report = None
        else:
            if report is None:
                self.step(4, False, "the adapter has no diagnostic read of the target fields")
                self.finding("C5", UNKNOWN, "adapter bez odczytu diagnostycznego pól celu")
            else:
                self._print_fields(4, "GET /positions/{dealId}", report.position)
                self._print_fields(4, "GET /confirms/{dealReference}", report.confirm)
                self.position_other = dict(report.position.other)
                self.step(4, report.position.found, "broker target fields read (GET /positions, GET /confirms)")
                self._judge_c5(report)
        if not await self._still_open():
            self.info(5, "stopped: the position is gone or shows no stop; flattening at once")
            return
        if not await self._protection_step(report):
            return
        if not await self._still_open():
            self.info(6, "stopped: the position is gone or shows no stop; flattening at once")
            return
        await self._c5d_step(report)

    def _judge_c5(self, report: TargetFieldsReport) -> None:
        plan = self.plan
        assert plan is not None and self.open_level is not None
        statuses: list[str] = []
        details: list[str] = []
        pos, conf = report.position, report.confirm
        if not pos.found:
            statuses.append(UNKNOWN)
            details.append("GET /positions/{dealId}: pozycji nie odczytano")
        elif not pos.target:
            statuses.append(DIVERGENT)
            details.append("GET /positions/{dealId}: brak kluczy celu (adapter używa celu wyliczonego z żądania: "
                           "sprawdź cel w aplikacji)")  # fmt: skip
        else:
            keys = ", ".join(f"{k}={show(v)}" for k, v in pos.target.items())
            known = [k for k in pos.target if k in _KNOWN_TARGET_KEYS]
            statuses.append(CONFIRMED if known else DIVERGENT)
            details.append(f"GET /positions/{{dealId}}: klucze celu {keys}"
                           + ("" if known else " (inne niż profitLevel/profitDistance, których adapter szuka)"))
        if not conf.found:
            statuses.append(UNKNOWN)
            details.append("GET /confirms/{dealReference}: potwierdzenia nie odczytano")
        elif not conf.target:
            statuses.append(DIVERGENT)
            details.append("GET /confirms/{dealReference}: brak kluczy celu (adapter ich nie wymaga)")
        else:
            keys = ", ".join(f"{k}={show(v)}" for k, v in conf.target.items())
            statuses.append(CONFIRMED if "profitLevel" in conf.target else DIVERGENT)
            details.append(f"GET /confirms/{{dealReference}}: klucze celu {keys}")
        shown, level = _shown_target(pos, self.side, self.open_level)
        level_key = shown and any(k.lower().endswith("level") for k in pos.target)
        distance = plan.target_distance
        from_fill = self.open_level + distance if self.side is Side.BUY else self.open_level - distance
        quote = plan.decision_quote
        from_quote = (quote.ask + distance) if self.side is Side.BUY else (quote.bid - distance)
        if not level_key or level is None:
            statuses.append(UNKNOWN)
            details.append("punkt odniesienia profitDistance: nie ustalono (broker nie podał poziomu celu)")
        elif from_fill == from_quote:
            statuses.append(UNKNOWN if level == from_fill else DIVERGENT)
            details.append(f"punkt odniesienia profitDistance: poziom celu {level}, wykonanie {self.open_level} równe "
                           f"notowaniu decyzji, więc wykonania i notowania nie da się tu rozróżnić "
                           f"(oczekiwany poziom {from_fill})")  # fmt: skip
        elif level == from_fill:
            statuses.append(CONFIRMED)
            details.append(f"punkt odniesienia profitDistance: cena wykonania (poziom celu {level} = wykonanie "
                           f"{self.open_level} {'+' if self.side is Side.BUY else '-'} {distance})")  # fmt: skip
        elif level == from_quote:
            statuses.append(DIVERGENT)
            details.append(f"punkt odniesienia profitDistance: notowanie, nie wykonanie (poziom celu {level} = "
                           f"notowanie decyzji ± {distance}; wykonanie {self.open_level})")  # fmt: skip
        else:
            statuses.append(DIVERGENT)
            details.append(f"punkt odniesienia profitDistance: poziom celu {level} różny od wykonania ± odległość "
                           f"({from_fill}) i od notowania ± odległość ({from_quote})")  # fmt: skip
        status = _worst(statuses)
        summary = {
            CONFIRMED: "pola celu obecne pod nazwami, które czyta adapter; odległość liczona od ceny wykonania",
            UNKNOWN: "część ustaleń niemożliwa w tej próbie (szczegóły niżej)",
            DIVERGENT: "broker zachowuje się inaczej niż założenia adaptera (szczegóły niżej)",
        }[status]
        self.finding("C5", status, summary, details)

    def _closer_target(self, target: Decimal, rules_step: Decimal, min_distance: Decimal) -> Decimal:
        """Cel o jeden krok bliżej ceny otwarcia (prawdziwa zmiana dla `PUT`), gdy nadal jest dalej niż minimum."""
        if rules_step <= 0 or self.open_level is None:
            return target
        closer = target - rules_step if self.side is Side.BUY else target + rules_step
        if abs(closer - self.open_level) < min_distance + rules_step:
            return target
        return closer

    async def _protection_step(self, report: TargetFieldsReport | None) -> bool:
        engine, plan = self.engine, self.plan
        assert plan is not None and self.open_level is not None
        at_broker = await cli._find_position(engine, self.deal_id)
        if at_broker is None or at_broker.stop_level is None:
            self.info(5, "stopped: the position is not listed with a stop; flattening at once")
            return False
        stop = at_broker.stop_level
        base = at_broker.target_level
        if base is None:
            distance = plan.target_distance
            base = self.open_level + distance if self.side is Side.BUY else self.open_level - distance
        try:
            rules = await engine.broker.instrument_rules(self.epic)
            target = self._closer_target(base, rules.stop_step, rules.min_stop_distance)
        except Exception:
            target = base
        try:
            await engine.probe(engine.raw_broker.set_protection, self.deal_id, stop, target)
        except Exception as exc:
            self.step(5, False, f"set_protection(stop {stop}, target {target}) failed ({self.error(exc)})")
            return await self._still_open()
        self.target_now = target
        ok, text = await self._verify(stop, target)
        self.step(5, ok, f"set_protection with both levels: {text}")
        return await self._still_open()

    async def _verify(self, stop: Decimal, target: Decimal) -> tuple[bool, str]:
        """Oba poziomy po zmianie ochrony: z surowego odczytu, gdy broker je pokazuje (różnica = FAIL); niepokazany
        poziom jest tylko opisany (właściciel sprawdza go w aplikacji)."""
        try:
            report = await self._read_fields()
        except Exception as exc:
            return False, f"stop {stop}, target {target}: read-back failed ({self.error(exc)})"
        at_broker = await cli._find_position(self.engine, self.deal_id)
        if at_broker is None or at_broker.stop_level is None:
            return False, f"stop {stop}, target {target}: the position is not listed with a stop"
        ok, parts = True, []
        other = report.position.other if report is not None else {}
        if "stopLevel" in other:
            same = other["stopLevel"] == stop
            ok &= same
            parts.append(f"stop {stop} ({'confirmed' if same else 'DIFFERS: ' + show(other['stopLevel'])} by "
                         "GET /positions)")  # fmt: skip
        else:
            ok &= at_broker.stop_level == stop
            parts.append(f"stop {stop} (not shown by the API; the adapter checked the amendment)")
        shown, level = _shown_target(report.position if report is not None else None, self.side, self.open_level)
        if shown:
            same = level == target
            ok &= same
            parts.append(f"target {target} ({'confirmed' if same else 'DIFFERS: ' + show(level)} by GET /positions)")
        else:
            parts.append(f"target {target} (not shown by the API: check it in the Capital.com app)")
        return ok, ", ".join(parts)

    async def _c5d_step(self, before: TargetFieldsReport | None) -> None:
        engine = self.engine
        at_broker = await cli._find_position(engine, self.deal_id)
        if at_broker is None or at_broker.stop_level is None or self.target_now is None:
            self.info(6, "C5d skipped: no position with a stop and a known target")
            self.finding("C5d", UNKNOWN, "nie sprawdzono (brak pozycji ze stopem i znanym celem)")
            return
        stop, target = at_broker.stop_level, self.target_now
        set_stop_error = None
        try:
            await engine.probe(engine.raw_broker.set_stop, self.deal_id, stop)
        except Exception as exc:
            set_stop_error = self.error(exc)
        after = None
        try:
            after = await self._read_fields()
        except Exception as exc:
            self.info(6, f"read after set_stop failed ({self.error(exc)})")
        if after is not None:
            self._print_fields(6, "after set_stop: GET /positions/{dealId}", after.position)
        was_shown, _ = _shown_target(before.position if before is not None else None, self.side, self.open_level)
        shown, level = _shown_target(after.position if after is not None else None, self.side, self.open_level)
        keys_now = after is not None and after.position.found and bool(after.position.target)
        if set_stop_error is not None:
            kept = "unknown"
            self.finding("C5d", UNKNOWN, f"set_stop z samym stopem nieudane ({set_stop_error})")
        elif shown and level == target:
            kept = "kept"
            self.finding("C5d", CONFIRMED, "PUT z samym stopLevel ZACHOWAŁ cel",
                         [f"cel po zmianie stopu: {level} (przed: {target})"])  # fmt: skip
        elif (shown and level is None) or (was_shown and not keys_now and after is not None and after.position.found):
            kept = "removed"
            self.finding("C5d", DIVERGENT, "PUT z samym stopLevel SKASOWAŁ cel",
                         ["silnik dla pasm z celem i tak zawsze wysyła stop razem z celem (set_protection)"])  # fmt: skip
        elif shown:
            kept = f"changed to {level}"
            self.finding("C5d", DIVERGENT, f"PUT z samym stopLevel ZMIENIŁ cel z {target} na {level}")
        else:
            kept = "not shown by the API"
            self.finding("C5d", UNKNOWN, "API nie pokazuje celu: sprawdź w aplikacji (historia: EDIT_STOP_AND_LIMIT), "
                                         "czy po zmianie stopu cel został")  # fmt: skip
        restored, text = False, "not attempted"
        try:
            await engine.probe(engine.raw_broker.set_protection, self.deal_id, stop, target)
        except Exception as exc:
            text = f"failed ({self.error(exc)})"
        else:
            restored, text = await self._verify(stop, target)
        ok = set_stop_error is None and restored
        first = f"set_stop with the stop only {stop}" + (f" failed ({set_stop_error})" if set_stop_error else "")
        self.step(6, ok, f"{first}; target after it: {kept}; set_protection restored: {text}")

    # --- [7] spłaszczenie ---

    async def _flatten_position(self) -> bool:
        engine, epic, journal, clock = self.engine, self.epic, self.journal, self.clock
        position = journal.current_position(epic)
        if position is not None and position.state in (PositionState.OPEN, PositionState.EXITING):
            await self.executor.flatten(position, FLATTEN_REASON)
        for _ in range(cli.SMOKE_SETTLE_ATTEMPTS):
            current = journal.current_position(epic)
            if current is None or current.state is PositionState.FLAT:
                break
            await clock.sleep(cli.SMOKE_SETTLE_PAUSE_S)
            await engine.reconcile_lane_once(epic)
            current = journal.current_position(epic)
            if current is not None and current.state is PositionState.EXITING:
                await self.executor.flatten(current, FLATTEN_REASON)
        current = journal.current_position(epic)
        return current is None or current.state is PositionState.FLAT

    async def _flatten(self) -> None:
        flat = await self._flatten_position()
        self.step(7, flat, "flatten through the engine (DELETE by dealId)")
        if not flat and await cli._still_at_broker(self.engine, self.deal_id) is not False:
            self.out(f"FAIL [7] LOUD: the probe position {LANE_LEFT_OPEN_HINT}")

    # --- [8] księga i przyczyna zamknięcia (C7) ---

    async def _booked(self) -> Any:
        engine, epic, journal = self.engine, self.epic, self.journal
        gone = await cli._find_position(engine, self.deal_id) is None
        trade = dict(journal.trades_by_deal(epic)).get(self.deal_id)
        currency = self.lane.loaded.cfg.currency
        costs = ""
        if trade is not None:
            costs = (f": gross {trade.gross_pnl}, spread {trade.spread_cost}, slippage {trade.slippage_penalty}, fees "
                     f"{trade.fees}, financing {trade.financing}, net {trade.net_pnl} {currency}, ledger exit "
                     f"{trade.exit_reason}")  # fmt: skip
        self.step(8, gone and trade is not None, f"position gone at the broker and the trade booked in {currency}{costs}")
        closure = await self._closure()
        sent_at = self._close_sent_at()
        # cel albo stop u brokera PRZED naszym zamknięciem: zamknięcie nie nasze (nasze dostało POSITION_NOT_FOUND);
        # MANUAL po wysłanym zamknięciu jest nasze bez porównywania zegarów (VPS i broker mogą się rozjechać)
        sent = sent_at is not None and (closure is None or closure.reason == "MANUAL" or closure.ts >= sent_at)
        if closure is not None:
            self.info(8, f"broker close reason: {closure.reason} at level {closure.exit_level} "
                         f"({'after our DELETE' if sent else 'without a close from the engine'})")  # fmt: skip
        self._judge_c7(closure, sent)
        self.gone = gone
        return trade

    def _close_sent_at(self) -> datetime | None:
        """Chwila pierwszego zamknięcia tej pozycji wysłanego przez silnik (dziennik `broker.close_position`)."""
        try:
            for e in self.journal.events("broker.close_position"):
                if json.loads(e.payload).get("deal_id") == self.deal_id:
                    return e.ts
        except Exception:
            pass
        return None

    async def _closure(self) -> ClosedDeal | None:
        since = (self.entry_ts or self.clock.now_utc()) - CLOSURE_LOOKBACK
        try:
            closed = await self.engine.probe(self.engine.raw_broker.closed_since, since)
        except Exception as exc:
            self.info(8, f"closure history unreadable ({self.error(exc)})")
            return None
        return next((c for c in closed if c.deal_id == self.deal_id), None)

    def _judge_c7(self, closure: ClosedDeal | None, sent: bool) -> None:
        untested = "przypisanie źródła TP do celu i SL do stopu nie sprawdzone w tej próbie (nie zadziałały)"
        if closure is None:
            self.finding("C7", UNKNOWN, "zamknięcia nie ma w historii aktywności w czasie próby (opóźnienie?)",
                         ["sprawdź w aplikacji historię zamknięcia i jej źródło"])  # fmt: skip
        elif sent and closure.reason == "MANUAL":
            self.finding("C7", CONFIRMED, "zamknięcie przez API (DELETE) ma w historii źródło USER (MANUAL)", [untested])
        elif not sent and closure.reason == "TARGET":
            self.finding("C7", CONFIRMED, "cel zamknął pozycję: źródło TP w historii (TARGET)")
        elif not sent and closure.reason == "STOP":
            self.finding("C7", CONFIRMED, "stop zamknął pozycję: źródło SL w historii (STOP)")
        elif sent and closure.reason in ("TARGET", "STOP"):
            self.finding("C7", UNKNOWN, f"wyścig: nasze zamknięcie i {closure.reason} w tej samej chwili")
        else:
            how = "po naszym DELETE" if sent else "bez zamknięcia z silnika"
            self.finding("C7", DIVERGENT, f"przyczyna zamknięcia {closure.reason} {how} (oczekiwano USER/MANUAL)")

    # --- [9] faza 2 w EUR ---

    async def _phase_two(self, trade: Any) -> None:
        engine, journal = self.engine, self.journal
        currency = self.lane.loaded.cfg.currency
        if not self.executor.foreign:
            self.info(9, f"{self.epic} is in the account currency: booked directly, no phase 2")
            return
        if trade is None:
            self.step(9, False, "phase 2 in EUR: no booked trade to settle")
            return
        await engine.refresh_fx()
        await engine.reconcile_lane_once(self.epic)  # rozliczenie fazy 2 (`_settle_foreign`) z kursu pary brokera
        fx = journal.trade_fx(self.deal_id)
        pending = self.deal_id in dict(journal.pnl_pending_deals())
        if fx is None or pending:
            self.step(9, False, f"phase 2 in EUR: no valid FX rate, the result {trade.net_pnl} {currency} stays "
                                "pending; every entry is blocked (PORTFOLIO_PNL_PENDING) until the engine settles it")
            return
        self.estimate_eur = fx.net_eur
        self.step(9, fx.source == ESTIMATED_FX, f"phase 2 in EUR: net {trade.net_pnl} {currency} -> {fx.net_eur} "
                                                f"EUR ({fx.source}, rate {fx.rate})")  # fmt: skip

    # --- [10] drugie zamknięcie ---

    async def _second_close(self) -> None:
        engine = self.engine
        if not self.gone:
            self.info(10, "skipped: position not confirmed gone (steps 7/8 failed); the engine's reconciliation handles"
                          " it")  # fmt: skip
            return
        outcome, ok = "accepted (!)", False
        try:
            await engine.probe(engine.raw_broker.close_position, self.deal_id)
        except BrokerRejected as exc:
            outcome, ok = f"BrokerRejected({show(self.scrub(exc.reason))})", exc.reason == POSITION_NOT_FOUND
        except Exception as exc:
            outcome = self.error(exc)
        if ok:
            self.step(10, True, "second close of the probe position rejected with POSITION_NOT_FOUND")
            return
        leftovers = [p for p in await cli._positions(engine) if p.epic == self.epic]
        engine.kill.soft("SMOKE_SECOND_CLOSE_UNEXPECTED")
        engine.alerts.alert("ERROR", f"SMOKE_SECOND_CLOSE_UNEXPECTED ({self.epic}): {outcome}; positions on "
                                     f"{self.epic}: {len(leftovers)}")  # fmt: skip
        self.out(f"FAIL [10] LOUD: a second close gave {outcome} instead of POSITION_NOT_FOUND; the emergency path "
                 f"relies on it. Positions on {self.epic} now: {len(leftovers)}")  # fmt: skip
        self.results.append(False)

    # --- [11] rozliczenie u brokera (C8) ---

    async def _settlement(self) -> None:
        rows = await self._transactions()
        if rows is not None:
            for r in rows:
                when = r.ts.isoformat() if r.ts is not None else "?"
                self.info(11, f"transaction {show(r.kind)} ({show(r.note)}): {show(r.amount)} {show(r.currency)} at "
                              f"{when} ({show(r.instrument)})")  # fmt: skip
            if not rows:
                self.info(11, "no rows in GET /history/transactions for the probe window")
        after = await self._balance()
        details = []
        if self.balance_before is not None and after is not None:
            details.append(f"saldo przed {self.balance_before} {ACCOUNT_CURRENCY}, po {after} {ACCOUNT_CURRENCY}, "
                           f"różnica {after - self.balance_before} (inne ruchy w tym czasie też by tu były)")
            self.info(11, f"account balance before {self.balance_before}, after {after}, difference "
                          f"{after - self.balance_before} {ACCOUNT_CURRENCY}")  # fmt: skip
        if self.position_other:
            details.append("GET /positions/{dealId} w trakcie: "
                           + ", ".join(f"{k}={show(self.position_other[k])}" for k in ("currency", "upl")
                                       if k in self.position_other))
        self._judge_c8(rows, details)

    async def _transactions(self) -> list[SettlementRow] | None:
        fn = getattr(self.engine.raw_broker, "diagnose_transactions", None)
        if fn is None:
            self.info(11, "the adapter has no read of the transaction history")
            return None
        since = (self.entry_ts or self.clock.now_utc()) - CLOSURE_LOOKBACK
        rows: list[SettlementRow] | None = None
        for attempt in range(TX_ATTEMPTS):
            if attempt:
                await self.clock.sleep(TX_PAUSE_S)
            try:
                rows = await self.engine.probe(fn, since, self.clock.now_utc())
            except Exception as exc:
                self.info(11, f"transaction history unreadable ({self.error(exc)})")
                continue
            if any(r.kind == "TRADE" for r in rows):
                break
        return rows

    def _judge_c8(self, rows: list[SettlementRow] | None, details: list[str]) -> None:
        lane_currency = self.lane.loaded.cfg.currency
        trades = [r for r in rows or [] if r.kind == "TRADE"]
        fx_rows = sorted({r.kind for r in rows or [] if r.kind in ("CONVERSION", "FX_COMMISSION")})
        if fx_rows:
            details.append(f"wiersze przewalutowania: {', '.join(fx_rows)}")
        if rows is None:
            self.finding("C8", UNKNOWN, "historii transakcji nie odczytano", details)
        elif not trades:
            self.finding("C8", UNKNOWN, "brak wiersza TRADE w historii transakcji w czasie próby (opóźnienie?); "
                                        "sprawdź w aplikacji walutę i kwotę wyniku", details)  # fmt: skip
        elif len(trades) > 1:
            self.finding("C8", UNKNOWN, f"{len(trades)} wiersze TRADE w oknie próby: bez referencji nie da się "
                                        "wskazać naszego", details)  # fmt: skip
        else:
            r = trades[0]
            estimate = (f"; szacunek ESTIMATED_FX {self.estimate_eur} {ACCOUNT_CURRENCY}"
                        + (f", różnica {r.amount - self.estimate_eur}" if r.amount is not None else "")
                        if self.estimate_eur is not None else "")  # fmt: skip
            if r.currency == ACCOUNT_CURRENCY:
                self.finding("C8", CONFIRMED, f"wynik rozliczony w {ACCOUNT_CURRENCY}: {show(r.amount)} "
                                              f"{ACCOUNT_CURRENCY}{estimate}", details)  # fmt: skip
            elif r.currency == lane_currency:
                self.finding("C8", DIVERGENT, f"wynik rozliczony w walucie instrumentu: {show(r.amount)} "
                                              f"{lane_currency} (nie w {ACCOUNT_CURRENCY}){estimate}", details)  # fmt: skip
            else:
                self.finding("C8", DIVERGENT, f"wynik w walucie {show(r.currency)}: {show(r.amount)}{estimate}",
                             details)  # fmt: skip


async def smoke_lane(engine: Engine, epic: str, out: Callable[[str], None] = print, hold_s: int | None = None) -> int:
    """Próba akceptacyjna pasma `epic` (patrz docstring modułu); kod wyjścia 0 tylko, gdy każdy krok PASS przeszedł.

    `hold_s`: None → `LANE_HOLD_DEFAULT_S`; liczba całkowita 0-600 (mniej niż `LANE_HOLD_MIN_S` jest podnoszone)."""
    if hold_s is None:
        hold_s = LANE_HOLD_DEFAULT_S
    if isinstance(hold_s, bool) or not isinstance(hold_s, int) or not 0 <= hold_s <= cli.SMOKE_HOLD_MAX_S:
        raise ValueError(f"hold_s: expected an integer from 0 to {cli.SMOKE_HOLD_MAX_S}, got {hold_s!r}")
    return await _Probe(engine, epic, out).run(hold_s)
