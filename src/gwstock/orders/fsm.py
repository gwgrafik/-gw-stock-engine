"""Automat stanów zlecenia i pozycji (spec §6): tabele dozwolonych przejść i ich kontrola.

Zasady:
- przejście spoza tabeli (także X -> X) to `InvalidTransition`: nieobsłużona zmiana stanu jest błędem wywołującego,
  a nie cichym no-opem;
- tabele są tylko do odczytu (`MappingProxyType` z wartościami `frozenset`): nikt nie dopisze krawędzi w locie;
- kontrola przyjmuje wyłącznie enumy (`OrderState`, `PositionState`); gołe napisy są odrzucane z `TypeError`,
  mimo że enumy dziedziczą po `str` i "INTENT" == OrderState.INTENT;
- stan pozycji zmienia się tylko na podstawie odpowiedzi i listy pozycji brokera, nigdy samego wysłania zlecenia
  (egzekwuje to wywołujący; tu jest wyłącznie tabela dozwolonych krawędzi).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType

from gwstock.types import OrderState, PositionState

_O = OrderState
_P = PositionState


class InvalidTransition(Exception):
    """Niedozwolone przejście stanu; komunikat nazywa oba stany, np. `ORDER INTENT -> CONFIRMED`."""


# Zlecenie. Stany końcowe (CONFIRMED, REJECTED, NOT_PLACED) nie mają wyjść.
# INTENT -> NOT_PLACED: zlecenie porzucone przed wysyłką (np. restart zastaje wiersz INTENT); bezpieczne, bo
# SUBMITTING jest zapisywane w dzienniku przed wywołaniem brokera. INTENT -> CONFIRMED/REJECTED/UNKNOWN jest
# niedozwolone: bez SUBMITTING nic nie poszło do brokera.
#
# Stany rozszerzone (WORKING, PARTIAL, CANCELING, CANCELED, EXPIRED) nie są używane w v1: żaden stan v1 do nich
# nie prowadzi (nie ma krawędzi wejściowej, dołoży ją adapter, który ich potrzebuje), a one same dochodzą do
# stanów v1 tylko przez CONFIRMED. CANCELED i EXPIRED są końcowe.
ALLOWED_ORDER_TRANSITIONS: Mapping[OrderState, frozenset[OrderState]] = MappingProxyType({
    _O.INTENT: frozenset({_O.SUBMITTING, _O.NOT_PLACED}),
    _O.SUBMITTING: frozenset({_O.CONFIRMED, _O.REJECTED, _O.UNKNOWN}),
    _O.UNKNOWN: frozenset({_O.CONFIRMED, _O.NOT_PLACED}),
    _O.CONFIRMED: frozenset(),
    _O.REJECTED: frozenset(),
    _O.NOT_PLACED: frozenset(),
    _O.WORKING: frozenset({_O.PARTIAL, _O.CANCELING, _O.CANCELED, _O.EXPIRED, _O.CONFIRMED}),
    _O.PARTIAL: frozenset({_O.CONFIRMED, _O.CANCELING, _O.CANCELED}),
    _O.CANCELING: frozenset({_O.CANCELED, _O.CONFIRMED}),
    _O.CANCELED: frozenset(),
    _O.EXPIRED: frozenset(),
})

# Pozycja. Nie ma krawędzi FLAT -> OPEN/EXITING/BLOCKED ani ENTERING -> EXITING: pozycja powstaje tylko przez
# ENTERING potwierdzone przez brokera, a BLOCKED oznacza rozbieżność z brokerem, której przed wejściem nie ma.
# BLOCKED opuszcza wyłącznie decyzja rekoncyliacji lub właściciela.
ALLOWED_POSITION_TRANSITIONS: Mapping[PositionState, frozenset[PositionState]] = MappingProxyType({
    _P.FLAT: frozenset({_P.ENTERING}),
    # OPEN: broker potwierdził; FLAT: wejście odrzucone lub niezłożone
    _P.ENTERING: frozenset({_P.OPEN, _P.FLAT, _P.BLOCKED}),
    # FLAT: pozycja zniknęła u brokera, bo zadziałał stop (księguje rekoncyliator)
    _P.OPEN: frozenset({_P.EXITING, _P.FLAT, _P.BLOCKED}),
    # OPEN: zamknięcie się nie powiodło, pozycja nadal otwarta
    _P.EXITING: frozenset({_P.FLAT, _P.BLOCKED, _P.OPEN}),
    _P.BLOCKED: frozenset({_P.FLAT, _P.OPEN}),
})


def _check(
    kind: str,
    table: Mapping,
    state_type: type[Enum],
    old: object,
    new: object,
) -> None:
    for role, state in (("old", old), ("new", new)):
        if not isinstance(state, state_type):
            raise TypeError(f"{kind} {role} state must be {state_type.__name__}, got {type(state).__name__} {state!r}")
    if new not in table[old]:
        raise InvalidTransition(f"{kind} {old.value} -> {new.value}")


def check_order_transition(old: OrderState, new: OrderState) -> None:
    """Przechodzi bez wyniku, gdy `old -> new` jest dozwolone; inaczej `InvalidTransition`, a dla nie-enuma `TypeError`."""
    _check("ORDER", ALLOWED_ORDER_TRANSITIONS, OrderState, old, new)


def check_position_transition(old: PositionState, new: PositionState) -> None:
    """Przechodzi bez wyniku, gdy `old -> new` jest dozwolone; inaczej `InvalidTransition`, a dla nie-enuma `TypeError`."""
    _check("POSITION", ALLOWED_POSITION_TRANSITIONS, PositionState, old, new)
