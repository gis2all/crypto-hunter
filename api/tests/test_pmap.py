"""`pmap` and the stop: the pool every paid pass runs its tasks through.

Nothing here is a round. The pool is where a stop turns into work not done - a task that never
started is the only thing a stop actually saves - so what these pin down is that a stopped
pass starts no further task, and that everything else about the function is what it always
was.
"""

from __future__ import annotations

import pytest

from app import cancel
from app.sources import http


def test_a_stopped_pass_starts_no_further_task():
    """The check is asked before every task, and what it raises is what ends the pass."""
    seen: list[int] = []

    def work(item: int) -> int:
        seen.append(item)
        if item == 2:
            # The operator pressing 中止, as the request leaves it behind.
            cancel.request("run-pmap")
        return item

    try:
        with pytest.raises(cancel.RoundCancelled):
            http.pmap(work, range(6), workers=1, stop=cancel.check("run-pmap"))
    finally:
        cancel.clear("run-pmap")
    # Three ran; the fourth was never started, and neither were the fifth and sixth.
    assert seen == [0, 1, 2]


def test_a_single_task_is_behind_the_same_check():
    """The one-item shortcut is still a task, and still asks before it runs."""
    cancel.request("run-one")
    try:
        with pytest.raises(cancel.RoundCancelled):
            http.pmap(lambda item: item, [1], workers=2, stop=cancel.check("run-one"))
    finally:
        cancel.clear("run-one")


def test_a_failure_is_still_that_task_s_own_none():
    """The stop changed how the queue is handed out, not what a failing task means."""

    def work(item: int) -> int:
        if item == 2:
            raise ValueError("this host said no")
        return item

    assert http.pmap(work, [0, 1, 2, 3], workers=2) == [0, 1, None, 3]


def test_no_stop_is_the_old_behaviour():
    """Every caller that hands in no check gets every answer, in order."""
    assert http.pmap(lambda item: item * 10, [1, 2, 3, 4, 5], workers=3) == [10, 20, 30, 40, 50]
    assert http.pmap(lambda item: item, [7], workers=3) == [7]
    assert http.pmap(lambda item: item, [], workers=3) == []
