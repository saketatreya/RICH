import json
import threading
import time

import pytest

from richbuild.compiler import CompiledArchitecture, CompiledTask
from richbuild.models import (
    AcceptanceScenario,
    ArchitectureEdge,
    ArchitectureNode,
    ArchitectureSpec,
    Contract,
    EdgeKind,
    Evidence,
    NodeKind,
    OperationContract,
    ProjectSpec,
    Requirement,
)
from richbuild.scheduler import (
    CancellationToken,
    DagScheduler,
    ProducedArtifact,
    SchedulerError,
    TaskEvidence,
    TaskPolicy,
    TaskResult,
)
from richbuild.store import RevisionConflict, RichStore, StoreError


def _task(
    node_id: str,
    order: int,
    *,
    dependencies: tuple[str, ...] = (),
) -> CompiledTask:
    return CompiledTask(
        task_id=f"implement:{node_id}",
        node_id=node_id,
        order=order,
        contract_id=f"contract:{node_id}",
        dependency_ids=dependencies,
        consumer_ids=(),
        requirement_ids=(f"requirement:{node_id}",),
        owned_paths=(f"packages/{node_id}",),
    )


def _prepared(
    tmp_path,
    tasks: tuple[CompiledTask, ...],
    *,
    run_status: str = "ready",
    task_statuses: dict[str, str] | None = None,
):
    store = RichStore(tmp_path)
    project = store.create_project("Scheduler", project_id="project.scheduler")
    requirements = tuple(
        Requirement(
            id=task.requirement_ids[0],
            title=f"Implement {task.node_id}",
            statement=f"The {task.node_id} component behaves as specified.",
        )
        for task in tasks
    )
    project_spec = ProjectSpec(
        id=project["id"],
        name="Scheduler",
        goal="Prove every scheduled component before release",
        audiences=("engineer",),
        requirements=requirements,
        acceptance_scenarios=tuple(
            AcceptanceScenario(
                id=f"scenario:{task.node_id}",
                title=f"{task.node_id} acceptance",
                when=(f"{task.node_id} is exercised",),
                then=(f"{task.node_id} satisfies its contract",),
                requirement_ids=task.requirement_ids,
                oracle=(
                    {"action": "navigate", "value": "/"},
                    {
                        "action": "assert_visible",
                        "locator": {"kind": "role", "value": "heading"},
                    },
                ),
            )
            for task in tasks
        ),
    )
    spec_revision = store.save_revision(
        project["id"],
        kind="project_spec",
        schema_version=project_spec.schema_version,
        document=project_spec.to_dict(),
        expected_revision=0,
    )
    root_node_id = tasks[-1].node_id
    contracts = tuple(
        Contract(
            id=task.contract_id,
            node_id=task.node_id,
            operations=(
                OperationContract(
                    id=f"operation:{task.node_id}",
                    name="execute",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    requirement_ids=task.requirement_ids,
                ),
            ),
        )
        for task in tasks
    )
    nodes = tuple(
        ArchitectureNode(
            id=task.node_id,
            name=task.node_id,
            kind=(
                NodeKind.APPLICATION
                if task.node_id == root_node_id
                else NodeKind.MODULE
            ),
            contract_id=task.contract_id,
            requirement_ids=task.requirement_ids,
            owned_paths=task.owned_paths,
        )
        for task in tasks
    )
    architecture = ArchitectureSpec(
        id="architecture.scheduler",
        project_id=project["id"],
        root_node_id=root_node_id,
        target_pack="nextjs-monorepo",
        nodes=nodes,
        edges=tuple(
            ArchitectureEdge(
                id=f"contains:{task.node_id}",
                kind=EdgeKind.CONTAINS,
                source_node_id=root_node_id,
                target_node_id=task.node_id,
            )
            for task in tasks
            if task.node_id != root_node_id
        ),
        contracts=contracts,
    )
    architecture_revision = store.save_revision(
        project["id"],
        kind="architecture",
        schema_version=architecture.schema_version,
        document=architecture.to_dict(),
        expected_revision=1,
    )
    run = store.create_run(
        project["id"],
        spec_revision_id=spec_revision.id,
        architecture_revision_id=architecture_revision.id,
        run_id="run.scheduler",
        status=run_status,
    )
    statuses = task_statuses or {}
    for task in tasks:
        status = statuses.get(task.node_id, "ready")
        durable_task_id = f"{run['id']}:{task.task_id}"
        store.create_task(
            run["id"],
            node_id=task.node_id,
            kind="implement",
            task_id=durable_task_id,
            status=status,
        )
        if status in {"succeeded", "cached"}:
            source = store.put_artifact(
                f"// previously verified {task.node_id}\n".encode(),
                media_type="text/plain",
            )
            store.attach_artifact(
                run["id"], source.digest, role="source", task_id=durable_task_id
            )
            result = store.put_artifact(
                json.dumps(
                    {
                        "node_id": task.node_id,
                        "status": "passed",
                        "scenario_id": f"scenario:{task.node_id}",
                    },
                    sort_keys=True,
                ).encode(),
                media_type="application/vnd.rich.evidence-result+json",
            )
            store.attach_artifact(
                run["id"],
                result.digest,
                role="evidence-result:acceptance",
                task_id=durable_task_id,
            )
            evidence = Evidence(
                id=f"evidence.{result.digest}",
                run_id=run["id"],
                task_id=durable_task_id,
                node_id=task.node_id,
                kind="acceptance",
                status="passed",
                requirement_ids=task.requirement_ids,
                acceptance_scenario_ids=(f"scenario:{task.node_id}",),
                artifact_ids=(result.digest,),
                metadata={
                    "attempt": 0,
                    "summary": "previous acceptance checks passed",
                },
            )
            record = store.put_artifact(
                (json.dumps(evidence.to_dict(), sort_keys=True) + "\n").encode(),
                media_type="application/vnd.rich.evidence+json",
            )
            store.attach_artifact(
                run["id"],
                record.digest,
                role="evidence:acceptance",
                task_id=durable_task_id,
            )
    plan = CompiledArchitecture(
        architecture_id="architecture.scheduler",
        architecture_revision=1,
        project_id=project["id"],
        project_revision=project_spec.revision,
        root_node_id=root_node_id,
        target_pack="nextjs-monorepo",
        tasks=tasks,
    )
    return store, run, plan


