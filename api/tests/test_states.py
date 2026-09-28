"""The state machine: a round only ever moves forward, and only along the path it walked."""

import pytest

from app.core.models import RunStatus as S
from app.pipeline import states


def test_happy_path_is_walkable():
    path = [S.PENDING, S.COLLECTED, S.SCORED, S.SELECTED, S.WRITTEN, S.RESEARCHED]
    for current, nxt in zip(path, path[1:]):
        assert states.can(current.value, nxt.value), f"{current} -> {nxt}"


def test_written_is_the_last_step_before_researched():
    assert states.can(S.WRITTEN.value, S.RESEARCHED.value)
    assert not states.can(S.WRITTEN.value, S.SCORED.value)


def test_skipping_stages_is_refused():
    assert not states.can(S.PENDING.value, S.WRITTEN.value)


def test_failed_researched_and_cancelled_are_terminal():
    assert states.TERMINAL == {S.RESEARCHED.value, S.CANCELLED.value, S.FAILED.value}
    assert states.next_states(S.FAILED.value) == set()
    assert states.next_states(S.CANCELLED.value) == set()


def test_cancelled_is_reachable_from_every_working_state():
    """A stop can land while a round is pulling, judging or writing - and it can never land
    on a state the round has already finished in, which is what keeps a published round from
    being relabelled as stopped."""
    for state in (S.PENDING, S.COLLECTED, S.SCORED, S.SELECTED, S.WRITTEN):
        assert states.can(state.value, S.CANCELLED.value), state
    for state in (S.RESEARCHED, S.CANCELLED, S.FAILED):
        assert not states.can(state.value, S.CANCELLED.value), state


def test_illegal_transition_raises_when_driving_a_run(isolated_db):
    from app.pipeline import run as pipeline_run

    created = pipeline_run.create_run("arc")
    with pytest.raises(ValueError):
        pipeline_run._set_status(created["id"], S.WRITTEN.value)
