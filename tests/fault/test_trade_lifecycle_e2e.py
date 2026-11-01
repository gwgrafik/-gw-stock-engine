"""Testy E2E cyklu życia transakcji (przegląd łączny właściciela: przyjęcie u brokera → timeout → restart → jedno
zaksięgowanie → aktualizacja limitów strat; scenariusze a–h i kryteria A–C).

Prawdziwe części: `Journal` (SQLite na dysku testu, WAL + synchronous=FULL), `OrderManager`, `Reconciler`, `Executor`,
`RiskGate`, `KillSwitch`, `CostModel`, a w scenariuszach dnia `Engine`; broker to `FakeBroker` (wstrzykiwanie awarii),
zegar `FakeClock`. Nic z kodu projektu nie jest atrapą.

Zasady pomiaru (`tests/fault/e2e_support.py`):
- każdy restart zamyka dziennik, otwiera go z dysku i buduje NOWE obiekty; sprawdza słabymi referencjami, że nic
  z poprzedniej instancji nie przeżyło, a nowe części wskazują tylko na nowy dziennik (kryterium B);
- broker jest ten sam przez wszystkie restarty: to, co przyjął lub zamknął, gdy silnika nie było, jest jego stanem
  (kryterium C);
- asercje czytają stan z dysku osobnym połączeniem tylko do odczytu (`read_disk`) i plik alarmów `data/alerts.log`;
- granice limitów strat pochodzą z `config/risk_v1.toml` (fixture `cfgs`), nigdy nie są tu przepisane;
- defekt w `src/` jest testem `xfail(strict=True)` z kodem BUG-E2E-n (opis i reprodukcja w raporcie zadania).
"""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import pytest

from gwstock.journal import JournalWriteError, PositionRecord
from gwstock.report.ledger import LEDGER_COLUMNS, export_csv
from gwstock.types import BrokerError, BrokerTimeout, KillLevel, OrderState, PositionState, Side
from tests.fault.e2e_support import (
    ASK,
    BID,
    CENT,
    CID,
    DAY,
    DEAL,
    EARLIER_DAY,
    EPIC,
    FILL,
    MID,
    RULES,
    SIZE,
    STOP_DISTANCE,
    STOP_LEVEL,
    T_ENTRY,
    T_FLATTEN,
    EngineRig,
    ProcessDied,
    Rig,
    alerts,
    assert_books_balance,
    calls_with_index,
    crash_after,
    crash_before,
    crash_image,
    crash_on_call,
    die,
    entry_candidate,
    entry_signal,
    install_faults,
    lag_history,
    make_world,
    manage,
    read_disk,
    reconcile,
)
from tests.fault.engine_sim import (  # noqa: F401  (fixtures symulowanego dnia silnika)
    DECISION_UTC,
    FLATTEN_UTC,
    at_time,
    berlin,
    broker,
    calls_at,
    cfgs,
    clock,
    config_dir,
    data_dir,
    day_rules,
    journal_path,
    make_engine,
    market,
    runtime,
    sim_root,
    stop_engine_at,
)
from tests.fault.test_lost_trade_regression import accept_then_stop_before_first_check

D = Decimal
O = OrderState
P = PositionState


# --- fixtures i kroki wspólne --------------------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path, cfgs):
    return make_world(tmp_path / "world", cfgs)


@pytest.fixture
def rig(world):
    r = Rig(world)
    yield r
    r.close()


async def enter(rig: Rig):
    return await rig.s.executor.enter(entry_signal(), RULES, DAY)


async def confirmed_entry(rig: Rig):
    rec = await enter(rig)
    assert rec is not None and rec.state is O.CONFIRMED and rec.deal_id == DEAL
    disk = read_disk(rig.path)
    assert disk.position == PositionRecord(EPIC, P.OPEN, DEAL, Side.BUY, SIZE, FILL, STOP_LEVEL, T_ENTRY)
    return rec


def any_alert(world, text: str) -> bool:
    return any(text in line for line in alerts(world))


def kill_reasons(disk) -> list[str]:
    """Przyczyny, które zmieniły stan kill switcha (audyt ENGAGE/ESCALATE/REASON), w kolejności."""
    return [r["reason"] for r in disk.kill_audit("ENGAGE", "ESCALATE", "REASON")]


# --- kryteria B i C: restart buduje wszystko od nowa, broker zostaje -------------------------------------------------


async def test_restart_closes_the_journal_rebuilds_every_object_and_keeps_the_broker(rig, world):
    await confirmed_entry(rig)
    rig.s.kill.soft("STOP_FILE")
    before = read_disk(rig.path)
    broker_before = world.broker
    rig.restart()  # w środku: stare połączenie nie działa, słabe referencje do starych obiektów martwe
    after = read_disk(rig.path)
    assert after == before  # restart niczego nie zapisuje i niczego nie gubi
    assert rig.restarts == 1 and world.broker is broker_before
    # nowy kill switch odtworzył poziom z dysku, nowa bramka widzi go i blokuje wejście
    assert rig.s.kill.level() is KillLevel.SOFT and rig.s.kill.reason() == "STOP_FILE"
    verdict = rig.s.gate.check_entry(entry_candidate(world), await world.broker.account())
    assert "KILL" in verdict.reasons and "DUPLICATE" in verdict.reasons
    # stan brokera nie zależy od restartu silnika: ta sama pozycja, to samo potwierdzenie referencji
    assert list(world.broker.positions) == [DEAL]
    assert (await world.broker.confirm("REF-1")).deal_id == DEAL
    assert world.broker.calls.count("open_position") == 1


# --- a: potwierdzenie zgubione, stop przed pierwszym sprawdzeniem ----------------------------------------------------


def assert_lost_trade_is_blocked(disk) -> None:
    order = disk.order(CID)
    assert order.state is O.UNKNOWN, order
    assert order.note.split()[0] == "UNMATCHED_CLOSED_DEAL", order.note
    assert disk.position == PositionRecord(EPIC, P.BLOCKED)  # nigdy FLAT: transakcja mogła się wykonać
    assert disk.trades == [] and disk.pnl == {} and disk.pnl_total == 0  # nic nie zaksięgowano po cichu
    notes = [p.get("event") for p in disk.payloads("reconcile.event")]
    assert "NOT_FOUND_BY_RECONCILER" not in notes and "POSITION_CLEARED" not in notes


async def test_a_lost_ack_and_stop_out_before_the_first_check_blocks_soft_kills_and_only_the_owner_clears(rig, world):
    accept_then_stop_before_first_check(world.broker)
    rec = await enter(rig)
    assert rec.state is O.UNKNOWN and rec.note.split()[0] == "UNMATCHED_CLOSED_DEAL"
    disk = read_disk(rig.path)
    assert_lost_trade_is_blocked(disk)
    assert disk.kill.level is KillLevel.SOFT and disk.kill.reason == "ENTRY_UNRESOLVED"
    assert "UNMATCHED_CLOSED_DEAL" in disk.codes("order.event") and "ENTRY_UNRESOLVED" in disk.codes()
    assert any_alert(world, "ENTRY_UNRESOLVED")

    # restart 1: wszystko z dysku; drugie wejście niemożliwe (bramka), uzgadnianie samo niczego nie rozstrzyga
    rig.restart()
    assert rig.s.kill.level() is KillLevel.SOFT
    assert await enter(rig) is None
    [block] = read_disk(rig.path).payloads("gate_block")
    assert {"KILL", "DUPLICATE", "ENTRIES_PER_DAY"} <= set(block["reasons"])
    for _ in range(3):
        result = await reconcile(rig)
        assert result.blocked and any(n.startswith("BLOCKED_AWAITING_OWNER") for n in result.notes)
    world.clock.advance(world.risk.mismatch_tolerance_s)
    await reconcile(rig)

    # restart 2: rozbieżność ponad tolerancję dołożyła przyczynę; nadal nic nie zaksięgowano
    rig.restart()
    disk = read_disk(rig.path)
    assert_lost_trade_is_blocked(disk)
    assert disk.kill.level is KillLevel.SOFT and kill_reasons(disk) == ["ENTRY_UNRESOLVED", "MISMATCH_TIMEOUT"]

    # zdjąć może tylko właściciel, jawnie i z nazwiskiem; silnik nigdy
    with pytest.raises(ValueError):
        rig.s.kill.clear("  ")
    assert read_disk(rig.path).kill.level is KillLevel.SOFT
    rig.s.kill.clear("owner")
    assert read_disk(rig.path).kill.level is KillLevel.NONE
    rig.restart()
    # bez decyzji właściciela o pozycji blokada wraca od razu, a wejścia blokuje dziennik
    await reconcile(rig)
    disk = read_disk(rig.path)
    assert_lost_trade_is_blocked(disk)
    assert disk.kill.level is KillLevel.SOFT and disk.kill.reason == "MISMATCH_TIMEOUT"
    assert world.broker.calls.count("open_position") == 1