def _status_map(report):
    return dict(report.task_statuses)


def _verified_result(
    context,
    *,
    summary: str | None = None,
    evidence: tuple[TaskEvidence, ...] = (),
) -> TaskResult:
    return TaskResult(
        summary=summary or f"{context.node_id} verified",
        evidence=(
            *evidence,
            TaskEvidence(
                kind="acceptance",
                status="passed",
                summary=f"{context.node_id} acceptance checks passed",
                requirement_ids=context.compiled_task.requirement_ids,
                acceptance_scenario_ids=(f"scenario:{context.node_id}",),
            ),
        ),
        artifacts=(
            ProducedArtifact(
                (
                    f"// {context.node_id} verified on attempt "
                    f"{context.attempt}\n"
                ).encode(),
                role="source",
                media_type="text/plain",
            ),
        ),
    )


def test_scheduler_selects_ready_tasks_deterministically_and_bounds_parallelism(
    tmp_path,
):
    tasks = (
        _task("a", 0),
        _task("b", 1),
        _task("c", 2, dependencies=("a",)),
        _task("d", 3, dependencies=("b",)),
    )
    store, run, plan = _prepared(tmp_path, tasks)
    lock = threading.Lock()
    two_running = threading.Event()
    active = 0
    max_active = 0

    def handler(context):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                two_running.set()
        if context.node_id in {"a", "b"}:
            assert two_running.wait(1)
        time.sleep(0.01)
        with lock:
            active -= 1
        return _verified_result(
            context,
            summary=f"{context.node_id} implemented",
            evidence=(
                TaskEvidence(
                    kind="unit",
                    status="passed",
                    summary=f"{context.node_id} unit checks passed",
                ),
            ),
        )

    report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={"*": handler},
        max_workers=2,
    ).run()

    assert report.succeeded
    assert set(_status_map(report).values()) == {"succeeded"}
    assert max_active == 2
    started = [
        event["task_id"]
        for event in store.list_events(run["id"])
        if event["event_type"] == "task.started"
    ]
    assert started[:2] == [
        "run.scheduler:implement:a",
        "run.scheduler:implement:b",
    ]


