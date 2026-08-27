"""A run's events, read as a timeline.

The formatting is the whole contribution: every line is one stored event, so
these tests are about legibility and about never inventing anything.
"""

import pytest

from richbuild.runlog import follow_run, format_event, run_is_settled
from richbuild.store import RichStore


def _event(sequence, event_type, payload=None, task_id=None):
    return {
        "sequence": sequence,
        "run_id": "run.1",
        "task_id": task_id,
        "event_type": event_type,
        "payload": payload or {},
        "created_at": "2026-08-27T14:03:09.123456+00:00",
    }


def test_a_line_carries_the_clock_the_outcome_and_the_event():
    line = format_event(_event(1, "task.succeeded", {"status": "succeeded"}))

    assert line.startswith("14:03:09")
    assert "ok" in line
    assert "task.succeeded" in line


def test_failure_is_visible_without_reading_the_payload():
    failed = format_event(_event(2, "task.failed", {"status": "failed", "summary": "unit exited with 1"}))
    errored = format_event(_event(3, "run.execution_error", {"error_type": "RunEngineError"}))

    assert "!!" in failed and "unit exited with 1" in failed
    assert "!!" in errored and "RunEngineError" in errored


def test_evidence_says_what_it_spoke_for():
    line = format_event(
        _event(
            4,
            "evidence.recorded",
            {
                "kind": "acceptance",
                "status": "passed",
                "requirement_ids": ["req.a", "req.b"],
                "acceptance_scenario_ids": ["scenario.a"],
            },
            task_id="run.1:implement:app",
        )
    )

    assert "acceptance" in line and "passed" in line
    assert "2 req" in line and "1 scenario" in line
    assert "[app]" in line, "the node is the part of the task id worth showing"


def test_following_stops_when_the_run_settles(tmp_path):
    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.log")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="running",
    )
    store.append_event(run["id"], "run.execution_requested", {})

    finished = {"value": False}
    slept = []

    def _sleep(seconds):
        slept.append(seconds)
        # The run settles while the follower is waiting, as it would in life.
        finished["value"] = True

    lines = list(
        follow_run(
            store,
            run["id"],
            follow=True,
            sleep=_sleep,
            is_finished=lambda: finished["value"],
        )
    )

    assert any("run.execution_requested" in line for line in lines)
    assert slept, "it waited at least once rather than exiting on an empty poll"


def test_not_following_returns_what_exists_and_stops(tmp_path):
    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.log2")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="running",
    )
    for index in range(3):
        store.append_event(run["id"], f"task.step{index}", {})

    def _never(_seconds):  # pragma: no cover - reaching this is the failure
        raise AssertionError("a non-following read must not wait")

    lines = list(follow_run(store, run["id"], sleep=_never))

    assert len(lines) == 3


@pytest.mark.parametrize(
    "status,settled",
    [("succeeded", True), ("failed", True), ("canceled", True),
     ("running", False), ("ready", False)],
)
def test_settled_states_are_the_ones_that_produce_no_more_events(
    tmp_path, status, settled
):
    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id=f"project.s{status}")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status=status,
    )

    assert run_is_settled(store, run["id"]) is settled


def test_an_unreadable_store_settles_rather_than_spinning():
    class _Broken:
        def get_run(self, run_id):
            raise RuntimeError("database is gone")

    assert run_is_settled(_Broken(), "run.x") is True


def test_a_retry_says_when_and_which_attempt():
    line = format_event(
        _event(5, "task.retry_scheduled", {"next_attempt": 2, "backoff_seconds": 2})
    )

    assert "attempt 2" in line and "2s" in line