@pytest.mark.parametrize(
    "crash",
    ["after_send", "at_check_1", "at_check_3", "before_history", "before_executor_kill"],
)
async def test_a_crash_at_every_point_of_the_unknown_resolution_still_ends_blocked_and_soft_killed(rig, world, crash):
    """Proces ginie w kolejnych punktach rozstrzygania UNKNOWN; po restarcie kończy je uzgadnianie (bez NOT_PLACED)."""
    fake = world.broker
    if crash == "after_send":
        fake.fail_next("open_position", ProcessDied(), applied=True)  # przyjęte u brokera, proces ginie
    else:
        accept_then_stop_before_first_check(fake)
        if crash == "at_check_1":
            fake.on_call("list_positions", die, once=True)
        elif crash == "at_check_3":
            crash_on_call(fake, "list_positions", 3)
        elif crash == "before_history":
            fake.on_call("closed_since", die, once=True)
        else:  # menedżer zapisał BLOCKED, wykonawca ginie, zanim włączy kill switch
            rig.s.kill.soft = die
    stop_out = {"done": False}

    def stop_at_first_list():
        if not stop_out["done"] and DEAL in fake.positions:
            stop_out["done"] = True
            fake.trigger_stop(DEAL, STOP_LEVEL)

    if crash == "after_send":
        fake.on_call("list_positions", stop_at_first_list)  # stop przed pierwszym sprawdzeniem, już po restarcie
    with pytest.raises(ProcessDied):
        await enter(rig)
    if crash == "at_check_3":
        del fake.list_positions  # tylko jedna śmierć: dalej zwykła metoda brokera
    rig.restart()
    disk = read_disk(rig.path)
    assert disk.order(CID).state in (O.SUBMITTING, O.UNKNOWN) and disk.trades == []
    for _ in range(8):  # 80 s: trzy puste odczyty, historia i tolerancja rozbieżności
        result = await reconcile(rig)
        assert result.blocked or result.notes[0].startswith(("UNKNOWN_NOT_FOUND", "RECOVERED_FROM_SUBMITTING"))
    rig.restart()
    disk = read_disk(rig.path)
    assert_lost_trade_is_blocked(disk)
    assert disk.kill.level is KillLevel.SOFT
    assert set(kill_reasons(disk)) & {"UNMATCHED_CLOSED_DEAL", "ENTRY_UNRESOLVED", "MISMATCH_TIMEOUT"}
    assert any_alert(world, "kill switch SOFT")
    assert fake.calls.count("open_position") == 1 and fake.positions == {}


# --- b: wejście potwierdzone, stop u brokera, jedno zaksięgowanie, limity strat --------------------------------------

STOP_AT = T_ENTRY + timedelta(hours=1)  # 10:06 w Berlinie


async def stop_out(rig: Rig) -> None:
    rig.world.at(STOP_AT)
    rig.world.broker.trigger_stop(DEAL, STOP_LEVEL)


async def flatten_at_1725(rig: Rig) -> None:
    rig.world.at(T_FLATTEN)
    await rig.s.executor.flatten(rig.s.journal.current_position(EPIC), "FORCED")


def assert_single_stop_booking(disk, seeds=None) -> None:
    [t] = disk.trades
    assert (t.side, t.size, t.entry_fill, t.exit_fill, t.exit_reason) == (Side.BUY, SIZE, FILL, STOP_LEVEL, "STOP")
    assert (t.decision_bid, t.decision_ask, t.exit_ts) == (BID, ASK, STOP_AT)
    assert [(deal, day) for deal, day, _ in disk.trade_rows] == [(DEAL, DAY.isoformat())]
    assert_books_balance(disk, seeds)
    assert disk.position == PositionRecord(EPIC, P.FLAT) and disk.order(CID).state is O.CONFIRMED
    assert disk.codes().count("TRADE_BOOKED") <= 1


@pytest.mark.parametrize("restart", ["none", "engine_down_when_the_stop_hits", "after_booking_before_flat", "after_flat"])
async def test_b_stop_out_at_the_broker_is_booked_exactly_once(rig, world, restart):
    await confirmed_entry(rig)
    if restart == "engine_down_when_the_stop_hits":
        rig.stop()  # proces nie żyje, gdy broker zamyka pozycję stopem
        await stop_out(rig)
        rig.start()
    else:
        await stop_out(rig)
    if restart == "after_booking_before_flat":
        crash_before(rig.s.journal, "upsert_position", lambda p: p.state is P.FLAT)
        with pytest.raises(ProcessDied):
            await reconcile(rig)
        disk = read_disk(rig.path)
        assert len(disk.trades) == 1 and disk.position.state is P.OPEN  # zaksięgowane, FLAT nie zdążył
        rig.restart()
    result = await reconcile(rig)
    if restart != "after_booking_before_flat":
        assert [c.deal_id for c in result.closed] == [DEAL]
    if restart == "after_flat":
        rig.restart()
    for _ in range(3):  # kolejne przebiegi (także po restarcie) niczego nie dopisują
        assert (await reconcile(rig)).closed == []
    disk = read_disk(rig.path)
    assert_single_stop_booking(disk)
    assert disk.pnl == {DAY: disk.trades[0].net_pnl} and disk.pnl_total == disk.trades[0].net_pnl < 0
    assert disk.codes().count("TRADE_BOOKED") == 1
    assert disk.kill.level is KillLevel.NONE  # −0,22 EUR jest daleko od limitów
    assert "close_position" not in world.broker.calls and world.broker.calls.count("open_position") == 1


LIMITS = {"daily": "max_daily_loss", "daily_2x": "max_daily_loss", "total": "max_total_drawdown"}
OFFSETS = {"below": CENT, "at": D("0"), "over": -CENT}  # o grosz mniej straty, dokładnie, o grosz więcej


def boundary(world, limit: str, where: str) -> Decimal:
    """Wynik (dnia albo łączny) dokładnie przy granicy z `config/risk_v1.toml`; DAILY_LOSS_2X to 2 × limit dnia (§9)."""
    value = getattr(world.risk, LIMITS[limit]) * (2 if limit == "daily_2x" else 1)
    return -value + OFFSETS[where]


def expected_kill(limit: str, where: str) -> tuple[KillLevel, str]:
    if where == "below":
        return {"daily": (KillLevel.NONE, ""), "daily_2x": (KillLevel.SOFT, "DAILY_LOSS"),
                "total": (KillLevel.NONE, "")}[limit]  # fmt: skip
    return {"daily": (KillLevel.SOFT, "DAILY_LOSS"), "daily_2x": (KillLevel.HARD, "DAILY_LOSS_2X"),
            "total": (KillLevel.HARD, "TOTAL_DRAWDOWN")}[limit]  # fmt: skip


async def calibrated_net(tmp_path, cfgs, close) -> Decimal:
    """Wynik netto tej samej transakcji w osobnym świecie bez zasiewu (koszty liczy prawdziwy CostModel)."""
    world = make_world(tmp_path / "calibration", cfgs)
    rig = Rig(world)
    try:
        await confirmed_entry(rig)
        await close(rig)
        if close is stop_out:
            await reconcile(rig)
        [trade] = read_disk(rig.path).trades
        return trade.net_pnl
    finally:
        rig.close()


