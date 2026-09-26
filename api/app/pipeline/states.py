"""Run state machine.

`sent` is a hard boundary: after it, only the lexicon may change. That is how
"send first, research later" is enforced - by the state, not by a prompt rule.

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
    S.WRITTEN.value: {S.SENT.value, S.CANCELLED.value, S.FAILED.value},
    S.SENT.value: {S.RESEARCHED.value, S.CANCELLED.value, S.FAILED.value},
    S.RESEARCHED.value: set(),
    S.CANCELLED.value: set(),
    S.FAILED.value: set(),
}

TERMINAL = {S.RESEARCHED.value, S.CANCELLED.value, S.FAILED.value}

#: The states after which the automated part of a round is over: the page shows 用时 and stops
#: the clock. `sent` is here even though it is not terminal, because what follows it is the
#: lexicon being confirmed - a human reading entries, not the round doing work. Leaving it out
#: is why every round read as still running with a stopwatch that never stopped.
DELIVERED = {S.SENT.value, *TERMINAL}


def can(current: str, target: str) -> bool:
    return target in ALLOWED.get(current, set())


def next_states(current: str) -> set[str]:
    return set(ALLOWED.get(current, set()))