def test_handler_outputs_and_evidence_are_durable_before_success(tmp_path):
    tasks = (_task("domain", 0),)
    store, run, plan = _prepared(tmp_path, tasks)

    report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={
            "domain": lambda context: TaskResult(
                summary="domain verified",
                evidence=(
                    TaskEvidence(
                        kind="acceptance",
                        status="passed",
                        summary="behavior matches the approved scenario",
                        requirement_ids=context.compiled_task.requirement_ids,
                        acceptance_scenario_ids=("scenario:domain",),
                    ),
                ),
                artifacts=(
                    ProducedArtifact(
                        b"export const value = 1;\n",
                        role="source",
                        media_type="text/typescript",
                    ),
                ),
            )
        },
    ).run()

    assert report.succeeded
    events = store.list_events(run["id"])
    evidence_events = [
        item for item in events if item["event_type"] == "evidence.recorded"
    ]
    assert [item["payload"]["kind"] for item in evidence_events] == [
        "acceptance",
        "execution",
    ]
    for event in evidence_events:
        artifact = store.get_artifact(event["payload"]["digest"])
        assert artifact.media_type == "application/vnd.rich.evidence+json"
        document = json.loads(artifact.path.read_text())
        assert document["task_id"] == "run.scheduler:implement:domain"
        assert document["metadata"]["attempt"] == 1
        result = store.get_artifact(document["artifact_ids"][0])
        assert (
            result.media_type
            == "application/vnd.rich.evidence-result+json"
        )
    event_types = [item["event_type"] for item in events]
    assert event_types.index("evidence.recorded") < event_types.index(
        "task.succeeded"
    )


def test_exhausted_failure_blocks_dependents_but_finishes_independent_work(
    tmp_path,
):
    tasks = (
        _task("failed_root", 0),
        _task("independent", 1),
        _task("dependent", 2, dependencies=("failed_root",)),
    )
    store, run, plan = _prepared(tmp_path, tasks)

    def handler(context):
        if context.node_id == "failed_root":
            raise RuntimeError("provider unavailable")
        return _verified_result(
            context, summary="independent branch complete"
        )

    report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={"*": handler},
        max_workers=2,
    ).run()

    assert report.status == "failed"
    assert _status_map(report) == {
        "failed_root": "failed",
        "independent": "succeeded",
        "dependent": "blocked",
    }
    blocked = next(
        item
        for item in store.list_events(run["id"])
        if item["event_type"] == "task.blocked"
    )
    assert blocked["payload"]["failed_dependency_ids"] == ["failed_root"]


def test_blocking_failed_evidence_prevents_false_success(tmp_path):
    tasks = (_task("semantic_check", 0),)
    store, run, plan = _prepared(tmp_path, tasks)

    report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={
            "*": lambda _: TaskResult(
                summary="process exited zero",
                evidence=(
                    TaskEvidence(
                        kind="acceptance",
                        status="failed",
                        summary="the generated behavior is semantically wrong",
                    ),
                ),
            )
        },
    ).run()

    assert report.status == "failed"
    assert _status_map(report) == {"semantic_check": "failed"}


def test_noop_handler_cannot_false_green(tmp_path):
    tasks = (_task("noop", 0),)
    store, run, plan = _prepared(tmp_path, tasks)

    report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={"*": lambda _: None},
    ).run()

    assert report.status == "failed"
    assert _status_map(report) == {"noop": "failed"}
    failure = next(
        event
        for event in store.list_events(run["id"])
        if event["event_type"] == "task.failed"
    )
    assert "without explicit passed blocking evidence" in failure["payload"][
        "summary"
    ]