def seed_for(world, limit: str, where: str, net: Decimal) -> tuple:
    """(dzień, kwota) wcześniejszego wyniku, po którym ta transakcja trafia dokładnie w `boundary`."""
    return (EARLIER_DAY if limit == "total" else DAY), boundary(world, limit, where) - net


@pytest.mark.parametrize("where", ["below", "at", "over"])
@pytest.mark.parametrize("limit", ["daily", "daily_2x", "total"])
async def test_b_loss_limit_kills_fire_at_the_exact_boundaries_after_the_reconciler_books(
    tmp_path, cfgs, limit, where
):
    net = await calibrated_net(tmp_path, cfgs, stop_out)
    world = make_world(tmp_path / "world", cfgs)
    rig = Rig(world)
    try:
        await confirmed_entry(rig)
        seed_day, seed = seed_for(world, limit, where, net)
        rig.s.journal.add_realized_pnl(seed_day, seed)  # wcześniejsze straty (po wejściu: bramka ich nie ocenia)
        await stop_out(rig)
        await reconcile(rig)
        rig.restart()
        disk = read_disk(rig.path)
        assert_single_stop_booking(disk, {seed_day: seed})
        reached = disk.pnl_total if limit == "total" else disk.pnl[DAY]
        assert reached == boundary(world, limit, where)
        level, reason = expected_kill(limit, where)
        assert disk.kill.level is level and disk.kill.reason == reason
        assert rig.s.kill.level() is level  # nowy kill switch odtworzył poziom z dysku
        triggered = {c for c in disk.codes() if c in ("DAILY_LOSS", "DAILY_LOSS_2X", "TOTAL_DRAWDOWN")}
        assert triggered == ({reason} | ({"DAILY_LOSS"} if reason == "DAILY_LOSS_2X" else set()) if reason else set())
        verdict = rig.s.gate.check_entry(entry_candidate(world), await world.broker.account())
        assert ("KILL" in verdict.reasons) is (level is not KillLevel.NONE)
        assert ("TOTAL_DRAWDOWN" in verdict.reasons) is (limit == "total" and where != "below")
        if reason:
            assert any(level.name in line and reason in line for line in alerts(world))
    finally:
        rig.close()


CRASH_CASES = [
    ("daily", "at", "reconciler", "after_record_trade"),
    ("daily", "over", "reconciler", "after_record_trade"),
    ("daily_2x", "at", "reconciler", "after_record_trade"),
    ("daily_2x", "over", "reconciler", "after_record_trade"),
    ("total", "at", "reconciler", "after_record_trade"),
    ("total", "over", "reconciler", "after_record_trade"),
    ("daily_2x", "at", "reconciler", "after_first_kill_write"),
    ("total", "at", "flatten", "after_record_trade"),
    ("daily", "at", "flatten", "after_record_trade"),
]


@pytest.mark.parametrize(("limit", "where", "path", "crash"), CRASH_CASES)
async def test_b_crash_between_booking_and_kill_escalation_keeps_entries_blocked_and_the_startup_check_reapplies(
    tmp_path, cfgs, limit, where, path, crash
):
    close = stop_out if path == "reconciler" else flatten_at_1725
    net = await calibrated_net(tmp_path, cfgs, close)
    world = make_world(tmp_path / "world", cfgs)
    rig = Rig(world)
    try:
        await confirmed_entry(rig)
        seed_day, seed = seed_for(world, limit, where, net)
        rig.s.journal.add_realized_pnl(seed_day, seed)
        if crash == "after_record_trade":
            crash_after(rig.s.journal, "record_trade")  # transakcja i wynik zatwierdzone, kill switch nie zdążył
        else:
            crash_on_call(rig.s.journal, "set_kill", 2)  # miękki zapisany, proces ginie przed zapisem twardego
        with pytest.raises(ProcessDied):
            if path == "reconciler":
                await stop_out(rig)
                await reconcile(rig)
            else:
                await flatten_at_1725(rig)
        rig.restart()
        disk = read_disk(rig.path)
        [trade] = disk.trades
        assert trade.net_pnl == net and disk.trade_rows[0][0] == DEAL
        assert_books_balance(disk, {seed_day: seed})  # zaksięgowane i policzone razem, dokładnie raz
        reached = disk.pnl_total if limit == "total" else disk.pnl[DAY]
        assert reached == boundary(world, limit, where)
        level, reason = expected_kill(limit, where)
        lost = KillLevel.NONE if crash == "after_record_trade" else KillLevel.SOFT
        assert disk.kill.level is lost and rig.s.kill.level() is lost  # poziom przepadł z procesem
        assert disk.position.state is (P.OPEN if path == "reconciler" else P.EXITING)  # FLAT nie zdążył

        # wejścia nadal zablokowane: bramka liczy DAILY_LOSS / TOTAL_DRAWDOWN z dziennika, nie z kill switcha
        verdict = rig.s.gate.check_entry(entry_candidate(world), await world.broker.account())
        assert ("TOTAL_DRAWDOWN" if limit == "total" else "DAILY_LOSS") in verdict.reasons and not verdict.passed

        # kontrola limitów przy starcie nakłada utracony poziom z liczników dziennika, idempotentnie
        rig.s.executor.enforce_loss_limits(DAY)
        disk = read_disk(rig.path)
        assert (disk.kill.level, disk.kill.reason) == (level, reason)
        changes = len(disk.kill_audit("ENGAGE", "ESCALATE", "REASON"))
        rig.s.executor.enforce_loss_limits(DAY)  # przy HARD miękka przyczyna trafia raz do audytu jako SUPPRESSED
        audit = len(read_disk(rig.path).kill_audit())
        rig.s.executor.enforce_loss_limits(DAY)
        disk = read_disk(rig.path)
        assert len(disk.kill_audit()) == audit and len(disk.kill_audit("ENGAGE", "ESCALATE", "REASON")) == changes
        assert (disk.kill.level, disk.kill.reason) == (level, reason)
        rig.restart()
        rig.s.executor.enforce_loss_limits(DAY)
        disk = read_disk(rig.path)
        assert (disk.kill.level, disk.kill.reason) == (level, reason)
        assert len(disk.kill_audit("ENGAGE", "ESCALATE", "REASON")) == changes  # po restarcie bez zmiany stanu

        # dokończenie przerwanego przebiegu: bez drugiej transakcji i bez drugiego doliczenia wyniku
        for _ in range(2):
            await manage(rig, flatten_at=T_FLATTEN)
        disk = read_disk(rig.path)
        assert len(disk.trades) == 1 and disk.position == PositionRecord(EPIC, P.FLAT)
        assert_books_balance(disk, {seed_day: seed})
        assert (disk.kill.level, disk.kill.reason) == (level, reason)
        assert world.broker.calls.count("close_position") == (0 if path == "reconciler" else 1)
    finally:
        rig.close()


# --- c: spłaszczenie o 17:25, timeout zamknięcia, historia spóźniona ---------------------------------------------------

LAG_S = 25  # zamknięcie widać w historii 25 s po fakcie: wykonawca czyta ją 3 razy w 6 s, uzgadnianie później
EXIT_MID = MID + 30  # o 17:25 rynek wyżej: wyjście BUY po bid = EXIT_MID − 1


def assert_single_forced_exit(disk, exit_ts=T_FLATTEN) -> None:
    [t] = disk.trades
    assert (t.exit_reason, t.exit_fill, t.exit_ts, t.entry_fill) == ("FORCED", EXIT_MID - 1, exit_ts, FILL)
    assert_books_balance(disk)
    assert disk.position == PositionRecord(EPIC, P.FLAT) and disk.order(CID).state is O.CONFIRMED
    assert disk.codes().count("TRADE_BOOKED") == 1


