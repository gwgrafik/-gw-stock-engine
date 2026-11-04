"""Uzgadnianie per instrument (projekt pasm, zadanie 8; ustalenia A1 i A2, sekcja 5): dwa obiekty `Reconciler` na jednym
dzienniku nie mogą sobie przeszkadzać.

Pod lupą:
- znaczniki `closed_marker` i `mismatch_since` są per instrument; pasmo DE40 v1 (`legacy_keys=True`) zachowuje
  dotychczasowe nazwy kluczy (złote ślady), inne pasma piszą `<nazwa>:<epic>`;
- zamknięcie innego instrumentu zaksięgowane później nie przesuwa początku historii naszej pozycji (A1);
- rozbieżność oczekująca jednego pasma nie jest czyszczona przebiegiem drugiego, a MISMATCH_TIMEOUT każdego pasma liczy
  się od jego własnego pierwszego wykrycia (A2);
- pozycja na instrumencie bez pasma jest zgłaszana w `unmanaged`: nigdy zamykana, przyjmowana ani zapisywana;
- pozycja pasma z celem bez celu u brokera jest zgłaszana w `untargeted` (sekcja 3.3): bez kill switcha i bez BLOCKED.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import gwstock.reconcile as reconcile_module
from gwstock.journal import OrderRecord, PositionRecord
from gwstock.orders.manager import HISTORY_MARGIN
from gwstock.reconcile import Reconciler, history_start, meta_key
from gwstock.types import OpenPosition, OrderState, PositionState, Side, TargetedOpenPosition
from tests.conftest import RecordingBooker, RecordingKillSink

O = OrderState
P = PositionState

T0 = datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc)  # = clock.now_utc() na starcie fixture `clock`
SECONDS = timedelta(seconds=1)
LANES = ("DE40", "US500")
LEVEL = {"DE40": Decimal("25782.3"), "US500": Decimal("6000.5")}
SIZE = {"DE40": Decimal("0.007"), "US500": Decimal("0.01")}
READ_ONLY = {"list_positions", "list_working_orders", "closed_since"}


@pytest.fixture(autouse=True)
def broker_is_only_read(fake):
    """Po każdym teście: uzgadnianie wywołało u brokera wyłącznie odczyty."""
    yield
    assert set(fake.calls) <= READ_ONLY, f"reconciler called a non-read broker method: {fake.calls}"


class Lane:
    """Uzgadnianie jednego pasma z własnym kill switchem i księgującym (żeby było widać, które pasmo co zrobiło)."""

    def __init__(self, journal, fake, clock, risk_cfg, epic, *, legacy_keys, lanes=LANES, targeted=False):
        self.epic = epic
        self.kill = RecordingKillSink()
        self.booker = RecordingBooker()
        self.rec = Reconciler(
            journal, fake, clock, self.kill, risk_cfg, epic, booker=self.booker, legacy_keys=legacy_keys,
            lane_epics=lanes, targeted=targeted,
        )  # fmt: skip

    async def run(self):
        return await self.rec.run_once()


@pytest.fixture
def de40(journal, fake, clock, risk_cfg):
    return Lane(journal, fake, clock, risk_cfg, "DE40", legacy_keys=True)


@pytest.fixture
def us500(journal, fake, clock, risk_cfg):
    return Lane(journal, fake, clock, risk_cfg, "US500", legacy_keys=False)


def held(epic, state=P.OPEN, deal_id=None, *, opened_at=T0):
    level = LEVEL[epic]
    return PositionRecord(epic, state, deal_id or f"DEAL-{epic}", Side.BUY, SIZE[epic], level, level - 30, opened_at)


def at_broker(epic, deal_id=None, *, opened_at=T0, stop=True, cls=OpenPosition, **extra):
    level = LEVEL.get(epic, Decimal("21000"))
    size = SIZE.get(epic, Decimal("1"))
    return cls(deal_id or f"DEAL-{epic}", epic, Side.BUY, size, level, level - 30 if stop else None, opened_at, **extra)


def put(fake, *positions):
    for p in positions:
        fake.positions[p.deal_id] = p


def events(journal, source="reconcile.event"):
    return [json.loads(e.payload) for e in journal.events(source)]


def has_note(result, code):
    return any(note.split()[0] == code for note in result.notes)


# --- klucze --------------------------------------------------------------------------------------------------------


def test_meta_key_keeps_the_legacy_name_and_suffixes_every_other_lane():
    assert meta_key("closed_marker", "DE40", legacy_keys=True) == "closed_marker"
    assert meta_key("mismatch_since", "DE40", legacy_keys=True) == "mismatch_since"
    assert meta_key("closed_marker", "US500", legacy_keys=False) == "closed_marker:US500"
    assert meta_key("mismatch_since", "US500", legacy_keys=False) == "mismatch_since:US500"
    # stałe modułu to nazwy dotychczasowe (dziennik DE40 na VPS i złote ślady)
    assert (reconcile_module.META_CLOSED_MARKER, reconcile_module.META_MISMATCH_SINCE) == (
        "closed_marker", "mismatch_since"
    )  # fmt: skip


def test_the_single_lane_default_keeps_the_legacy_keys(journal, fake, clock, kill_sink, risk_cfg):
    # dzisiejsze złożenie silnika (`app._build_reconciler`) nie podaje `legacy_keys`: DE40 v1 bajt w bajt
    assert Reconciler(journal, fake, clock, kill_sink, risk_cfg, "DE40").legacy_keys is True


def test_history_start_reads_the_marker_of_the_positions_instrument(journal):
    journal.set_meta("closed_marker", (T0 - 3600 * SECONDS).isoformat())  # DE40 (klucz dotychczasowy)
    journal.set_meta("closed_marker:US500", (T0 + 3600 * SECONDS).isoformat())

    assert history_start(journal, held("DE40", opened_at=None), legacy_keys=True) == T0 - 3600 * SECONDS - HISTORY_MARGIN
    assert history_start(journal, held("US500", opened_at=None), legacy_keys=False) == (
        T0 + 3600 * SECONDS - HISTORY_MARGIN
    )
    # zamknięcie US500 zaksięgowane później nie przesuwa początku historii DE40 (A1)
    assert history_start(journal, held("DE40"), legacy_keys=True) == T0 - HISTORY_MARGIN


def test_history_start_needs_an_explicit_key_choice(journal):
    with pytest.raises(TypeError):
        history_start(journal, held("DE40"))  # type: ignore[call-arg]


# --- A1: znacznik zamknięć per instrument ---------------------------------------------------------------------------


async def test_a_later_us500_closure_does_not_hide_the_de40_closure(journal, fake, clock, de40, us500):
    # Restart po przerwie: DE40 wybity stopem o 08:30, US500 o 10:30; US500 uzgodnione (i zaksięgowane) pierwsze.
    journal.upsert_position(held("DE40"))
    journal.upsert_position(held("US500"))
    put(fake, at_broker("DE40"), at_broker("US500"))
    clock.advance(3600)
    fake.trigger_stop("DEAL-DE40", LEVEL["DE40"] - 30)
    clock.advance(7200)
    fake.trigger_stop("DEAL-US500", LEVEL["US500"] - 30)
    clock.advance(60)

    first = await us500.run()
    assert [c.deal_id for c in first.closed] == ["DEAL-US500"]
    assert journal.get_meta("closed_marker:US500") == (T0 + 3 * 3600 * SECONDS).isoformat()
    assert journal.get_meta("closed_marker") is None  # klucz DE40 nietknięty

    second = await de40.run()

    assert [c.deal_id for c in second.closed] == ["DEAL-DE40"] and not second.blocked
    assert journal.current_position("DE40") == PositionRecord("DE40", P.FLAT)
    assert journal.current_position("US500") == PositionRecord("US500", P.FLAT)
    assert [d.deal_id for _, d in de40.booker.calls] == ["DEAL-DE40"]
    assert [d.deal_id for _, d in us500.booker.calls] == ["DEAL-US500"]
    assert journal.get_meta("closed_marker") == (T0 + 3600 * SECONDS).isoformat()
    assert de40.kill.calls == [] and us500.kill.calls == []
    # odczyt historii DE40 od jego własnej dolnej granicy (otwarcie), nie od zamknięcia US500
    assert events(journal, "broker.closed_since")[-1]["since"] == (T0 - HISTORY_MARGIN).isoformat()


async def test_the_de40_marker_does_not_move_the_us500_history_start(journal, fake, clock, de40, us500):
    journal.upsert_position(held("DE40", opened_at=T0 + 7200 * SECONDS))
    journal.upsert_position(held("US500"))
    put(fake, at_broker("DE40", opened_at=T0 + 7200 * SECONDS), at_broker("US500"))
    clock.advance(3600)
    fake.trigger_stop("DEAL-US500", LEVEL["US500"] - 30)
    clock.advance(7200)
    fake.trigger_stop("DEAL-DE40", LEVEL["DE40"] - 30)
    clock.advance(60)

    assert [c.deal_id for c in (await de40.run()).closed] == ["DEAL-DE40"]
    assert journal.get_meta("closed_marker") == (T0 + 3 * 3600 * SECONDS).isoformat()

    result = await us500.run()

    assert [c.deal_id for c in result.closed] == ["DEAL-US500"]
    assert journal.current_position("US500") == PositionRecord("US500", P.FLAT)
    assert journal.get_meta("closed_marker:US500") == (T0 + 3600 * SECONDS).isoformat()


# --- A2: znacznik rozbieżności per instrument -----------------------------------------------------------------------


async def test_a_clean_de40_pass_does_not_clear_the_us500_mismatch(journal, fake, clock, de40, us500, risk_cfg):
    journal.upsert_position(held("US500"))  # US500 zniknął u brokera, historia pusta: rozbieżność oczekująca

    await us500.run()
    assert journal.get_meta("mismatch_since:US500") == T0.isoformat()
    tolerance = risk_cfg.mismatch_tolerance_s
    for _ in range(3):  # przebiegi przeplatane jak w takcie silnika
        clock.advance(tolerance // 2)
        assert not (await de40.run()).blocked
        assert journal.get_meta("mismatch_since:US500") == T0.isoformat()
        await us500.run()

    assert us500.kill.calls == ["MISMATCH_TIMEOUT"] and de40.kill.calls == []
    assert journal.get_meta("mismatch_since") is None  # klucz DE40 nietknięty


async def test_mismatch_timeouts_of_two_lanes_run_independently(journal, fake, clock, de40, us500, risk_cfg):
    tolerance = risk_cfg.mismatch_tolerance_s
    journal.upsert_position(held("US500"))
    await us500.run()  # US500: rozbieżność od T0
    clock.advance(tolerance // 2)
    journal.upsert_position(held("DE40"))
    await de40.run()  # DE40: rozbieżność od T0 + tolerancja/2
    assert journal.get_meta("mismatch_since") == (T0 + tolerance // 2 * SECONDS).isoformat()
    assert journal.get_meta("mismatch_since:US500") == T0.isoformat()

    clock.advance(tolerance // 2 + 1)
    await de40.run()
    await us500.run()

    assert us500.kill.calls == ["MISMATCH_TIMEOUT"] and de40.kill.calls == []  # DE40 jeszcze w tolerancji
    clock.advance(tolerance // 2)
    await de40.run()
    assert de40.kill.calls == ["MISMATCH_TIMEOUT"]


async def test_resolving_one_lane_clears_only_its_own_mismatch(journal, fake, clock, de40, us500):
    journal.upsert_position(held("DE40"))
    journal.upsert_position(held("US500"))
    await de40.run()
    await us500.run()
    assert journal.get_meta("mismatch_since") == T0.isoformat()
    assert journal.get_meta("mismatch_since:US500") == T0.isoformat()
    put(fake, at_broker("US500"))  # US500 znów zgodny z brokerem

    assert not (await us500.run()).blocked

    assert not journal.get_meta("mismatch_since:US500")
    assert journal.get_meta("mismatch_since") == T0.isoformat()


# --- sekcja 5.1: pozycja na instrumencie bez pasma -------------------------------------------------------------------


async def test_a_position_on_an_instrument_without_a_lane_is_reported_and_never_touched(journal, fake, de40, us500):
    owner = at_broker("US100", "DEAL-OWNER", stop=False)
    put(fake, at_broker("DE40"), at_broker("US500"), owner)
    journal.upsert_position(held("DE40"))
    journal.upsert_position(held("US500"))

    first = await de40.run()
    second = await us500.run()

    for result in (first, second):
        assert result.unmanaged == [owner] and not result.blocked
        assert result.unprotected == []  # obca pozycja bez stopu nie trafia do ochrony (stop ustawiałby wykonawca)
    assert fake.positions["DEAL-OWNER"] == owner and journal.current_position("US100") is None
    assert de40.kill.calls == [] and us500.kill.calls == []
    assert de40.booker.calls == [] and us500.booker.calls == []
    assert journal.current_position("DE40") == held("DE40") and journal.current_position("US500") == held("US500")


async def test_a_position_on_another_lane_is_not_unmanaged(journal, fake, de40):
    put(fake, at_broker("US500"))  # pozycja pasma US500: uzgadnia ją pasmo US500, nie DE40

    result = await de40.run()

    assert result.unmanaged == [] and not result.blocked and de40.kill.calls == []


async def test_without_a_lane_list_every_other_instrument_is_unmanaged(journal, fake, reconciler, kill_sink):
    # jedno pasmo (dzisiejsze złożenie DE40): każda pozycja na innym instrumencie jest bez pasma
    other = at_broker("US500")
    put(fake, other)

    result = await reconciler.run_once()

    assert result.unmanaged == [other] and result.notes == [] and kill_sink.calls == []
    assert [e.source for e in journal.events()] == ["broker.list_positions", "broker.list_working_orders"]


async def test_an_unknown_entry_never_adopts_a_matching_position_on_an_instrument_without_a_lane(journal, fake, de40):
    oid = "gws-20261102-DE40-1-entry"
    journal.insert_order(OrderRecord(oid, "DE40", Side.BUY, SIZE["DE40"], Decimal("30"), O.UNKNOWN, None, None, T0, T0))
    journal.upsert_position(PositionRecord("DE40", P.ENTERING))
    lookalike = OpenPosition("DEAL-X", "US100", Side.BUY, SIZE["DE40"], LEVEL["DE40"], LEVEL["DE40"] - 30, T0)
    put(fake, lookalike)

    result = await de40.run()

    assert result.unmanaged == [lookalike] and has_note(result, "UNKNOWN_NOT_FOUND")
    assert journal.get_order(oid).state is O.UNKNOWN and journal.current_position("DE40").state is P.ENTERING
    assert fake.positions == {"DEAL-X": lookalike}


async def test_a_skipped_pass_reports_nothing_unmanaged(journal, fake, clock, kill_sink, risk_cfg):
    put(fake, at_broker("US100", "DEAL-OWNER"))
    gated = Reconciler(journal, fake, clock, kill_sink, risk_cfg, "DE40", entry_gate=lambda: True)

    result = await gated.run_once()

    assert result.skipped and result.unmanaged == []


def test_the_lane_list_must_contain_the_reconcilers_own_instrument(journal, fake, clock, kill_sink, risk_cfg):
    with pytest.raises(ValueError, match="lane_epics"):
        Reconciler(journal, fake, clock, kill_sink, risk_cfg, "US500", legacy_keys=False, lane_epics=["DE40"])


# --- sekcja 3.3: pozycja pasma z celem bez celu u brokera ------------------------------------------------------------


async def test_a_targeted_lane_reports_its_position_without_a_target(journal, fake, clock, risk_cfg):
    lane = Lane(journal, fake, clock, risk_cfg, "US500", legacy_keys=False, targeted=True)
    bare = at_broker("US500")
    put(fake, bare)
    journal.upsert_position(held("US500"))

    result = await lane.run()

    assert result.untargeted == [bare] and has_note(result, "UNTARGETED")
    assert result.unprotected == [] and not result.blocked and lane.kill.calls == []  # cel nie jest ochroną
    assert journal.current_position("US500").state is P.OPEN
    assert [e["event"] for e in events(journal)] == ["UNTARGETED"]


async def test_a_targeted_lane_with_its_target_at_the_broker_reports_nothing(journal, fake, clock, risk_cfg):
    lane = Lane(journal, fake, clock, risk_cfg, "US500", legacy_keys=False, targeted=True)
    level = LEVEL["US500"]
    put(fake, at_broker("US500", cls=TargetedOpenPosition, target_level=level + 45, target_derived=True))
    journal.upsert_position(held("US500"))

    result = await lane.run()

    assert result.untargeted == [] and result.notes == [] and lane.kill.calls == []


async def test_a_targeted_position_without_stop_and_target_is_in_both_lists(journal, fake, clock, risk_cfg):
    lane = Lane(journal, fake, clock, risk_cfg, "US500", legacy_keys=False, targeted=True)
    bare = at_broker("US500", stop=False)
    put(fake, bare)
    journal.upsert_position(held("US500"))

    result = await lane.run()

    assert result.unprotected == [bare] and result.untargeted == [bare]


async def test_a_lane_without_a_target_never_reports_untargeted(journal, fake, de40):
    put(fake, at_broker("DE40"))
    journal.upsert_position(held("DE40"))

    result = await de40.run()

    assert result.untargeted == [] and result.notes == []


async def test_a_foreign_position_is_never_untargeted(journal, fake, clock, risk_cfg):
    # obca pozycja na instrumencie pasma: FOREIGN_POSITION jak dotąd; cel ustawia się tylko na NASZEJ pozycji
    lane = Lane(journal, fake, clock, risk_cfg, "US500", legacy_keys=False, targeted=True)
    put(fake, at_broker("US500", "DEAL-OWNER"))

    result = await lane.run()

    assert result.untargeted == [] and result.blocked and lane.kill.calls == ["FOREIGN_POSITION"]


# --- ochrona przed pomyłką w złożeniu: jedno pasmo na klucze dotychczasowe ---------------------------------------------


def test_only_one_instrument_may_use_the_legacy_keys_of_a_journal(journal, fake, clock, kill_sink, risk_cfg):
    Reconciler(journal, fake, clock, kill_sink, risk_cfg, "DE40")
    Reconciler(journal, fake, clock, kill_sink, risk_cfg, "DE40", legacy_keys=True)  # ponowne złożenie (np. _downgrade)
    with pytest.raises(ValueError, match="legacy"):
        Reconciler(journal, fake, clock, kill_sink, risk_cfg, "US500")  # domyślne klucze dotychczasowe: kolizja A1/A2
    with pytest.raises(ValueError, match="legacy"):
        Reconciler(journal, fake, clock, kill_sink, risk_cfg, "DE40", legacy_keys=False)  # dwa zestawy kluczy DE40
    Reconciler(journal, fake, clock, kill_sink, risk_cfg, "US500", legacy_keys=False)


def test_legacy_keys_must_be_a_bool(journal, fake, clock, kill_sink, risk_cfg):
    with pytest.raises(ValueError, match="legacy_keys"):
        Reconciler(journal, fake, clock, kill_sink, risk_cfg, "US500", legacy_keys="no")  # type: ignore[arg-type]
