"""Testy automatu stanów zleceń i pozycji (spec §6) oraz identyfikatorów zleceń `gws-...`.

Tabele przejść są sprawdzane względem NIEZALEŻNEJ listy krawędzi z testu (pełny iloczyn kartezjański stanów),
więc dodatkowa lub brakująca krawędź w `fsm.py` nie przejdzie niezauważona.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from itertools import product
from types import MappingProxyType

import pytest

from gwstock.journal import Journal, OrderRecord
from gwstock.orders.fsm import (
    ALLOWED_ORDER_TRANSITIONS,
    ALLOWED_POSITION_TRANSITIONS,
    InvalidTransition,
    check_order_transition,
    check_position_transition,
)
from gwstock.orders.ids import make_client_id, parse_client_id
from gwstock.types import OrderState, PositionState, Side

O = OrderState
P = PositionState

# --- wzorzec: dozwolone krawędzie zleceń (spec §6 + ustalenie INTENT -> NOT_PLACED) --------------

V1_ORDER_EDGES = [
    (O.INTENT, O.SUBMITTING),
    (O.INTENT, O.NOT_PLACED),  # porzucone przed wysyłką (restart zastaje INTENT); SUBMITTING jest zapisywany przed brokerem
    (O.SUBMITTING, O.CONFIRMED),
    (O.SUBMITTING, O.REJECTED),
    (O.SUBMITTING, O.UNKNOWN),
    (O.UNKNOWN, O.CONFIRMED),
    (O.UNKNOWN, O.NOT_PLACED),
]
# stany rozszerzone: nieużywane w v1, osiągalne tylko jeden z drugiego (i kończą się w CONFIRMED)
EXTENDED_ORDER_EDGES = [
    (O.WORKING, O.PARTIAL),
    (O.WORKING, O.CANCELING),
    (O.WORKING, O.CANCELED),
    (O.WORKING, O.EXPIRED),
    (O.WORKING, O.CONFIRMED),
    (O.PARTIAL, O.CONFIRMED),
    (O.PARTIAL, O.CANCELING),
    (O.PARTIAL, O.CANCELED),
    (O.CANCELING, O.CANCELED),
    (O.CANCELING, O.CONFIRMED),
]
ORDER_EDGES = V1_ORDER_EDGES + EXTENDED_ORDER_EDGES
EXTENDED_ORDER_STATES = {O.WORKING, O.PARTIAL, O.CANCELING, O.CANCELED, O.EXPIRED}
V1_ORDER_STATES = set(OrderState) - EXTENDED_ORDER_STATES

# --- wzorzec: dozwolone krawędzie pozycji (spec §6 "Pozycja" + ustalenia) ------------------------

POSITION_EDGES = [
    (P.FLAT, P.ENTERING),
    (P.FLAT, P.BLOCKED),  # uzgadnianie przy FLAT: obca pozycja, sprzeczność z dziennikiem, niedopasowane zamknięcie
    (P.ENTERING, P.OPEN),  # broker potwierdził
    (P.ENTERING, P.FLAT),  # wejście odrzucone / niezłożone
    (P.ENTERING, P.BLOCKED),
    (P.OPEN, P.EXITING),
    (P.OPEN, P.FLAT),  # pozycja zniknęła u brokera (zadziałał stop), księguje rekoncyliator
    (P.OPEN, P.BLOCKED),
    (P.EXITING, P.FLAT),
    (P.EXITING, P.BLOCKED),
    (P.EXITING, P.OPEN),  # zamknięcie się nie powiodło, pozycja nadal otwarta
    (P.BLOCKED, P.FLAT),  # tylko decyzja rekoncyliacji lub właściciela
    (P.BLOCKED, P.OPEN),
    (P.BLOCKED, P.EXITING),  # wyłącznie awaryjne spłaszczenie dowodnie naszej pozycji (deal_id na liście brokera)
]


def _pair_id(pair):
    return f"{pair[0].value}->{pair[1].value}"


def _params(pairs):
    return [pytest.param(a, b, id=_pair_id((a, b))) for a, b in pairs]


ORDER_FORBIDDEN = [pair for pair in product(OrderState, OrderState) if pair not in ORDER_EDGES]
POSITION_FORBIDDEN = [pair for pair in product(PositionState, PositionState) if pair not in POSITION_EDGES]


def _edges(table):
    return {(a, b) for a, targets in table.items() for b in targets}


# --- zlecenia: tabela --------------------------------------------------------------------------


def test_spec_transitions():
    ok = [
        (O.INTENT, O.SUBMITTING),
        (O.SUBMITTING, O.CONFIRMED),
        (O.SUBMITTING, O.REJECTED),
        (O.SUBMITTING, O.UNKNOWN),
        (O.UNKNOWN, O.CONFIRMED),
        (O.UNKNOWN, O.NOT_PLACED),
    ]
    for a, b in ok:
        check_order_transition(a, b)
    for a, b in [(O.INTENT, O.CONFIRMED), (O.CONFIRMED, O.SUBMITTING), (O.NOT_PLACED, O.CONFIRMED), (O.REJECTED, O.UNKNOWN)]:
        with pytest.raises(InvalidTransition):
            check_order_transition(a, b)


def test_every_order_state_has_a_key_and_table_is_exactly_the_allowed_set():
    assert set(ALLOWED_ORDER_TRANSITIONS) == set(OrderState)
    assert all(isinstance(v, frozenset) for v in ALLOWED_ORDER_TRANSITIONS.values())
    assert _edges(ALLOWED_ORDER_TRANSITIONS) == set(ORDER_EDGES)


@pytest.mark.parametrize(("old", "new"), _params(ORDER_EDGES))
def test_order_transition_allowed(old, new):
    assert check_order_transition(old, new) is None


@pytest.mark.parametrize(("old", "new"), _params(ORDER_FORBIDDEN))
def test_order_transition_forbidden(old, new):
    with pytest.raises(InvalidTransition, match=f"^ORDER {old.value} -> {new.value}$"):
        check_order_transition(old, new)


def test_terminal_states_have_no_exits():
    for s in (O.CONFIRMED, O.REJECTED, O.NOT_PLACED, O.CANCELED, O.EXPIRED):
        assert not ALLOWED_ORDER_TRANSITIONS[s]


def test_intent_can_be_abandoned_but_cannot_skip_submitting():
    check_order_transition(O.INTENT, O.NOT_PLACED)
    for new in (O.CONFIRMED, O.REJECTED, O.UNKNOWN):
        with pytest.raises(InvalidTransition):
            check_order_transition(O.INTENT, new)


def test_order_self_transition_is_invalid():
    for s in OrderState:
        with pytest.raises(InvalidTransition, match=f"ORDER {s.value} -> {s.value}"):
            check_order_transition(s, s)


# --- zlecenia: stany rozszerzone nie wyciekają do v1 ---------------------------------------------


def test_no_v1_state_enters_an_extended_state():
    for s in V1_ORDER_STATES:
        assert ALLOWED_ORDER_TRANSITIONS[s].isdisjoint(EXTENDED_ORDER_STATES), s


def test_extended_states_only_reach_each_other_or_confirmed():
    for s in EXTENDED_ORDER_STATES:
        assert ALLOWED_ORDER_TRANSITIONS[s] <= EXTENDED_ORDER_STATES | {O.CONFIRMED}, s


@pytest.mark.parametrize("new", [O.WORKING, O.PARTIAL, O.CANCELING, O.CANCELED, O.EXPIRED])
@pytest.mark.parametrize("old", [O.INTENT, O.SUBMITTING, O.UNKNOWN])
def test_v1_path_cannot_reach_extended_states(old, new):
    with pytest.raises(InvalidTransition):
        check_order_transition(old, new)


def test_only_v1_states_are_reachable_from_intent():
    seen, todo = {O.INTENT}, [O.INTENT]
    while todo:
        for nxt in ALLOWED_ORDER_TRANSITIONS[todo.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    assert seen == V1_ORDER_STATES


# --- pozycje -----------------------------------------------------------------------------------


def test_position_flow():
    for a, b in [(P.FLAT, P.ENTERING), (P.ENTERING, P.OPEN), (P.OPEN, P.EXITING), (P.EXITING, P.FLAT), (P.OPEN, P.BLOCKED)]:
        check_position_transition(a, b)
    with pytest.raises(InvalidTransition):
        check_position_transition(P.FLAT, P.OPEN)  # pozycja nie powstaje bez odpowiedzi brokera na wejście


def test_every_position_state_has_a_key_and_table_is_exactly_the_allowed_set():
    assert set(ALLOWED_POSITION_TRANSITIONS) == set(PositionState)
    assert all(isinstance(v, frozenset) for v in ALLOWED_POSITION_TRANSITIONS.values())
    assert _edges(ALLOWED_POSITION_TRANSITIONS) == set(POSITION_EDGES)


@pytest.mark.parametrize(("old", "new"), _params(POSITION_EDGES))
def test_position_transition_allowed(old, new):
    assert check_position_transition(old, new) is None


@pytest.mark.parametrize(("old", "new"), _params(POSITION_FORBIDDEN))
def test_position_transition_forbidden(old, new):
    with pytest.raises(InvalidTransition, match=f"^POSITION {old.value} -> {new.value}$"):
        check_position_transition(old, new)


def test_position_cannot_appear_from_flat_except_through_entering():
    for new in (P.OPEN, P.EXITING):
        with pytest.raises(InvalidTransition):
            check_position_transition(P.FLAT, new)
    with pytest.raises(InvalidTransition):
        check_position_transition(P.ENTERING, P.EXITING)


def test_flat_can_only_be_blocked_by_reconciliation_or_start_entering():
    assert ALLOWED_POSITION_TRANSITIONS[P.FLAT] == {P.ENTERING, P.BLOCKED}  # BLOCKED: obca pozycja przy FLAT


def test_position_self_transition_is_invalid():
    for s in PositionState:
        with pytest.raises(InvalidTransition, match=f"POSITION {s.value} -> {s.value}"):
            check_position_transition(s, s)


def test_blocked_exits_only_to_flat_open_or_the_emergency_exit():
    assert ALLOWED_POSITION_TRANSITIONS[P.BLOCKED] == {P.FLAT, P.OPEN, P.EXITING}


# --- sprawdzanie typów: gołe napisy i obcy enum nie przechodzą ------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("INTENT", O.SUBMITTING),  # enum dziedziczy po str, więc "INTENT" == OrderState.INTENT; sprawdzamy typ jawnie
        (O.INTENT, "SUBMITTING"),
        ("INTENT", "SUBMITTING"),
        (None, O.SUBMITTING),
        (O.INTENT, None),
        (P.FLAT, O.SUBMITTING),
        (O.INTENT, P.ENTERING),
    ],
)
def test_order_check_accepts_only_order_state_enums(old, new):
    with pytest.raises(TypeError):
        check_order_transition(old, new)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("FLAT", P.ENTERING),
        (P.FLAT, "ENTERING"),
        ("FLAT", "ENTERING"),
        (None, P.ENTERING),
        (P.FLAT, None),
        (O.INTENT, P.ENTERING),
        (P.FLAT, O.SUBMITTING),
    ],
)
def test_position_check_accepts_only_position_state_enums(old, new):
    with pytest.raises(TypeError):
        check_position_transition(old, new)


def test_invalid_transition_is_a_plain_exception():
    assert issubclass(InvalidTransition, Exception)
    assert not issubclass(InvalidTransition, (TypeError, ValueError))


# --- tabele są tylko do odczytu --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "key"),
    [(ALLOWED_ORDER_TRANSITIONS, O.CONFIRMED), (ALLOWED_POSITION_TRANSITIONS, P.BLOCKED)],
    ids=["order", "position"],
)
def test_transition_tables_are_read_only(table, key):
    assert isinstance(table, MappingProxyType)
    with pytest.raises(TypeError):
        table[key] = table[key]  # przypisanie tej samej wartości: gdyby przeszło, nie psuje innych testów
    assert all(isinstance(v, frozenset) for v in table.values())  # wartości też niemutowalne


# --- identyfikatory ------------------------------------------------------------------------------

DAY = date(2026, 11, 2)


def test_client_id_format_and_uniqueness():
    a = make_client_id(date(2026, 11, 2), "DE40", 1, "entry")
    assert a == "gws-20261102-DE40-1-entry" and len(a) <= 128
    assert a != make_client_id(date(2026, 11, 2), "DE40", 2, "entry")


def test_client_id_distinguishes_day_instrument_and_role():
    base = make_client_id(DAY, "DE40", 1, "entry")
    assert base != make_client_id(date(2026, 11, 3), "DE40", 1, "entry")
    assert base != make_client_id(DAY, "US500", 1, "entry")
    assert base != make_client_id(DAY, "DE40", 1, "exit")


def test_client_id_pads_date_and_does_not_pad_seq():
    assert make_client_id(date(2027, 1, 5), "DE40", 12, "exit") == "gws-20270105-DE40-12-exit"
    assert make_client_id(date(1, 2, 3), "X", 1, "r") == "gws-00010203-X-1-r"


def test_client_id_starts_with_the_day_prefix_that_journal_counts_by():
    assert make_client_id(DAY, "DE40", 7, "entry").startswith("gws-20261102-")


def test_journal_count_orders_counts_generated_ids(tmp_path):
    journal = Journal.open(str(tmp_path / "j.sqlite"))
    try:
        t0 = datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc)
        for day, seq in [(DAY, 1), (DAY, 2), (date(2026, 11, 3), 1)]:
            journal.insert_order(
                OrderRecord(
                    order_id=make_client_id(day, "DE40", seq, "entry"),
                    epic="DE40",
                    side=Side.BUY,
                    size=Decimal("0.001"),
                    stop_distance=Decimal("30"),
                    state=OrderState.INTENT,
                    deal_reference=None,
                    deal_id=None,
                    created_at=t0,
                    updated_at=t0,
                )
            )
        assert journal.count_orders(DAY) == 2 and journal.next_seq(DAY) == 3
        assert journal.count_orders(date(2026, 11, 3)) == 1
    finally:
        journal.close()


def test_client_id_length_limit_is_128_inclusive():
    prefix_len = len("gws-20261102--1-r")  # 17 znaków stałych razem z jednoznakową rolą
    ok = make_client_id(DAY, "A" * (128 - prefix_len), 1, "r")
    assert len(ok) == 128
    with pytest.raises(ValueError, match="128"):
        make_client_id(DAY, "A" * (128 - prefix_len + 1), 1, "r")
    with pytest.raises(ValueError, match="128"):
        make_client_id(DAY, "DE40", 1, "r" * 200)


@pytest.mark.parametrize("long_field", ["instrument", "role"])
def test_client_id_of_exactly_128_characters_round_trips_and_129_is_rejected(long_field):
    fixed = len("gws-20261102-") + len("-1-") + 1  # prefiks z datą, "-1-" i jednoznakowy drugi człon
    filler = "A" * (128 - fixed)
    instrument, role = (filler, "r") if long_field == "instrument" else ("D", filler)
    client_id = make_client_id(DAY, instrument, 1, role)
    assert len(client_id) == 128
    assert parse_client_id(client_id) == (DAY, instrument, 1, role)
    assert make_client_id(*parse_client_id(client_id)) == client_id
    longer = (filler + "A", "r") if long_field == "instrument" else ("D", filler + "A")
    with pytest.raises(ValueError, match="128"):
        make_client_id(DAY, longer[0], 1, longer[1])
    with pytest.raises(ValueError, match="128"):
        parse_client_id(f"gws-20261102-{longer[0]}-1-{longer[1]}")


@pytest.mark.parametrize("bad_day", [datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc), datetime(2026, 11, 2), "2026-11-02", None, 20261102])
def test_client_id_day_must_be_a_plain_date(bad_day):
    with pytest.raises(TypeError, match="date"):
        make_client_id(bad_day, "DE40", 1, "entry")


@pytest.mark.parametrize("bad", ["", "DE-40", "DE 40", "DE40\n", "DE40_", "DE.40", "DĘ40", "DE４０", "-", "gws-x"])
@pytest.mark.parametrize("field", ["instrument", "role"])
def test_client_id_instrument_and_role_must_be_alphanumeric_without_dash(field, bad):
    kwargs = {"instrument": "DE40", "role": "entry"}
    kwargs[field] = bad
    with pytest.raises(ValueError, match=field):
        make_client_id(DAY, kwargs["instrument"], 1, kwargs["role"])


@pytest.mark.parametrize("field", ["instrument", "role"])
@pytest.mark.parametrize("bad", [None, 40, b"DE40"])
def test_client_id_instrument_and_role_must_be_str(field, bad):
    kwargs = {"instrument": "DE40", "role": "entry"}
    kwargs[field] = bad
    with pytest.raises(TypeError, match=field):
        make_client_id(DAY, kwargs["instrument"], 1, kwargs["role"])


@pytest.mark.parametrize("field", ["instrument", "role"])
def test_client_id_rejects_str_subclasses(field):
    # (str, Enum) w f-stringu 3.12 daje "Side.BUY", więc id byłoby nieparsowalne; wymagamy dokładnie `str`
    kwargs = {"instrument": "DE40", "role": "entry"}
    kwargs[field] = Side.BUY
    with pytest.raises(TypeError, match=rf"{field}.*Side"):
        make_client_id(DAY, kwargs["instrument"], 1, kwargs["role"])


def test_client_id_rejects_value_whose_rendering_is_not_the_expected_token():
    class OffsetDay(date):  # podklasa `date` z nadpisanym miesiącem: sklejony napis ma 9 cyfr zamiast 8
        @property
        def month(self):
            return 123

    class OddInt(int):  # podklasa `int` renderowana jako coś, co nie jest liczbą >= 1 bez zer wiodących
        def __format__(self, spec):
            return "007"

    with pytest.raises(ValueError, match="client id"):
        make_client_id(OffsetDay(2026, 11, 2), "DE40", 1, "entry")
    with pytest.raises(ValueError, match="client id"):
        make_client_id(DAY, "DE40", OddInt(7), "entry")


@pytest.mark.parametrize("bad_seq", [0, -1, -100])
def test_client_id_seq_must_be_positive(bad_seq):
    with pytest.raises(ValueError, match="seq"):
        make_client_id(DAY, "DE40", bad_seq, "entry")


@pytest.mark.parametrize("bad_seq", [True, False, 1.0, "1", None, Decimal(1)])
def test_client_id_seq_must_be_a_real_int(bad_seq):
    with pytest.raises(TypeError, match="seq"):
        make_client_id(DAY, "DE40", bad_seq, "entry")


# --- parse_client_id -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "instrument", "seq", "role"),
    [
        (DAY, "DE40", 1, "entry"),
        (date(2026, 12, 31), "DE40", 12, "exit"),
        (date(9999, 12, 31), "A1b2", 999999, "stopfix"),
        (date(1, 1, 1), "X", 1, "r"),
    ],
)
def test_parse_client_id_is_the_inverse_of_make(day, instrument, seq, role):
    client_id = make_client_id(day, instrument, seq, role)
    assert parse_client_id(client_id) == (day, instrument, seq, role)
    assert make_client_id(*parse_client_id(client_id)) == client_id


def test_parse_client_id_returns_typed_tuple():
    d, instrument, seq, role = parse_client_id("gws-20261102-DE40-3-entry")
    assert type(d) is date and d == DAY
    assert (instrument, seq, role) == ("DE40", 3, "entry")
    assert type(seq) is int


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "gws",
        "gws-20261102-DE40-1",  # brak roli
        "gws-20261102-DE40-1-entry-x",  # nadmiarowy człon
        "xxx-20261102-DE40-1-entry",  # obcy prefiks
        "GWS-20261102-DE40-1-entry",
        "gws-2026110-DE40-1-entry",  # 7 cyfr
        "gws-202611022-DE40-1-entry",  # 9 cyfr
        "gws-20261302-DE40-1-entry",  # miesiąc 13
        "gws-20261131-DE40-1-entry",  # 31 listopada
        "gws-00000000-DE40-1-entry",
        "gws-2026-11-02-DE40-1-entry",  # data z kreskami
        "gws-20261102--1-entry",  # pusty instrument
        "gws-20261102-DE40--entry",  # pusty seq
        "gws-20261102-DE40-1-",  # pusta rola
        "gws-20261102-DE40-0-entry",
        "gws-20261102-DE40-01-entry",  # niekanoniczny seq (make nigdy go nie produkuje)
        "gws-20261102-DE40-+1-entry",
        "gws-20261102-DE40-1.0-entry",
        "gws-20261102-DE40-x-entry",
        "gws-20261102-DE40-1-entry\n",
        "gws-20261102-DE40-1-ent ry",
        "gws-20261102-DE_40-1-entry",
        "gws-20261102-DE40-١-entry",  # cyfra arabsko-indyjska
        "gws-٢٠٢٦١١٠٢-DE40-1-entry",
        "gws-20261102-" + "A" * 120 + "-1-entry",  # dłuższy niż 128
    ],
)
def test_parse_client_id_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_client_id(bad)


@pytest.mark.parametrize("bad", [None, 20261102, b"gws-20261102-DE40-1-entry"])
def test_parse_client_id_requires_str(bad):
    with pytest.raises(TypeError):
        parse_client_id(bad)