@pytest.mark.parametrize("restart", ["none", "after_close_applied", "after_await_history", "after_booking_before_flat"])
async def test_c_flatten_close_timeout_lists_positions_never_resends_and_the_lagging_closure_is_booked_once(
    rig, world, restart
):
    await confirmed_entry(rig)
    world.at(T_FLATTEN)
    world.market.mid = EXIT_MID
    lag_history(world.broker, world.clock, LAG_S)
    closes = calls_with_index(world.broker, "close_position")
    position = read_disk(rig.path).position
    if restart == "after_close_applied":
        world.broker.fail_next("close_position", ProcessDied(), applied=True)  # zamknięte u brokera, proces ginie
        with pytest.raises(ProcessDied):
            await rig.s.executor.flatten(position, "FORCED")
        disk = read_disk(rig.path)
        assert disk.position.state is P.EXITING and disk.meta[f"exit_intent:{DEAL}"] == "FORCED"  # przed wysyłką
        rig.restart()
    else:
        world.broker.fail_next("close_position", BrokerTimeout(), applied=True)
        assert await rig.s.executor.flatten(position, "FORCED") is None
        disk = read_disk(rig.path)
        assert disk.position.state is P.EXITING and disk.trades == []  # nigdy FLAT bez zaksięgowania
        # po timeoucie najpierw lista pozycji (pozycji nie ma), potem historia; żadnej drugiej wysyłki
        assert world.broker.calls[closes[0]:] == ["close_position", "list_positions"] + ["closed_since"] * 3
        assert "CLOSE_SENT_AWAIT_HISTORY" in disk.codes() and any_alert(world, "CLOSE_SENT_AWAIT_HISTORY")
        if restart == "after_await_history":
            rig.restart()
    if restart == "after_booking_before_flat":
        crash_before(rig.s.journal, "upsert_position", lambda p: p.state is P.FLAT)
        with pytest.raises(ProcessDied):
            for _ in range(6):
                await manage(rig)
        assert len(read_disk(rig.path).trades) == 1
        rig.restart()
    for _ in range(6):  # silnik: uzgadnianie co 10 s, ponowienie EXITING zawsze od listy pozycji
        await manage(rig)
    disk = read_disk(rig.path)
    assert_single_forced_exit(disk)
    assert len(closes) == 1  # jedno zamknięcie przez cały scenariusz, także po restarcie
    assert world.broker.positions == {} and world.broker.calls.count("open_position") == 1


async def test_c_close_timeout_not_applied_keeps_exiting_and_the_retry_closes_only_after_the_list_shows_it(rig, world):
    await confirmed_entry(rig)
    world.at(T_FLATTEN)
    world.market.mid = EXIT_MID
    closes = calls_with_index(world.broker, "close_position")
    world.broker.fail_next("close_position", BrokerTimeout(), applied=False)
    assert await rig.s.executor.flatten(read_disk(rig.path).position, "FORCED") is None
    disk = read_disk(rig.path)
    assert disk.position.state is P.EXITING and "FLATTEN_STILL_OPEN" in disk.codes()
    assert world.broker.calls[closes[0]:] == ["close_position", "list_positions"]  # pozycja jest: bez drugiej wysyłki
    rig.restart()
    await manage(rig)
    assert len(closes) == 2
    assert world.broker.calls[closes[1] - 1] == "list_positions"  # ponowienie dopiero po liście, która ją pokazała
    [*_, verified] = [p for p in read_disk(rig.path).payloads("broker.list_positions") if p.get("context") == "flatten"]
    assert [p["deal_id"] for p in verified["response"]] == [DEAL]
    await manage(rig)
    disk = read_disk(rig.path)
    assert_single_forced_exit(disk, exit_ts=T_FLATTEN + timedelta(seconds=10))  # zamknęło ponowienie po restarcie
    assert len(closes) == 2 and world.broker.positions == {}


# --- d: kill switch twardy w trakcie pozycji -------------------------------------------------------------------------

HARD_AT = T_ENTRY + timedelta(hours=2)


def hard_stop_file(world) -> None:
    (world.data_dir / "HARD_STOP").write_text("")  # plik operatora: kill switch twardy


@pytest.mark.parametrize("restart", ["after_hard", "mid_emergency_close"])
async def test_d_hard_kill_mid_position_emergency_flattens_and_books_across_restarts(rig, world, restart):
    await confirmed_entry(rig)
    world.at(HARD_AT)
    hard_stop_file(world)
    rig.s.kill.scan_files()
    assert read_disk(rig.path).kill.level is KillLevel.HARD
    rig.restart()
    assert rig.s.kill.level() is KillLevel.HARD  # z dysku
    closes = calls_with_index(world.broker, "close_position")
    if restart == "mid_emergency_close":
        world.broker.fail_next("close_position", ProcessDied(), applied=True)
        with pytest.raises(ProcessDied):
            await manage(rig)
        rig.restart()
    for _ in range(3):
        await manage(rig)
    disk = read_disk(rig.path)
    [t] = disk.trades
    assert t.exit_reason == "FORCED" and t.exit_ts == HARD_AT + timedelta(seconds=10)
    assert disk.meta[f"exit_intent:{DEAL}"] == "EMERGENCY"
    assert_books_balance(disk)
    assert disk.position == PositionRecord(EPIC, P.FLAT)
    assert (disk.kill.level, disk.kill.reason) == (KillLevel.HARD, "HARD_STOP_FILE")  # zostaje do decyzji właściciela
    assert len(closes) == 1 and world.broker.positions == {}


async def test_d_emergency_flatten_with_every_journal_write_failing_still_closes_and_is_booked_after_restart(
    rig, world
):
    await confirmed_entry(rig)
    world.at(HARD_AT)
    faulty = install_faults(rig.s.journal)
    faulty.fail_from = 1  # od teraz każdy zapis dziennika zawodzi (pełny dysk)
    hard_stop_file(world)
    rig.s.kill.scan_files()
    assert rig.s.kill.level() is KillLevel.HARD  # w pamięci; zapis stanu zawiódł
    closes = calls_with_index(world.broker, "close_position")
    await rig.s.executor.emergency_flatten()
    assert len(closes) == 1 and world.broker.positions == {}  # zamknięcie poszło mimo martwego dziennika
    disk = read_disk(rig.path)
    assert disk.position.state is P.OPEN and disk.trades == [] and disk.kill.level is KillLevel.NONE
    for text in ("NOT_PERSISTED", "BOOKING_FAILED", "EMERGENCY_FLATTEN_INCOMPLETE", "JOURNAL_WRITE_FAILED"):
        assert any_alert(world, text), text
    rig.restart()  # nowy proces, dysk znów przyjmuje zapisy
    rig.s.kill.scan_files()  # HARD_STOP nadal leży: HARD wraca i tym razem jest na dysku
    for _ in range(2):
        await manage(rig)
    disk = read_disk(rig.path)
    [t] = disk.trades
    assert t.exit_ts == HARD_AT and t.exit_fill == BID
    # zamiar wyjścia (`exit_intent`) nie trafił na dysk, więc księga widzi zamknięcie wywołaniem (obserwacja w raporcie)
    assert t.exit_reason == "MANUAL" and f"exit_intent:{DEAL}" not in disk.meta
    assert_books_balance(disk)
    assert disk.position == PositionRecord(EPIC, P.FLAT) and disk.kill.level is KillLevel.HARD
    assert len(closes) == 1


async def test_d_exits_are_never_blocked_by_the_gate_even_with_every_entry_check_failing(rig, world):
    await confirmed_entry(rig)
    rig.s.journal.add_realized_pnl(DAY, -2 * world.risk.max_daily_loss - CENT)
    rig.s.journal.add_realized_pnl(EARLIER_DAY, -world.risk.max_total_drawdown)
    rig.s.executor.enforce_loss_limits(DAY)
    (world.data_dir / "STOP").write_text("")
    rig.s.kill.scan_files()
    world.at(T_FLATTEN)
    rig.restart()
    verdict = rig.s.gate.check_entry(entry_candidate(world), await world.broker.account())
    blocked = {"KILL", "DAILY_LOSS", "TOTAL_DRAWDOWN", "OUTSIDE_WINDOW", "DUPLICATE", "ENTRIES_PER_DAY"}
    assert blocked <= set(verdict.reasons) and rig.s.kill.level() is KillLevel.HARD
    assert rig.s.gate.check_exit().passed
    closed = await rig.s.executor.flatten(read_disk(rig.path).position, "MANUAL")  # zwykłe wyjście, nie awaryjne
    assert closed is not None and closed.deal_id == DEAL
    disk = read_disk(rig.path)
    assert disk.position == PositionRecord(EPIC, P.FLAT) and len(disk.trades) == 1


