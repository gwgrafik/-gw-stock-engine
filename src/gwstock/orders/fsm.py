"""Automat stanów zlecenia i pozycji (spec §6): tabele dozwolonych przejść i ich kontrola.

Zasady:
- przejście spoza tabeli (także X -> X) to `InvalidTransition`: nieobsłużona zmiana stanu jest błędem wywołującego,
  a nie cichym no-opem;
- kontrola przyjmuje wyłącznie enumy (`OrderState`, `PositionState`); gołe napisy są odrzucane z `TypeError`,
  mimo że enumy dziedziczą po `str` i "INTENT" == OrderState.INTENT;
- stan pozycji zmienia się tylko na podstawie odpowiedzi i listy pozycji brokera, nigdy samego wysłania zlecenia
  (egzekwuje to wywołujący; tu jest wyłącznie tabela dozwolonych krawędzi).
"""

from __future__ import annotations

from enum import Enum

from gwstock.types import OrderState, PositionState

O = OrderState
P = PositionState


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
ALLOWED_ORDER_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    O.INTENT: frozenset({O.SUBMITTING, O.NOT_PLACED}),
    O.SUBMITTING: frozenset({O.CONFIRMED, O.REJECTED, O.UNKNOWN}),
    O.UNKNOWN: frozenset({O.CONFIRMED, O.NOT_PLACED}),
    O.CONFIRMED: frozenset(),
    O.REJECTED: frozenset(),
    O.NOT_PLACED: frozenset(),
    O.WORKING: frozenset({O.PARTIAL, O.CANCELING, O.CANCELED, O.EXPIRED, O.CONFIRMED}),
    O.PARTIAL: frozenset({O.CONFIRMED, O.CANCELING, O.CANCELED}),
    O.CANCELING: frozenset({O.CANCELED, O.CONFIRMED}),
    O.CANCELED: frozenset(),
    O.EXPIRED: frozenset(),
}

# Pozycja. Nie ma krawędzi FLAT -> OPEN/EXITING/BLOCKED ani ENTERING -> EXITING: pozycja powstaje tylko przez
# ENTERING potwierdzone przez brokera, a BLOCKED oznacza rozbieżność z brokerem, której przed wejściem nie ma.
# BLOCKED opuszcza wyłącznie decyzja rekoncyliacji lub właściciela.
ALLOWED_POSITION_TRANSITIONS: dict[PositionState, frozenset[PositionState]] = {
    P.FLAT: frozenset({P.ENTERING}),
    # OPEN: broker potwierdził; FLAT: wejście odrzucone lub niezłożone
    P.ENTERING: frozenset({P.OPEN, P.FLAT, P.BLOCKED}),
    # FLAT: pozycja zniknęła u brokera, bo zadziałał stop (księguje rekoncyliator)
    P.OPEN: frozenset({P.EXITING, P.FLAT, P.BLOCKED}),
    # OPEN: zamknięcie się nie powiodło, pozycja nadal otwarta
    P.EXITING: frozenset({P.FLAT, P.BLOCKED, P.OPEN}),
    P.BLOCKED: frozenset({P.FLAT, P.OPEN}),
}


def _check(
    kind: str,
    table: dict,
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