def test_passed_evidence_without_source_artifact_cannot_succeed(tmp_path):
    tasks = (_task("artifactless", 0),)
    store, run, plan = _prepared(tmp_path, tasks)

    report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={
            "*": lambda _: TaskResult(
                evidence=(
                    TaskEvidence(
                        kind="unit",
                        status="passed",
                        summary="unit checks passed",
                    ),
                ),
            )
        },
    ).run()

    assert report.status == "failed"
    assert _status_map(report) == {"artifactless": "failed"}
    failure = next(
        event
        for event in store.list_events(run["id"])
        if event["event_type"] == "task.failed"
    )
    assert "required durable artifacts are missing" in failure["payload"][
        "summary"
    ]


def test_policy_requires_explicit_acceptance_scenario_coverage(tmp_path):
    tasks = (_task("policy", 0),)
    store, run, plan = _prepared(tmp_path, tasks)

    report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={
            "*": lambda _: TaskResult(
                evidence=(
                    TaskEvidence(
                        kind="unit",
                        status="passed",
                        summary="unit checks passed",
                    ),
                ),
                artifacts=(ProducedArtifact(b"source\n", role="source"),),
            )
        },
        default_policy=TaskPolicy(
            required_acceptance_scenario_ids=("scenario:policy",)
        ),
    ).run()

    assert report.status == "failed"
    assert _status_map(report) == {"policy": "failed"}
    failure = next(
        event
        for event in store.list_events(run["id"])
        if event["event_type"] == "task.failed"
    )
    assert "scenario:policy" in failure["payload"]["summary"]


def test_release_traceability_blocks_uncovered_project_scenario(tmp_path):
    tasks = (_task("trace", 0),)
    store, run, plan = _prepared(tmp_path, tasks)

    report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={
            "*": lambda _: TaskResult(
                evidence=(
                    TaskEvidence(
                        kind="unit",
                        status="passed",
                        summary="unit checks passed",
                    ),
                ),
                artifacts=(ProducedArtifact(b"source\n", role="source"),),
            )
        },
    ).run()

    assert report.status == "failed"
    assert _status_map(report) == {"trace": "succeeded"}
    release_failure = next(
        event
        for event in store.list_events(run["id"])
        if event["event_type"] == "run.release_validation_failed"
    )
    assert "acceptance evidence does not cover" in release_failure["payload"][
        "message"
    ]


def test_failed_attempt_retries_and_restart_recovers_durable_running_task(
    tmp_path,
):
    tasks = (
        _task("done", 0),
        _task("resumed", 1, dependencies=("done",)),
        _task("after", 2, dependencies=("resumed",)),
    )
    store, run, plan = _prepared(
        tmp_path,
        tasks,
        run_status="running",
        task_statuses={"done": "succeeded", "resumed": "running"},
    )
    resumed_id = "run.scheduler:implement:resumed"
    store.set_task_status(
        resumed_id,
        "running",
        expected_status="running",
        increment_attempt=True,
    )
    calls: list[tuple[str, int]] = []

    def handler(context):
        calls.append((context.node_id, context.attempt))
        if context.node_id == "resumed" and context.attempt == 2:
            return TaskResult(
                succeeded=False, summary="transient model response"
            )
        return _verified_result(context, summary="recovered")

    report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={"*": handler},
        default_policy=TaskPolicy(max_attempts=3),
    ).run()

    assert report.succeeded
    assert calls == [("resumed", 2), ("resumed", 3), ("after", 1)]
    assert dict(report.task_attempts) == {"done": 0, "resumed": 3, "after": 1}
    event_types = [
        item["event_type"] for item in store.list_events(run["id"])
    ]
    assert "task.interrupted" in event_types
    assert event_types.count("task.retry_scheduled") == 2


def test_timeout_requests_cooperative_stop_and_blocks_dependent(tmp_path):
    tasks = (
        _task("slow", 0),
        _task("after", 1, dependencies=("slow",)),
    )
    store, run, plan = _prepared(tmp_path, tasks)
    observed_cancellation = threading.Event()

    def slow(context):
        assert context.deadline_monotonic is not None
        assert context.deadline_monotonic > time.monotonic()
        assert context.remaining_seconds is not None
        assert 0 < context.remaining_seconds <= 0.03
        assert context.wait_for_cancellation(1)
        observed_cancellation.set()
        return TaskResult(summary="late output")

    started = time.monotonic()
    report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={"*": slow},
        default_policy=TaskPolicy(max_attempts=1, timeout_seconds=0.03),
        cancellation_grace_seconds=0.2,
    ).run()

    assert time.monotonic() - started < 0.5
    assert observed_cancellation.wait(0.2)
    assert report.status == "failed"
    assert _status_map(report) == {"slow": "failed", "after": "blocked"}
    failure_evidence = next(
        item
        for item in store.list_events(run["id"])
        if item["event_type"] == "evidence.recorded"
        and item["payload"]["status"] == "error"
    )
    assert "deadline" in failure_evidence["payload"]["summary"]