# --- e: awaria zapisu dziennika w każdym punkcie zapisu `Executor.enter()` --------------------------------------------


async def probe_write_points(tmp_path, cfgs) -> list[str]:
    """Punkty zapisu wejścia odkryte liczeniem transakcji dziennika w udanym przebiegu (nic nie jest tu wpisane)."""
    world = make_world(tmp_path / "probe", cfgs)
    rig = Rig(world)
    try:
        faulty = install_faults(rig.s.journal)
        rec = await enter(rig)
        assert rec.state is O.CONFIRMED and rig.s.kill.level() is KillLevel.NONE
        return list(faulty.labels)
    finally:
        rig.close()


async def enter_with_write_failure(root, cfgs, n: int, label: str, mode: str, points: list[str]) -> list[str]:
    """Wejście z awarią zapisu nr `n` (`once`: tylko ten; `from_here_on`: ten i każdy następny); zwraca naruszenia."""
    from gwstock.orders.manager import EntryBlocked

    where = f"write {n} {label} [{mode}]"
    problems: list[str] = []

    def check(ok: bool, text: str) -> None:
        if not ok:
            problems.append(f"{where}: {text}")

    world = make_world(root, cfgs)
    rig = Rig(world)
    sends: list[tuple] = []

    def on_send() -> None:  # w chwili wysyłki: co już JEST na dysku
        disk = read_disk(rig.path)
        state = next((o.state for o in disk.orders if o.order_id == CID), None)
        sends.append((state, f"entry_ctx:{CID}" in disk.meta, disk.position))

    world.broker.on_call("open_position", on_send)
    try:
        faulty = install_faults(rig.s.journal)
        if mode == "once":
            faulty.fail_at = {n}
        else:
            faulty.fail_from = n
        await enter(rig)  # awaria dziennika nigdy nie wychodzi z `enter` wyjątkiem
        check(faulty.labels[: n - 1] == points[: n - 1], f"write sequence differs from the probe: {faulty.labels}")
        sent = len(sends)
        for state, ctx, position in sends:
            check(state is O.SUBMITTING, f"order sent while the journal row was {state}")
            check(ctx, "order sent without the entry context on disk")
            check(position is not None and position.state is P.ENTERING, f"order sent with position {position}")
        check(rig.s.kill.level() >= KillLevel.SOFT, "a failed journal write did not engage the kill switch")
        check(any_alert(world, "JOURNAL_WRITE_FAILED"), "no JOURNAL_WRITE_FAILED alert")
        if mode == "from_here_on":
            check(any_alert(world, "NOT_PERSISTED"), "the kill switch could not persist, but no NOT_PERSISTED alert")
        disk = read_disk(rig.path)
        if sent:
            states = {o.order_id: o.state for o in disk.orders}
            check(states.get(CID) in (O.SUBMITTING, O.CONFIRMED), f"the journal lost the sent order: {states}")
        else:
            check(world.broker.positions == {}, "a position exists although nothing was sent")
        if mode == "once":
            check(disk.kill.level >= KillLevel.SOFT, f"kill switch not persisted: {disk.kill}")

        rig.restart()  # nowy proces na zdrowym dysku
        for _ in range(4):
            await reconcile(rig)
        try:  # kolejna próba tego dnia (w oknie wejścia) nigdy nie daje drugiego zlecenia
            await enter(rig)
        except EntryBlocked:
            pass  # wiersz zlecenia pod tym numerem już jest: DUPLICATE_CLIENT_ID, kill switch ENTRY_EXCEPTION
        disk = read_disk(rig.path)
        check(world.broker.calls.count("open_position") <= 1, "a second order reached the broker")
        for p in world.broker.positions.values():
            check(
                disk.position is not None and disk.position.deal_id == p.deal_id
                and disk.position.state in (P.OPEN, P.EXITING),
                f"broker position {p.deal_id} unknown to the journal ({disk.position})",
            )  # fmt: skip
            owner = [o for o in disk.orders if o.deal_id == p.deal_id]
            check(len(owner) == 1 and owner[0].state is O.CONFIRMED, f"no confirmed order for {p.deal_id}: {owner}")
        if not world.broker.positions:
            check(all(o.state in (O.NOT_PLACED, O.REJECTED) for o in disk.orders), f"orders left open: {disk.orders}")
            check(disk.position is None or disk.position.state is P.FLAT, f"position {disk.position}")
        check(disk.trades == [] and disk.pnl == {}, "something was booked")
    finally:
        rig.close()
    return problems


@pytest.mark.parametrize("mode", ["once", "from_here_on"])
async def test_e_a_journal_write_failure_at_each_write_point_of_enter_never_sends_blind(tmp_path, cfgs, mode):
    points = await probe_write_points(tmp_path, cfgs)
    assert {"order:INTENT", "order:SUBMITTING", "order:CONFIRMED", "meta:entry_ctx"} <= set(points), points
    assert points.index("meta:entry_ctx") < points.index("order:INTENT") < points.index("order:SUBMITTING")
    problems: list[str] = []
    for n, label in enumerate(points, 1):
        problems += await enter_with_write_failure(tmp_path / f"{mode}-{n:02d}", cfgs, n, label, mode, points)
    assert problems == [], "\n".join(problems)


# --- BUG-E2E-1: wejście przyjęte przez uzgadnianie omija straże po wypełnieniu ------------------------------------------

OVERSIZE = D("5")  # broker wypełnia 0.035 zamiast 0.007: ryzyko 0.035 × 30 × 1 = 1.05 EUR > max_loss_per_trade


async def oversize_entry_via(rig, world, path: str) -> None:
    world.broker.fill_size_factor = OVERSIZE
    if path == "on_time_confirm":
        await enter(rig)
    elif path == "checks_incomplete":  # odpowiedź zgubiona, lista pozycji nieczytelna przez 30 s
        world.broker.fail_next("open_position", BrokerTimeout(), applied=True)
        for _ in range(3):
            world.broker.fail_next("list_positions", BrokerError("HTTP 502"))
        assert (await enter(rig)).state is O.UNKNOWN
    elif path == "restart_after_send":  # proces ginie zaraz po przyjęciu zlecenia
        world.broker.fail_next("open_position", ProcessDied(), applied=True)
        with pytest.raises(ProcessDied):
            await enter(rig)
        rig.restart()
    else:  # zapis CONFIRMED zawodzi po wypełnieniu (wejście zostaje uzgadnianiu)
        real = rig.s.journal.update_order
        failed = []

        def update_order(order_id, **fields):
            if fields.get("state") is O.CONFIRMED and not failed:  # jeden raz: zapis menedżera
                failed.append(order_id)
                raise JournalWriteError("disk I/O error (injected)")
            return real(order_id, **fields)

        rig.s.journal.update_order = update_order
        assert await enter(rig) is None
    for _ in range(3):  # takty silnika przed 17:25: uzgadnianie przyjmuje pozycję
        await manage(rig)


async def test_control_on_time_confirm_closes_the_oversize_fill_at_once(rig, world):
    await oversize_entry_via(rig, world, "on_time_confirm")
    disk = read_disk(rig.path)
    assert "SIZE_MISMATCH_RISK" in disk.codes() and disk.position.state is P.FLAT and len(disk.trades) == 1


