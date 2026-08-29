"""The trusted construction of a run's execution path.

``execution.py`` binds the pieces that make a run restart-safe: an event sink that
writes only under the lease that owns the run, a cancellation token that also reads
the durable record, and an executor that refuses to guess which route pays.
"""

import pytest

from richbuild.execution import (
    DefaultRunExecutor,
    _DurableCancellation,
    _LeaseBoundModelEventSink,
)
from richbuild.store import RichStore, StoreError


def _run(store):
    project = store.create_project("Demo", project_id="project.demo")
    return store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="ready",
    )


def test_model_event_sink_refuses_to_write_without_a_bound_owner(tmp_path):
    store = RichStore(tmp_path)
    run = _run(store)
    sink = _LeaseBoundModelEventSink(store, run["id"])

    with pytest.raises(StoreError, match="no bound execution owner"):
        sink("model.attempt.started", {"run_id": run["id"], "task_id": "task"})
    assert store.list_events(run["id"]) == []


def test_model_event_sink_checks_the_run_and_task_before_the_owner(tmp_path):
    store = RichStore(tmp_path)
    run = _run(store)
    sink = _LeaseBoundModelEventSink(store, run["id"])

    with pytest.raises(ValueError, match="different run"):
        sink("model.attempt.started", {"run_id": "run_other", "task_id": "task"})
    with pytest.raises(ValueError, match="no durable task id"):
        sink("model.attempt.started", {"run_id": run["id"]})


def test_model_event_sink_binds_exactly_one_owner(tmp_path):
    store = RichStore(tmp_path)
    run = _run(store)
    sink = _LeaseBoundModelEventSink(store, run["id"])

    sink.bind("owner-a")
    sink.bind("owner-a")
    with pytest.raises(RuntimeError, match="already bound"):
        sink.bind("owner-b")
    with pytest.raises(ValueError):
        sink.bind("")


def test_model_event_sink_write_is_fenced_by_the_store(tmp_path):
    """A token nobody leased is refused at the store, not trusted by the sink."""

    store = RichStore(tmp_path)
    run = _run(store)
    sink = _LeaseBoundModelEventSink(store, run["id"])
    sink.bind("owner-nobody-leased")

    with pytest.raises(StoreError):
        sink("model.attempt.started", {"run_id": run["id"], "task_id": "task"})
    assert store.list_events(run["id"]) == []


def test_durable_cancellation_reads_the_standing_request(tmp_path):
    store = RichStore(tmp_path)
    run = _run(store)
    token = _DurableCancellation(store, run["id"])

    assert token.is_cancelled is False
    store.request_run_cancellation(run["id"], reason="stop at the checkpoint")
    # The poll is throttled to once a second on the hot path; wind it back so
    # the test does not have to wait for the throttle.
    token._checked_at = 0.0
    assert token.is_cancelled is True
    # And once cancelled, it stays cancelled without asking the store again.
    assert token.is_cancelled is True


def test_durable_cancellation_never_fails_a_run_on_a_store_error(tmp_path):
    class BrokenStore:
        def run_cancellation(self, run_id):
            raise RuntimeError("database on fire")

    token = _DurableCancellation.__new__(_DurableCancellation)
    _DurableCancellation.__init__(token, RichStore(tmp_path), "run_x")
    token._store = BrokenStore()
    token._checked_at = 0.0

    assert token.is_cancelled is False


def test_executor_refuses_an_unknown_route_and_a_wrong_store(tmp_path):
    store = RichStore(tmp_path)

    with pytest.raises(ValueError, match="route must be one of"):
        DefaultRunExecutor(store, route="carrier-pigeon")
    with pytest.raises(TypeError, match="store must be a RichStore"):
        DefaultRunExecutor(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="runtime_builder must be callable"):
        DefaultRunExecutor(store, runtime_builder="not callable")  # type: ignore[arg-type]


def test_availability_reports_a_reason_or_none_and_never_raises(tmp_path, monkeypatch):
    """The probe is asked before a run is accepted, so it must answer, not
    raise: None when this host can run a sandbox, a sentence that names the
    remedy when it cannot. The first version raised NameError at runtime
    because no test ever called the real thing."""
    from richbuild import executor as executor_module

    store = RichStore(tmp_path)
    verdict = DefaultRunExecutor(store).availability()
    assert verdict is None or (isinstance(verdict, str) and verdict)

    monkeypatch.setattr(executor_module.BubblewrapExecutor, "available", lambda self: False)
    reason = DefaultRunExecutor(store).availability()
    assert reason is not None and "Bubblewrap" in reason and "rich doctor" in reason


def test_availability_names_a_toolchain_that_drifted(tmp_path, monkeypatch):
    """The M3 drive found this too: the host's Node had moved a patch version and
    the run died after acceptance as SandboxUnavailable. The probe resolves the
    toolchain the way a run does, so the drift is a refusal that names it."""
    from richbuild import execution as execution_module
    from richbuild.executor import SandboxUnavailable

    def drifted():
        raise SandboxUnavailable("trusted Node version mismatch: expected 22.23.2, found 23.0.0")

    # The sandbox is probed first; on a host that refuses user namespaces (a
    # CI runner did) its reason would be the one returned, and this test is
    # about the toolchain's.
    monkeypatch.setattr(execution_module, "sandbox_availability", lambda: None)
    monkeypatch.setattr(execution_module, "trusted_node_pnpm_runtime", drifted)
    reason = DefaultRunExecutor(RichStore(tmp_path)).availability()
    assert reason is not None
    assert "expected 22.23.2, found 23.0.0" in reason and "rich doctor" in reason


def test_the_cache_root_lives_beside_the_workspaces(tmp_path):
    """One shared cache per state directory, so a second build installs from
    what the first one downloaded. Captured at the runtime builder, which is
    the one place both the bootstrap and the gates learn it from."""
    from richbuild.execution import DefaultRunExecutor

    store = RichStore(tmp_path / "state")
    run = _run(store)
    captured = {}

    class Stop(Exception):
        pass

    def builder(budget, *, event_history, event_sink, route, cache_root):
        captured["cache_root"] = cache_root
        raise Stop()

    workspace = tmp_path / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    with pytest.raises(Stop):
        DefaultRunExecutor(store, runtime_builder=builder).execute(run_id=run["id"], workspace=workspace)
    assert captured["cache_root"] == (tmp_path / "state").parent / "cache"