def test_uncooperative_timeout_fails_closed_without_exceeding_worker_bound(
    tmp_path,
):
    tasks = (_task("stuck", 0), _task("other", 1))
    store, run, plan = _prepared(tmp_path, tasks)
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0

    def handler(_context):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        release.wait(0.5)
        with lock:
            active -= 1
        return TaskResult()

    report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={"*": handler},
        max_workers=1,
        default_policy=TaskPolicy(max_attempts=2, timeout_seconds=0.01),
        cancellation_grace_seconds=0.02,
    ).run()
    release.set()

    assert report.status == "failed"
    assert max_active == 1
    assert set(_status_map(report).values()) <= {
        "failed",
        "blocked",
        "canceled",
    }
    assert any(
        item["event_type"] == "scheduler.uncooperative_handlers"
        for item in store.list_events(run["id"])
    )


def test_cancellation_marks_running_and_unstarted_tasks_durably(tmp_path):
    tasks = (
        _task("active", 0),
        _task("after", 1, dependencies=("active",)),
    )
    store, run, plan = _prepared(tmp_path, tasks)
    token = CancellationToken()
    started = threading.Event()
    report_box = []

    def handler(context):
        started.set()
        context.wait_for_cancellation(1)
        return TaskResult(summary="ignored after cancellation")

    def execute():
        report_box.append(
            DagScheduler(
                store,
                run_id=run["id"],
                plan=plan,
                handlers={"*": handler},
            ).run(token)
        )

    worker = threading.Thread(target=execute)
    worker.start()
    assert started.wait(1)
    token.cancel("founder stopped the run")
    worker.join(1)

    assert not worker.is_alive()
    report = report_box[0]
    assert report.status == "canceled"
    assert _status_map(report) == {"active": "canceled", "after": "canceled"}
    canceled = [
        item
        for item in store.list_events(run["id"])
        if item["event_type"] == "task.canceled"
    ]
    assert {item["payload"]["reason"] for item in canceled} == {
        "founder stopped the run"
    }