@pytest.mark.parametrize("path", ["checks_incomplete", "restart_after_send", "journal_failure_after_send"])
async def test_bug_e2e_1_an_adopted_oversize_fill_is_closed_by_the_risk_guard(rig, world, path):
    closes = calls_with_index(world.broker, "close_position")
    await oversize_entry_via(rig, world, path)
    disk = read_disk(rig.path)
    assert disk.order(CID).state is O.CONFIRMED and disk.order(CID).note.startswith("ADOPTED_BY_RECONCILER")
    # jak przy potwierdzeniu na czas (spec §5 pkt 7, §9): straż ryzyka zamyka pozycję od razu
    assert "SIZE_MISMATCH_RISK" in disk.codes() and "FILL_GUARD_ADOPTED" in disk.codes()
    [t] = disk.trades
    assert t.size == SIZE * OVERSIZE and t.exit_reason == "FORCED"  # przyjęta z wielkością brokera
    assert disk.position == PositionRecord(EPIC, P.FLAT) and world.broker.positions == {} and len(closes) == 1
    assert disk.meta[f"fill_guarded:{DEAL}"] == "SIZE_MISMATCH_RISK"
    assert_books_balance(disk)


async def test_crash_after_adoption_before_the_fill_guard_still_closes_after_the_restart(rig, world):
    world.broker.fill_size_factor = OVERSIZE
    world.broker.fail_next("open_position", ProcessDied(), applied=True)
    with pytest.raises(ProcessDied):
        await enter(rig)
    rig.restart()
    # uzgadnianie przyjmuje pozycję (zapis CONFIRMED trafia na dysk), proces ginie przed strażą wypełnienia
    crash_after(rig.s.journal, "update_order", when=lambda _order_id, **f: f.get("state") is O.CONFIRMED)
    with pytest.raises(ProcessDied):
        await manage(rig)
    disk = read_disk(rig.path)
    assert disk.order(CID).state is O.CONFIRMED and disk.position.state is P.OPEN
    assert "SIZE_MISMATCH_RISK" not in disk.codes() and f"fill_guarded:{DEAL}" not in disk.meta
    rig.restart()
    closes = calls_with_index(world.broker, "close_position")
    await manage(rig)  # znacznik, nie wynik uzgadniania, decyduje: straż biegnie przy pierwszym przebiegu
    disk = read_disk(rig.path)
    assert "SIZE_MISMATCH_RISK" in disk.codes() and disk.position == PositionRecord(EPIC, P.FLAT)
    [t] = disk.trades
    assert t.size == SIZE * OVERSIZE and t.exit_reason == "FORCED" and len(closes) == 1
    assert_books_balance(disk)
    await manage(rig)
    assert len(read_disk(rig.path).trades) == 1 and len(closes) == 1


# --- f: atomowość księgowania na poziomie dziennika (kryterium A) -----------------------------------------------------


def booked_trade():
    """Transakcja ze stopem jak w scenariuszu b (wartości z księgi; tu liczy się tylko atomowość zapisu)."""
    from gwstock.journal import TradeRecord

    return TradeRecord(
        EPIC, Side.BUY, SIZE, BID, ASK, FILL, STOP_LEVEL, D("-0.20"), D("0.01"), D("0.01"), D("0.00"), D("0.00"),
        D("-0.22"), T_ENTRY, T_ENTRY, T_ENTRY, STOP_AT, 0, "STOP",
    )  # fmt: skip


@pytest.mark.parametrize(
    "statement",
    ["INSERT INTO trades", "SELECT net FROM realized_pnl", "INSERT INTO realized_pnl", "COMMIT"],
    ids=["trade_insert", "pnl_read", "pnl_write", "commit"],
)
async def test_f_record_trade_is_all_or_nothing_when_a_statement_fails_and_a_retry_books_both_once(
    tmp_path, statement
):
    from gwstock.journal import Journal

    path = tmp_path / "journal.sqlite"
    seed = D("-0.50")
    journal = Journal.open(str(path))
    journal.add_realized_pnl(DAY, seed)  # wcześniejszy wynik dnia
    faulty = install_faults(journal)
    faulty.fail_before(statement)  # po INSERT do trades (pnl_*, commit) albo zamiast niego
    with pytest.raises(JournalWriteError):
        journal.record_trade(booked_trade(), DEAL, DAY)
    assert faulty.rules == []  # awaria naprawdę wypadła w tej instrukcji
    journal.close()
    disk = read_disk(path)
    assert disk.trades == [] and disk.pnl == {DAY: seed}  # ani transakcji, ani wyniku

    journal = Journal.open(str(path))  # restart: ponowienie księgowania
    try:
        assert journal.record_trade(booked_trade(), DEAL, DAY) is True
        assert journal.record_trade(booked_trade(), DEAL, DAY) is False  # idempotentne po deal_id
    finally:
        journal.close()
    disk = read_disk(path)
    assert disk.trades == [booked_trade()]
    assert_books_balance(disk, {DAY: seed})


async def test_f_a_kill_9_image_taken_between_the_trade_insert_and_the_pnl_update_holds_neither(tmp_path):
    from gwstock.journal import Journal

    path = tmp_path / "live" / "journal.sqlite"
    path.parent.mkdir()
    journal = Journal.open(str(path))
    journal.add_realized_pnl(DAY, D("-0.50"))
    images = []

    def kill_9() -> None:  # obraz plików w chwili śmierci procesu, w środku otwartej transakcji
        images.append(crash_image(path, tmp_path / "image"))
        die()

    install_faults(journal).after("INSERT INTO trades", kill_9)
    with pytest.raises(ProcessDied):
        journal.record_trade(booked_trade(), DEAL, DAY)
    journal.close()
    [image] = images
    disk = read_disk(image)
    assert disk.trades == [] and disk.pnl == {DAY: D("-0.50")}  # po kill -9: ani transakcji, ani wyniku
    restarted = Journal.open(str(image))
    try:
        assert restarted.record_trade(booked_trade(), DEAL, DAY) is True
    finally:
        restarted.close()
    assert_books_balance(read_disk(image), {DAY: D("-0.50")})


# --- g: broker żyje własnym życiem, gdy silnika nie ma (kryterium C) ----------------------------------------------------


@pytest.mark.parametrize("while_down", ["accepted", "stopped_out", "closed_by_owner", "accepted_and_stopped_out"])
async def test_g_what_the_broker_did_while_the_engine_was_down_is_discovered_after_the_restart(rig, world, while_down):
    fake = world.broker
    if while_down.startswith("accepted"):
        fake.fail_next("open_position", ProcessDied(), applied=True)  # przyjęte, odpowiedź nigdy nie dotarła
        with pytest.raises(ProcessDied):
            await enter(rig)
        assert read_disk(rig.path).order(CID).state is O.SUBMITTING
    else:
        await confirmed_entry(rig)
    rig.stop()  # silnika nie ma: obiekty porzucone, broker działa dalej
    world.at(STOP_AT)
    if while_down in ("stopped_out", "accepted_and_stopped_out"):
        fake.trigger_stop(DEAL, STOP_LEVEL)
    elif while_down == "closed_by_owner":
        await fake.close_position(DEAL)  # właściciel w aplikacji brokera
    rig.start()
    for _ in range(4):
        await reconcile(rig)
    disk = read_disk(rig.path)
    order = disk.order(CID)
    if while_down == "accepted":
        assert order.state is O.CONFIRMED and order.deal_id == DEAL and order.note.split()[0] == "ADOPTED_BY_RECONCILER"
        assert disk.position.state is P.OPEN and disk.position.deal_id == DEAL and disk.trades == []
    elif while_down == "accepted_and_stopped_out":
        assert_lost_trade_is_blocked(disk)  # zamknięcie bez dopasowanego zlecenia: BLOCKED, nic nie zaksięgowane
        assert disk.kill.level is KillLevel.SOFT and "UNMATCHED_CLOSED_DEAL" in kill_reasons(disk)
        assert any_alert(world, "UNMATCHED_CLOSED_DEAL")
    else:
        [t] = disk.trades
        assert t.exit_reason == ("STOP" if while_down == "stopped_out" else "MANUAL") and t.exit_ts == STOP_AT
        assert disk.position == PositionRecord(EPIC, P.FLAT) and order.state is O.CONFIRMED
        assert_books_balance(disk)
    assert fake.calls.count("open_position") == 1


# --- h i kryterium A na poziomie silnika: prawdziwy `Engine`, dzień symulowany z `engine_sim` ----------------------------

