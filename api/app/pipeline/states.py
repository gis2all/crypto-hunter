"""Run state machine.

Every round ends the same way: `written` records the rows and `researched` closes it.
There is no publish-then-read split left - the reading happens before the score, inside
the round (see `report/research.py`), so nothing can rewrite a card after it is stored.

`cancelled` is the operator's own exit. It is reachable from every state a round can be
working in - a stop can land during the pull, during the gate, or while the model is
writing - and from none of the states a round has already finished in, which is what keeps
"cancelled" from ever overwriting a published round.
"""

from __future__ import annotations

from ..core.models import RunStatus as S

ALLOWED: dict[str, set[str]] = {
    S.PENDING.value: {S.COLLECTED.value, S.CANCELLED.value, S.FAILED.value},
    S.COLLECTED.value: {S.SCORED.value, S.CANCELLED.value, S.FAILED.value},
    S.SCORED.value: {S.SELECTED.value, S.CANCELLED.value, S.FAILED.value},
    S.SELECTED.value: {S.WRITTEN.value, S.CANCELLED.value, S.FAILED.value},
    S.WRITTEN.value: {S.RESEARCHED.value, S.CANCELLED.value, S.FAILED.value},
    S.RESEARCHED.value: set(),
    S.CANCELLED.value: set(),
    S.FAILED.value: set(),
}

TERMINAL = {S.RESEARCHED.value, S.CANCELLED.value, S.FAILED.value}


def can(current: str, target: str) -> bool:
    return target in ALLOWED.get(current, set())


def next_states(current: str) -> set[str]:
    return set(ALLOWED.get(current, set()))