def _claim_once_expired(store, run_id, timeout=10.0):
    """Take ownership as soon as the incumbent lease lapses.

    Sleeping for a fixed interval and hoping guesses at wall-clock timing the
    test does not control. Polling states the actual precondition: the takeover
    happens the moment expiry makes it legal.
    """

    deadline = time.monotonic() + timeout
    while True:
        try:
            return store.claim_run_execution(run_id)
        except StoreError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def test_successor_owner_is_the_only_scheduler_that_can_complete(tmp_path):
    tasks = (_task("owned", 0),)
    store, run, plan = _prepared(tmp_path, tasks)
    first_started = threading.Event()
    release_first = threading.Event()
    first_errors: list[BaseException] = []

    def first_handler(context):
        first_started.set()
        # Generous: the test releases this explicitly, so the timeout is only a
        # safety net against a hung run, never a claim about how fast the
        # successor finishes.
        release_first.wait(30)
        return _verified_result(context, summary="stale owner output")

    # Claim a comfortable lease and collapse it below, once the handler has
    # definitely started. Racing a 30ms lease against thread startup made this
    # test flake on a loaded machine: _launch performs a fenced write before it
    # dispatches, so an expiry that lands first stops the handler ever running.
    first_lease = store.claim_run_execution(
        run["id"],
        lease_seconds=30,
    )
    first_scheduler = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={"*": first_handler},
        default_policy=TaskPolicy(max_attempts=2),
        owner_token=first_lease.owner_token,
    )

    def execute_first():
        try:
            first_scheduler.run()
        except BaseException as exc:
            first_errors.append(exc)

    first_thread = threading.Thread(target=execute_first)
    first_thread.start()
    assert first_started.wait(10), "the first scheduler never reached its handler"
    store.renew_run_execution(
        run["id"],
        owner_token=first_lease.owner_token,
        lease_seconds=0.01,
    )
    successor = _claim_once_expired(store, run["id"])

    # Scheduler-level cancellation, evidence publication, and finalization all
    # pass through the same fenced store boundary.
    with pytest.raises(RevisionConflict, match="ownership was lost"):
        first_scheduler._cancel_remaining("stale owner cancellation")
    with pytest.raises(RevisionConflict, match="ownership was lost"):
        first_scheduler._record_evidence(
            tasks[0],
            1,
            TaskEvidence(
                kind="unit",
                status="passed",
                summary="stale evidence",
            ),
        )
    with pytest.raises(RevisionConflict, match="ownership was lost"):
        first_scheduler._finish("failed")

    successor_report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={"*": _verified_result},
        default_policy=TaskPolicy(max_attempts=2),
        owner_token=successor.owner_token,
    ).run()

    assert successor_report.succeeded
    assert dict(successor_report.task_attempts) == {"owned": 2}
    release_first.set()
    first_thread.join(30)
    assert not first_thread.is_alive()
    # Two orderings are legal and which one happens is a race: the stale owner
    # either loses its lease on a fenced write, or reaches _finish after the
    # successor has already marked the run terminal and correctly finds nothing
    # left to write. Asserting one of them was asserting the race. What must
    # hold either way is that it never fails in some other manner, and never
    # changes anything -- which is what the durable checks below establish.
    assert all(
        isinstance(error, RevisionConflict) for error in first_errors
    ), first_errors

    events = store.list_events(run["id"])
    assert sum(
        event["event_type"] == "scheduler.completed" for event in events
    ) == 1
    assert not any(event["event_type"] == "task.canceled" for event in events)
    assert "stale owner output" not in str(events)
    assert store.get_run(run["id"])["status"] == "succeeded"
    assert store.get_task("run.scheduler:implement:owned")["status"] == "succeeded"


def test_plan_mismatch_fails_before_mutating_run(tmp_path):
    tasks = (_task("only", 0),)
    store, run, plan = _prepared(tmp_path, tasks)
    bad_plan = CompiledArchitecture(
        architecture_id=plan.architecture_id,
        architecture_revision=1,
        project_id="project.someone_else",
        project_revision=1,
        root_node_id="only",
        target_pack=plan.target_pack,
        tasks=tasks,
    )

    try:
        DagScheduler(
            store,
            run_id=run["id"],
            plan=bad_plan,
            handlers={"*": lambda _: None},
        )
    except SchedulerError as exc:
        assert "different project" in str(exc)
    else:
        raise AssertionError("project mismatch was accepted")
    assert store.get_run(run["id"])["status"] == "ready"


def test_evidence_events_carry_the_requirements_they_speak_for(tmp_path):
    """The control plane answers "is this requirement proven, and by what?"
    from the event stream. Without these ids that needs one artifact fetch per
    piece of evidence, and the question stops getting asked."""

    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.trace")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="running",
    )
    store.append_event(
        run["id"],
        "evidence.recorded",
        {
            "kind": "acceptance",
            "status": "passed",
            "summary": "acceptance command passed",
            "requirement_ids": ["req.checklist"],
            "acceptance_scenario_ids": ["scenario.checklist"],
        },
    )

    (event,) = [
        item
        for item in store.list_events(run["id"])
        if item["event_type"] == "evidence.recorded"
    ]

    assert event["payload"]["requirement_ids"] == ["req.checklist"]
    assert event["payload"]["acceptance_scenario_ids"] == ["scenario.checklist"]


def _acceptance_failure(context, *, attributed):
    return TaskResult(
        summary="acceptance failed",
        evidence=(
            TaskEvidence(
                kind="acceptance",
                status="failed",
                summary="two steps failed on pages another task owns",
                requirement_ids=context.compiled_task.requirement_ids,
                attributed_node_ids=attributed,
            ),
        ),
    )