ENGINE_STOP_AT = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)  # 11:00 w Berlinie: stop u brokera
ENGINE_CRASH_AT = datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)  # po wejściu 09:05, przed wyjściem 17:25


@pytest.fixture
def engine_rig(make_engine, journal_path):
    r = EngineRig(make_engine, journal_path)
    yield r
    r.close()


def ledger_rows(journal_file, out):
    from gwstock.journal import Journal

    journal = Journal.open_readonly(str(journal_file))
    try:
        assert export_csv(journal, out) == len(journal.trades())
    finally:
        journal.close()
    with out.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert tuple(reader.fieldnames) == LEDGER_COLUMNS
        return list(reader)


def assert_row_matches(row: dict, trade) -> None:
    for name in LEDGER_COLUMNS:
        value, text = getattr(trade, name), row[name]
        if isinstance(value, Decimal):
            assert Decimal(text) == value, name
        elif isinstance(value, datetime):
            assert datetime.fromisoformat(text.replace("Z", "+00:00")) == value, name
        elif isinstance(value, Side):
            assert text == value.value, name
        else:
            assert text == str(value), name


async def test_h_full_engine_day_with_a_restart_between_entry_and_exit_books_one_trade_and_a_consistent_ledger(
    engine_rig, broker, clock, sim_root
):
    entries = calls_at(broker, clock, "open_position")
    closes = calls_at(broker, clock, "close_position")
    at_time(broker, clock, ENGINE_CRASH_AT, die)  # proces ginie w trakcie zarządzania pozycją
    with pytest.raises(ProcessDied):
        await engine_rig.engine.run_day(DAY)
    disk = read_disk(engine_rig.path)
    assert disk.order(CID).state is O.CONFIRMED and disk.position.state is P.OPEN and disk.trades == []
    engine_rig.restart()
    report = await engine_rig.engine.run_day(DAY)
    assert entries == [DECISION_UTC] and closes == [FLATTEN_UTC]  # jedno wejście, jedno wyjście o 17:25
    disk = read_disk(engine_rig.path)
    [t] = disk.trades
    assert (t.exit_reason, t.exit_ts, t.size) == ("FORCED", FLATTEN_UTC, D("0.001"))
    assert report.entries == 1 and report.final_position_state is P.FLAT and report.skip_reason is None
    assert report.net_pnl == t.net_pnl and disk.pnl == {DAY: t.net_pnl} and disk.pnl_total == t.net_pnl
    assert_books_balance(disk)
    assert disk.position == PositionRecord(EPIC, P.FLAT) and disk.kill.level is KillLevel.NONE
    [row] = ledger_rows(engine_rig.path, sim_root / "ledger.csv")
    assert_row_matches(row, t)


async def test_engine_crash_between_booking_and_kill_escalation_reapplies_the_hard_kill_before_any_broker_call(
    engine_rig, broker, clock, cfgs
):
    limit = cfgs[1].max_total_drawdown
    engine_rig.engine.journal.add_realized_pnl(EARLIER_DAY, -limit + CENT)  # grosz od limitu łącznego
    at_time(broker, clock, ENGINE_STOP_AT, lambda: broker.trigger_stop(DEAL, STOP_LEVEL))
    crash_after(engine_rig.engine.journal, "record_trade")  # uzgadnianie księguje, proces ginie przed kill switchem
    with pytest.raises(ProcessDied):
        await engine_rig.engine.run_day(DAY)
    disk = read_disk(engine_rig.path)
    [t] = disk.trades
    assert t.exit_reason == "STOP" and t.net_pnl < 0 and disk.pnl_total == -limit + CENT + t.net_pnl <= -limit
    assert disk.kill.level is KillLevel.NONE and disk.position.state is P.OPEN  # poziom i FLAT przepadły
    engine_rig.restart()
    first_call = []
    broker.on_call("login", lambda: first_call.append(read_disk(engine_rig.path).kill), once=True)
    stop_engine_at(engine_rig.engine, clock, ENGINE_STOP_AT + timedelta(minutes=5))
    await engine_rig.engine.run_day(DAY)
    [kill] = first_call  # stan na dysku w chwili pierwszego wywołania brokera po restarcie
    assert (kill.level, kill.reason) == (KillLevel.HARD, "TOTAL_DRAWDOWN")
    disk = read_disk(engine_rig.path)
    assert len(disk.trades) == 1 and disk.position == PositionRecord(EPIC, P.FLAT)
    assert_books_balance(disk, {EARLIER_DAY: -limit + CENT})
    assert (disk.kill.level, disk.kill.reason) == (KillLevel.HARD, "TOTAL_DRAWDOWN")
    assert broker.calls.count("open_position") == 1 and "close_position" not in broker.calls


async def test_engine_kill_9_inside_record_trade_restarts_from_the_crash_image_and_books_exactly_once(
    engine_rig, broker, clock, sim_root
):
    at_time(broker, clock, ENGINE_STOP_AT, lambda: broker.trigger_stop(DEAL, STOP_LEVEL))
    images = []

    def kill_9() -> None:  # obraz plików dziennika w chwili śmierci: transakcja zapisana, wynik jeszcze nie
        images.append(crash_image(engine_rig.path, sim_root / "image"))
        die()

    install_faults(engine_rig.engine.journal).after("INSERT INTO trades", kill_9)
    with pytest.raises(ProcessDied):
        await engine_rig.engine.run_day(DAY)
    [image] = images
    disk = read_disk(image)
    assert disk.trades == [] and disk.pnl == {} and disk.position.state is P.OPEN  # ani transakcji, ani wyniku
    engine_rig.restart(path=image)  # nowy proces na tym, co przeżyło kill -9
    stop_engine_at(engine_rig.engine, clock, ENGINE_STOP_AT + timedelta(minutes=5))
    await engine_rig.engine.run_day(DAY)
    disk = read_disk(image)
    [t] = disk.trades
    assert t.exit_reason == "STOP" and t.exit_ts == ENGINE_STOP_AT and disk.pnl == {DAY: t.net_pnl}
    assert_books_balance(disk)
    assert disk.position == PositionRecord(EPIC, P.FLAT) and broker.calls.count("open_position") == 1


async def test_control_engine_without_a_restart_closes_a_slipped_fill_at_once(engine_rig, broker, clock):
    broker.fill_slippage = D("10")  # wykonanie 10 pkt gorzej niż notowanie decyzji: > 25% stopu 30 pkt
    stop_engine_at(engine_rig.engine, clock, berlin(DAY, 9, 20))
    await engine_rig.engine.run_day(DAY)
    disk = read_disk(engine_rig.path)
    assert "SLIPPAGE_GUARD" in disk.codes() and disk.position.state is P.FLAT and len(disk.trades) == 1


async def test_bug_e2e_1_engine_restart_mid_entry_still_closes_a_slipped_fill(engine_rig, broker, clock):
    broker.fill_slippage = D("10")
    broker.on_call("confirm", die, once=True)  # zlecenie przyjęte, proces ginie przed potwierdzeniem
    with pytest.raises(ProcessDied):
        await engine_rig.engine.run_day(DAY)
    engine_rig.restart()
    closes = calls_at(broker, clock, "close_position")
    stop_engine_at(engine_rig.engine, clock, berlin(DAY, 9, 20))
    await engine_rig.engine.run_day(DAY)
    disk = read_disk(engine_rig.path)
    assert disk.order(CID).state is O.CONFIRMED and disk.order(CID).note.startswith("ADOPTED_BY_RECONCILER")
    assert "SLIPPAGE_GUARD" in disk.codes() and "FILL_GUARD_ADOPTED" in disk.codes()
    [t] = disk.trades
    assert t.entry_fill == FILL + 10 and t.exit_reason == "FORCED"  # przyjęta po restarcie, zamknięta przez straż
    assert disk.position == PositionRecord(EPIC, P.FLAT) and len(closes) == 1 and closes[0] < berlin(DAY, 9, 20)
    assert broker.calls.count("open_position") == 1
    assert_books_balance(disk)