def _events_by_type(store, run_id):
    grouped: dict[str, list] = {}
    for event in store.list_events(run_id):
        grouped.setdefault(event["event_type"], []).append(event)
    return grouped


def test_acceptance_failure_reopens_the_owner_not_the_root(tmp_path):
    tasks = (_task("web", 0), _task("app", 1, dependencies=("web",)))
    store, run, plan = _prepared(tmp_path, tasks)
    calls: list[tuple[str, int]] = []

    def handler(context):
        calls.append((context.node_id, context.attempt))
        if context.node_id == "app" and context.attempt == 1:
            return _acceptance_failure(context, attributed=("web",))
        return _verified_result(context)

    report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={"*": handler},
        default_policy=TaskPolicy(max_attempts=3),
    ).run()

    assert report.succeeded
    assert calls == [("web", 1), ("app", 1), ("web", 2), ("app", 2)]
    assert dict(report.task_attempts) == {"web": 2, "app": 2}
    events = _events_by_type(store, run["id"])
    (reopened,) = events["task.reopened"]
    assert reopened["task_id"] == f"{run['id']}:implement:web"
    assert reopened["payload"]["failed_node_id"] == "app"
    assert reopened["payload"]["failed_attempt"] == 1
    assert reopened["payload"]["next_attempt"] == 2
    (superseded,) = events["task.superseded"]
    assert superseded["task_id"] == f"{run['id']}:implement:app"
    assert superseded["payload"]["reopened_node_ids"] == ["web"]
    (failed,) = events["task.failed"]
    assert failed["payload"]["will_retry"] is False
    assert failed["payload"]["reopened_node_ids"] == ["web"]
    recorded = [
        event
        for event in events["evidence.recorded"]
        if event["payload"]["kind"] == "acceptance"
        and event["payload"]["status"] == "failed"
    ]
    assert recorded[0]["payload"]["attributed_node_ids"] == ["web"]
    # The reopen is a retry as far as deadlines are concerned, so a resumed
    # scheduler restores it the same way.
    assert [
        event["payload"].get("reason")
        for event in events["task.retry_scheduled"]
    ] == ["reopened"]


def test_exhausted_owner_withholds_the_root_retry(tmp_path):
    tasks = (_task("web", 0), _task("app", 1, dependencies=("web",)))
    store, run, plan = _prepared(tmp_path, tasks)
    calls: list[tuple[str, int]] = []

    def handler(context):
        calls.append((context.node_id, context.attempt))
        if context.node_id == "app":
            return _acceptance_failure(context, attributed=("web",))
        return _verified_result(context)

    report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={"*": handler},
        default_policy=TaskPolicy(max_attempts=1),
    ).run()

    assert report.status == "failed"
    assert calls == [("web", 1), ("app", 1)]
    assert _status_map(report) == {"web": "succeeded", "app": "failed"}
    events = _events_by_type(store, run["id"])
    (withheld,) = events["task.retry_withheld"]
    assert withheld["payload"]["exhausted_node_ids"] == ["web"]
    assert "task.reopened" not in events
    assert "task.retry_scheduled" not in events


def test_attribution_to_a_stranger_falls_back_to_the_plain_retry(tmp_path):
    tasks = (
        _task("web", 0),
        _task("aside", 1),
        _task("app", 2, dependencies=("web",)),
    )
    store, run, plan = _prepared(tmp_path, tasks)
    calls: list[tuple[str, int]] = []

    def handler(context):
        calls.append((context.node_id, context.attempt))
        if context.node_id == "app" and context.attempt == 1:
            return _acceptance_failure(context, attributed=("aside", "ghost"))
        return _verified_result(context)

    report = DagScheduler(
        store,
        run_id=run["id"],
        plan=plan,
        handlers={"*": handler},
        default_policy=TaskPolicy(max_attempts=2),
    ).run()

    assert report.succeeded
    assert calls == [("web", 1), ("aside", 1), ("app", 1), ("app", 2)]
    events = _events_by_type(store, run["id"])
    (ignored,) = events["task.attribution_ignored"]
    assert ignored["payload"]["node_ids"] == ["aside", "ghost"]
    assert "task.reopened" not in events
    assert "task.superseded" not in events