async def test_e_a_crash_between_the_entry_context_and_the_intent_row_sends_nothing_and_the_retry_proceeds(rig, world):
    """O-4: kontekst wejścia zapisany, wiersz INTENT nie (nic nie wysłano: INTENT poprzedza wysyłkę); po restarcie
    ponowna próba tego dnia dostaje ten sam numer kolejny, osierocony kontekst jest nasz, więc zostaje zastąpiony,
    a wejście idzie dalej (jedno zlecenie, bez ENTRY_EXCEPTION). Wiersz zlecenia pod tym numerem nadal blokuje."""
    crash_before(rig.s.journal, "insert_order", lambda o: True)
    with pytest.raises(ProcessDied):
        await enter(rig)
    disk = read_disk(rig.path)
    assert f"entry_ctx:{CID}" in disk.meta and disk.orders == [] and world.broker.calls == ["account"]
    rig.restart()
    rec = await enter(rig)
    assert rec is not None and rec.state is O.CONFIRMED and rec.order_id == CID
    disk = read_disk(rig.path)
    assert disk.kill.level is KillLevel.NONE and "ENTRY_EXCEPTION" not in kill_reasons(disk)
    assert world.broker.calls.count("open_position") == 1 and [o.order_id for o in disk.orders] == [CID]
    assert disk.position.state is P.OPEN and disk.position.deal_id == DEAL
    assert "ORPHAN_ENTRY_CONTEXT_REPLACED" in disk.codes()


async def test_engine_journal_failure_between_the_trade_and_pnl_statements_books_once_on_the_next_pass(
    engine_rig, broker, clock
):
    at_time(broker, clock, ENGINE_STOP_AT, lambda: broker.trigger_stop(DEAL, STOP_LEVEL))
    faulty = install_faults(engine_rig.engine.journal)
    faulty.fail_before("INSERT INTO realized_pnl")  # transakcja wstawiona, wynik dnia zawodzi: wycofane oba
    stop_engine_at(engine_rig.engine, clock, ENGINE_STOP_AT + timedelta(minutes=2))
    await engine_rig.engine.run_day(DAY)
    assert faulty.rules == []
    disk = read_disk(engine_rig.path)
    [t] = disk.trades
    assert t.exit_reason == "STOP" and disk.pnl == {DAY: t.net_pnl}
    assert_books_balance(disk)
    assert disk.position == PositionRecord(EPIC, P.FLAT)
    assert (disk.kill.level, disk.kill.reason) == (KillLevel.SOFT, "JOURNAL_WRITE_FAILED")


# --- BUG-E2E-2: zablokowaną „zgubioną transakcję" księguje właściciel (resolve-lost-trade) ----------------------------


async def test_bug_e2e_2_the_owner_books_a_blocked_lost_trade_with_resolve_lost_trade(rig, world, cfgs):
    """Reprodukcja z raportu (bez referencji: broker nie poda danych wejścia): uzgadnianie czeka na właściciela,
    `resolve-lost-trade` przy zatrzymanym silniku księguje ostrożny szacunek straty jedyną ścieżką księgowania, a po
    restarcie uzgadnianie nie widzi już rozbieżności."""
    from gwstock.app import Engine, Mode
    from gwstock.cli import DEAL_PROMPT, ESTIMATE_PROMPT, ESTIMATE_WORD, resolve_lost_trade
    from gwstock.config import RuntimeConfig
    from gwstock.journal import Journal

    accept_then_stop_before_first_check(world.broker)
    await enter(rig)
    for _ in range(8):  # silnik pracuje dalej: uzgadnianie samo niczego nie rozstrzyga
        await reconcile(rig)
    before = read_disk(rig.path)
    assert_lost_trade_is_blocked(before)
    assert before.kill.level is KillLevel.SOFT and "MISMATCH_TIMEOUT" in kill_reasons(before)
    timeouts = before.codes("reconcile.event").count("MISMATCH_TIMEOUT")
    [closure] = await world.broker.closed_since(T_ENTRY - timedelta(minutes=1))
    rig.stop()  # właściciel zatrzymuje silnik (polecenie działa tylko przy zatrzymanym)
    calls = len(world.broker.calls)
    answers = {DEAL_PROMPT.format(code="UNMATCHED_CLOSED_DEAL"): DEAL, ESTIMATE_PROMPT: ESTIMATE_WORD}
    out: list[str] = []
    journal = Journal.open(str(rig.path))
    try:
        engine = Engine(world.broker, journal, world.clock, world.calendar, cfgs, world.data_dir,
                        RuntimeConfig(live_allowed=False, account_id="ACC-1"), Mode.DRY_RUN)  # fmt: skip
        code = await resolve_lost_trade(engine, "owner", answers.__getitem__, out.append)
    finally:
        journal.close()
    assert code == 0, out
    reads = {"login", "account", "list_positions", "list_working_orders", "closed_since", "instrument_rules"}
    assert set(world.broker.calls[calls:]) <= reads  # u brokera tylko odczyty
    disk = read_disk(rig.path)
    [t] = disk.trades
    # szacunek ze stopu: −(wielkość × 30 pkt × wartość punktu) − modelowy poślizg wyjścia STOP
    stop_slippage = ((world.costs.entry_slippage_points + world.costs.stop_exit_extra_slippage_points) * SIZE
                     * RULES.value_per_point).quantize(CENT, rounding=ROUND_HALF_UP)  # fmt: skip
    assert t.gross_pnl == -(SIZE * STOP_DISTANCE * RULES.value_per_point).quantize(CENT)
    assert t.net_pnl == t.gross_pnl - stop_slippage and t.exit_fill == closure.exit_level and t.exit_reason == "STOP"
    assert "ESTIMATED" in disk.meta[f"trade_flags:{DEAL}"].split()
    order = disk.order(CID)
    assert order.state is O.CONFIRMED and order.deal_id == DEAL and order.note.split()[0] == "RESOLVED_BY_OWNER"
    assert disk.position == PositionRecord(EPIC, P.FLAT)
    assert disk.pnl == {DAY: t.net_pnl}  # strata trafiła do licznika dnia zamknięcia (limity strat ją widzą)
    assert_books_balance(disk)
    assert [p["phase"] for p in disk.payloads("owner.resolve")] == ["BOOKING", "DONE"]
    assert disk.kill == before.kill  # kill switch zdejmuje właściciel osobno (clear-kill)

    rig.start()  # po restarcie: dwa przebiegi uzgadniania bez nowej rozbieżności
    for _ in range(2):
        result = await reconcile(rig)
        assert not result.blocked and not result.skipped
    after = read_disk(rig.path)
    assert after.position == PositionRecord(EPIC, P.FLAT) and len(after.trades) == 1
    assert after.codes("reconcile.event").count("MISMATCH_TIMEOUT") == timeouts
    assert after.meta.get("mismatch_since", "") == "" and after.kill.level is before.kill.level
    assert_books_balance(after)


async def test_g_a_fill_accepted_while_down_without_a_broker_stop_is_adopted_and_protected_with_the_entry_stop(
    rig, world
):
    fake = world.broker
    fake.fail_next("open_position", ProcessDied(), applied=True)
    with pytest.raises(ProcessDied):
        await enter(rig)
    rig.stop()
    fake.positions[DEAL] = replace(fake.positions[DEAL], stop_level=None)  # broker nie trzyma stopu
    rig.start()
    result = await reconcile(rig)  # przyjęcie + procedura zabezpieczenia (jak silnik po uzgadnianiu)
    assert [p.deal_id for p in result.unprotected] == [DEAL]
    assert fake.positions[DEAL].stop_level == STOP_LEVEL  # odległość stopu z kontekstu wejścia (30 pkt)
    disk = read_disk(rig.path)
    assert disk.order(CID).state is O.CONFIRMED and "STOP_ATTACHED" in disk.codes()
    assert disk.kill.level is KillLevel.NONE and fake.calls.count("set_stop") == 1
    await reconcile(rig)
    assert read_disk(rig.path).position.stop_level == STOP_LEVEL
