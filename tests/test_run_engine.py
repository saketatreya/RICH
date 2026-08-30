import json
from decimal import Decimal
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

import richbuild.coding as coding_module
from richbuild.budget import BudgetLedger, RunBudget, Usage
from richbuild.coding import ApprovalWitness, CodingWorker
from richbuild.compiler import CompiledTask, compile_architecture
from richbuild.execution import (
    DefaultRunExecutor,
    _LeaseBoundModelEventSink,
)
from richbuild.executor import BubblewrapExecutor, ExecutionResult
from richbuild.models import (
    AcceptanceScenario,
    ArchitectureEdge,
    ArchitectureNode,
    ArchitectureSpec,
    Contract,
    EdgeKind,
    NodeKind,
    OperationContract,
    ProjectSpec,
    Requirement,
)
from richbuild.providers import (
    MODEL_ATTEMPT_EVENT_SCHEMA,
    ModelGateway,
    ModelResponse,
    recover_model_usage,
)
from richbuild.run_engine import (
    AcceptanceCoverageContext,
    ApprovalValidationError,
    BubblewrapCommandRunner,
    ConcurrentExecutionError,
    RunEngine,
    RunEngineConfig,
    VerificationCommand,
    WorkspaceValidationError,
    _DurableSourceTransactionSink,
    _recover_prepared_source_transactions,
)
from richbuild.scheduler import CancellationToken
from richbuild.store import RichStore, StoreError
from richbuild.target_packs.nextjs import (
    NextJsTargetPack,
    NextJsTargetPackConfig,
)


class FakeModelProvider:
    name = "fake"

    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        payload = {
            "summary": "Implemented the approved application behavior",
            "files": [
                {
                    "operation": "create",
                    "path": "apps/web/generated.ts",
                    "content": (
                        "export const approvedBehavior = "
                        '\"verified outside the model\";\n'
                    ),
                }
            ],
        }
        return ModelResponse(
            text=json.dumps(payload),
            parsed=payload,
            provider=self.name,
            model=request.model,
            usage=Usage(
                model_attempts=1,
                input_tokens=200,
                output_tokens=100,
                cost_usd=Decimal("0.01"),
                execution_seconds=0.05,
            ),
            provider_request_id=f"fake-{len(self.requests)}",
        )


class BlockingModelProvider(FakeModelProvider):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def generate(self, request):
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("test provider was not released")
        return super().generate(request)


class PassingCommandRunner:
    def __init__(self):
        self.commands: list[VerificationCommand] = []

    def run(
        self,
        workspace,
        command,
        *,
        cancellation=None,
        deadline=None,
    ):
        self.commands.append(command)
        assert workspace.is_dir()
        assert cancellation is None or callable(cancellation)
        assert deadline is None or deadline > 0
        coverage = ""
        if command.kind == "acceptance":
            assert command.acceptance_context is not None
            coverage = "RICH_ACCEPTANCE_COVERAGE " + json.dumps(
                {
                    "schema_version": "rich.acceptance-coverage/v1",
                    "context": command.acceptance_context.to_dict(),
                    "scenario_ids": sorted(
                        command.expected_acceptance_scenario_ids
                    ),
                },
                sort_keys=True,
            ) + "\n"
        return ExecutionResult(
            argv=command.argv,
            returncode=0,
            stdout=f"{command.kind} passed\n{coverage}",
            stderr="",
            duration_seconds=0.01,
        )


class NoVerificationRunner:
    def __init__(self):
        self.commands: list[VerificationCommand] = []

    def run(
        self,
        _workspace,
        command,
        *,
        cancellation=None,
        deadline=None,
    ):
        self.commands.append(command)
        return None


class UnattestedAcceptanceRunner(PassingCommandRunner):
    def run(self, workspace, command, **controls):
        result = super().run(workspace, command, **controls)
        if command.kind != "acceptance":
            return result
        return ExecutionResult(
            argv=result.argv,
            returncode=0,
            stdout="acceptance process exited successfully without a report\n",
            stderr="",
            duration_seconds=result.duration_seconds,
        )


class ForgedAcceptanceRunner(PassingCommandRunner):
    def run(self, workspace, command, **controls):
        result = super().run(workspace, command, **controls)
        if command.kind != "acceptance":
            return result
        assert command.acceptance_context is not None
        forged = {
            "schema_version": "rich.acceptance-coverage/v1",
            "context": {
                **command.acceptance_context.to_dict(),
                "nonce": "0" * 64,
            },
            "scenario_ids": list(
                command.expected_acceptance_scenario_ids
            ),
        }
        return ExecutionResult(
            argv=result.argv,
            returncode=0,
            stdout=f"RICH_ACCEPTANCE_COVERAGE {json.dumps(forged)}\n",
            stderr="",
            duration_seconds=result.duration_seconds,
        )


class SourceMutatingRunner(PassingCommandRunner):
    def run(self, workspace, command, **controls):
        result = super().run(workspace, command, **controls)
        if command.kind == "build":
            (workspace / "apps/web/src/app/page.tsx").write_text(
                "export default function ForgedAfterCheck() { return null }\n"
            )
        return result


def _term_ignoring_verification_argv(tmp_path, prefix):
    started = tmp_path / f"{prefix}.started"
    child_pid = tmp_path / f"{prefix}-child.pid"
    late_marker = tmp_path / f"{prefix}-late"
    child_script = "\n".join(
        (
            "import os",
            "import signal",
            "import time",
            "from pathlib import Path",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            f"Path({str(child_pid)!r}).write_text(str(os.getpid()))",
            "time.sleep(0.6)",
            f"Path({str(late_marker)!r}).write_text('late')",
        )
    )
    script = "\n".join(
        (
            "import signal",
            "import subprocess",
            "import sys",
            "import time",
            "from pathlib import Path",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            f"subprocess.Popen([sys.executable, '-c', {child_script!r}])",
            f"child = Path({str(child_pid)!r})",
            "deadline = time.monotonic() + 2",
            "while not child.exists() and time.monotonic() < deadline:",
            "    time.sleep(0.01)",
            f"Path({str(started)!r}).write_text('started')",
            "time.sleep(10)",
        )
    )
    return (
        ("/usr/bin/python3", "-c", script),
        started,
        child_pid,
        late_marker,
    )


def _direct_process_command_runner(monkeypatch):
    executor = BubblewrapExecutor(
        executable="/usr/bin/python3",
        poll_interval_seconds=0.01,
        termination_grace_seconds=0.05,
    )

    def direct_command(workspace, received_argv, policy):
        assert Path(workspace).is_dir()
        policy.validate()
        return list(received_argv)

    monkeypatch.setattr(executor, "command", direct_command)
    return BubblewrapCommandRunner(executor, timeout_seconds=5)


def _gateway(provider, *, event_sink=None):
    return ModelGateway(
        [provider],
        BudgetLedger(
            RunBudget(
                max_model_attempts=8,
                max_input_tokens=100_000,
                max_output_tokens=100_000,
                max_cost_usd=Decimal("20"),
                max_execution_seconds=2_000,
            )
        ),
        event_sink=event_sink,
    )


def _prepared_state(tmp_path, *, max_model_attempts=8):
    store = RichStore(tmp_path / "state")
    project_record = store.create_project(
        "Verified public build", project_id="project.public-build"
    )
    project = ProjectSpec(
        id=project_record["id"],
        name="Verified public build",
        goal="Publish behavior only after an independent acceptance run",
        audiences=("developer",),
        requirements=(
            Requirement(
                id="requirement.behavior",
                title="Approved behavior",
                statement="The application exposes the approved behavior.",
            ),
        ),
        acceptance_scenarios=(
            AcceptanceScenario(
                id="scenario.behavior",
                title="Approved behavior is visible",
                given=("The approved application has been built.",),
                when=("A user opens the application.",),
                then=("The approved behavior is visible.",),
                requirement_ids=("requirement.behavior",),
                oracle=(
                    {"action": "open_requirement"},
                    {
                        "action": "assert_visible",
                        "locator": {
                            "kind": "text",
                            "value": "The application exposes the approved behavior.",
                        },
                    },
                ),
            ),
        ),
    )
    spec_revision = store.save_revision(
        project.id,
        kind="product_spec",
        schema_version=project.schema_version,
        document=project.to_dict(),
        expected_revision=0,
    )
    node = ArchitectureNode(
        id="app",
        name="Application",
        kind=NodeKind.APPLICATION,
        contract_id="contract.app",
        requirement_ids=("requirement.behavior",),
        owned_paths=(
            ".rich/generated",
            "apps/web",
            "packages/contracts",
            "packages/domain",
            "packages/ui",
        ),
    )
    contract = Contract(
        id="contract.app",
        node_id=node.id,
        operations=(
            OperationContract(
                id="operation.app.behavior",
                name="showBehavior",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                requirement_ids=("requirement.behavior",),
            ),
        ),
    )
    architecture = ArchitectureSpec(
        id="architecture.public-build",
        project_id=project.id,
        root_node_id=node.id,
        target_pack="nextjs-app-router",
        nodes=(node,),
        edges=(),
        contracts=(contract,),
        project_spec_revision=project.revision,
    )
    architecture_revision = store.save_revision(
        project.id,
        kind="architecture",
        schema_version=architecture.schema_version,
        document=architecture.to_dict(),
        expected_revision=1,
    )
    approval = store.request_approval(
        project.id,
        gate="architecture",
        request={
            "revision_id": architecture_revision.id,
            "spec_revision_id": spec_revision.id,
            "target_pack": architecture.target_pack,
            "node_ids": sorted(architecture.node_index),
        },
    )
    approval = store.decide_approval(
        approval["id"],
        approved=True,
        decision={"actor": "test-approver"},
    )
    run = store.create_run(
        project.id,
        spec_revision_id=spec_revision.id,
        architecture_revision_id=architecture_revision.id,
        run_id="run.public-build",
        status="ready",
        budget={
            "max_model_attempts": max_model_attempts,
            "max_input_tokens": 100_000,
            "max_output_tokens": 100_000,
            "max_cost_usd": "20",
            "max_execution_seconds": 2_000,
        },
    )
    plan = compile_architecture(architecture, project)
    for task in plan.tasks:
        store.create_task(
            run["id"],
            node_id=task.node_id,
            kind="implement",
            task_id=f"{run['id']}:{task.task_id}",
            status="ready",
            dependency_task_ids=tuple(
                f"{run['id']}:implement:{dependency_id}"
                for dependency_id in task.dependency_ids
            ),
        )

    workspace = tmp_path / "workspace"
    target_pack = NextJsTargetPack(
        NextJsTargetPackConfig(
            project_name="verified-fixture",
            project_spec=project,
            architecture=architecture,
        )
    )
    target_pack.scaffold(workspace)
    manifest = store.put_artifact(
        (workspace / ".rich/target-pack.json").read_bytes(),
        media_type="application/vnd.rich.target-pack-manifest+json",
    )
    store.attach_artifact(
        run["id"], manifest.digest, role="scaffold_manifest"
    )
    store.append_event(
        run["id"],
        "run.prepared",
        {
            "architecture_approval_id": approval["id"],
            "task_count": len(plan.tasks),
        },
    )
    store.append_event(
        run["id"],
        "scaffold.completed",
        {
            "destination": str(workspace.absolute()),
            "manifest_digest": manifest.digest,
        },
    )
    return {
        "store": store,
        "project": project,
        "architecture": architecture,
        "approval": approval,
        "run": run,
        "plan": plan,
        "workspace": workspace,
    }


def _engine(state, provider, runner, *, max_task_attempts=1):
    return RunEngine(
        state["store"],
        gateway=_gateway(provider),
        command_runner=runner,
        provider=provider.name,
        model="fake-code-model",
        config=RunEngineConfig(max_task_attempts=max_task_attempts),
    )


def test_acceptance_context_uses_one_shot_file_not_inherited_secret(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = BubblewrapExecutor(executable="/usr/bin/bwrap")
    observed = {}
    context = AcceptanceCoverageContext(
        run_id="run.context",
        task_id="run.context:acceptance",
        attempt=1,
        nonce="a" * 64,
    )

    def fake_run(
        received_workspace,
        argv,
        policy,
        *,
        cancellation=None,
        deadline=None,
    ):
        observed["policy"] = policy
        guest_path = policy.environment[
            "RICH_ACCEPTANCE_CONTEXT_FILE"
        ]
        host_path = received_workspace / guest_path.removeprefix(
            "/workspace/"
        )
        observed["context"] = json.loads(host_path.read_text())
        host_path.unlink()
        report = {
            "schema_version": "rich.acceptance-coverage/v1",
            "context": context.to_dict(),
            "scenario_ids": ["scenario.context"],
        }
        return ExecutionResult(
            argv=tuple(argv),
            returncode=0,
            stdout=f"RICH_ACCEPTANCE_COVERAGE {json.dumps(report)}\n",
            stderr="",
            duration_seconds=0.01,
        )

    monkeypatch.setattr(executor, "run", fake_run)
    result = BubblewrapCommandRunner(executor).run(
        workspace,
        VerificationCommand(
            "acceptance",
            ("pnpm", "run", "test:e2e"),
            expected_acceptance_scenario_ids=("scenario.context",),
            acceptance_context=context,
        ),
    )

    assert result.passed
    assert observed["context"] == context.to_dict()
    environment = observed["policy"].environment
    assert "RICH_ACCEPTANCE_CONTEXT" not in environment
    assert context.nonce not in repr(environment)
    assert not list((workspace / "test-results").iterdir())


def test_execute_generates_then_independently_verifies_release(tmp_path):
    state = _prepared_state(tmp_path)
    provider = FakeModelProvider()
    runner = PassingCommandRunner()

    report = _engine(state, provider, runner).execute(
        run_id=state["run"]["id"],
        workspace=state["workspace"],
        architecture_approval_id=state["approval"]["id"],
    )

    assert report.succeeded
    assert state["store"].get_run(state["run"]["id"])["status"] == "succeeded"
    assert [command.kind for command in runner.commands] == [
        "lint",
        "static",
        "unit",
        "build",
        "acceptance",
    ]
    assert runner.commands[-1].expected_acceptance_scenario_ids == (
        "scenario.behavior",
    )
    assert len(provider.requests) == 1
    roles = {
        record["role"]
        for record in state["store"].list_run_artifacts(state["run"]["id"])
    }
    assert "generated-source" in roles
    assert {
        "verification:static",
        "verification:unit",
        "verification:acceptance",
        "source:release-snapshot",
        "evidence:static",
        "evidence:unit",
        "evidence:acceptance",
    } <= roles
    acceptance_records = [
        record
        for record in state["store"].list_run_artifacts(state["run"]["id"])
        if record["role"] == "evidence:acceptance"
    ]
    evidence_document = json.loads(
        state["store"]
        .get_artifact(acceptance_records[-1]["digest"])
        .path.read_text()
    )
    assert evidence_document["blocking"] is True
    assert evidence_document["status"] == "passed"
    assert evidence_document["acceptance_scenario_ids"] == [
        "scenario.behavior"
    ]


def test_generation_cannot_succeed_when_verification_is_not_observed(tmp_path):
    state = _prepared_state(tmp_path)
    provider = FakeModelProvider()
    runner = NoVerificationRunner()

    report = _engine(state, provider, runner).execute(
        run_id=state["run"]["id"],
        workspace=state["workspace"],
    )

    assert not report.succeeded
    assert report.status == "failed"
    task = state["store"].list_tasks(state["run"]["id"])[0]
    assert task["status"] == "failed"
    assert (state["workspace"] / "apps/web/generated.ts").is_file()
    assert any(
        event["event_type"] == "evidence.recorded"
        and event["payload"]["kind"] == "static"
        and event["payload"]["status"] == "error"
        for event in state["store"].list_events(state["run"]["id"])
    )
    assert [command.kind for command in runner.commands] == [
        "lint",
        "static",
        "unit",
        "build",
        "acceptance",
    ]
    assert not any(
        event["event_type"] == "evidence.recorded"
        and event["payload"]["kind"] == "acceptance"
        and event["payload"]["status"] == "passed"
        for event in state["store"].list_events(state["run"]["id"])
    )


@pytest.mark.parametrize(
    "runner_type",
    [UnattestedAcceptanceRunner, ForgedAcceptanceRunner],
)
def test_acceptance_requires_exact_attempt_bound_report(tmp_path, runner_type):
    state = _prepared_state(tmp_path)

    report = _engine(
        state, FakeModelProvider(), runner_type()
    ).execute(
        run_id=state["run"]["id"],
        workspace=state["workspace"],
    )

    assert not report.succeeded
    acceptance = [
        event
        for event in state["store"].list_events(state["run"]["id"])
        if event["event_type"] == "evidence.recorded"
        and event["payload"]["kind"] == "acceptance"
    ]
    assert acceptance
    assert all(event["payload"]["status"] != "passed" for event in acceptance)


def test_release_rejects_source_changed_while_verification_runs(tmp_path):
    state = _prepared_state(tmp_path)

    report = _engine(
        state, FakeModelProvider(), SourceMutatingRunner()
    ).execute(
        run_id=state["run"]["id"],
        workspace=state["workspace"],
    )

    assert not report.succeeded
    assert not any(
        attachment["role"] == "source:release-snapshot"
        for attachment in state["store"].list_run_artifacts(
            state["run"]["id"]
        )
    )


def test_restart_recovers_interrupted_attempt_and_resumes_durably(tmp_path):
    state = _prepared_state(tmp_path)
    store = state["store"]
    run_id = state["run"]["id"]
    task = store.list_tasks(run_id)[0]
    store.set_run_status(run_id, "running", expected_status="ready")
    store.set_task_status(
        task["id"],
        "running",
        expected_status="ready",
        increment_attempt=True,
    )
    provider = FakeModelProvider()
    runner = PassingCommandRunner()

    report = _engine(
        state, provider, runner, max_task_attempts=2
    ).execute(run_id=run_id, workspace=state["workspace"])

    assert report.succeeded
    recovered = store.get_task(task["id"])
    assert recovered["status"] == "succeeded"
    assert recovered["attempt"] == 2
    assert any(
        event["event_type"] == "task.interrupted"
        and event["task_id"] == task["id"]
        for event in store.list_events(run_id)
    )


def test_execution_rejects_unbound_approval_and_wrong_workspace(tmp_path):
    state = _prepared_state(tmp_path)
    provider = FakeModelProvider()
    runner = PassingCommandRunner()
    unrelated = state["store"].request_approval(
        state["project"].id,
        gate="architecture",
        request=state["approval"]["request"],
    )
    unrelated = state["store"].decide_approval(
        unrelated["id"], approved=True, decision={"actor": "other"}
    )
    engine = _engine(state, provider, runner)

    with pytest.raises(ApprovalValidationError, match="not the approval bound"):
        engine.execute(
            run_id=state["run"]["id"],
            workspace=state["workspace"],
            architecture_approval_id=unrelated["id"],
        )

    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    with pytest.raises(WorkspaceValidationError, match="not the destination"):
        engine.execute(
            run_id=state["run"]["id"],
            workspace=other_workspace,
        )

    (state["workspace"] / "package.json").write_text(
        '{"name":"tampered-verifier"}\n'
    )
    with pytest.raises(WorkspaceValidationError, match="protected scaffold file changed"):
        engine.execute(
            run_id=state["run"]["id"],
            workspace=state["workspace"],
        )


def test_resume_accepts_only_durably_recorded_generated_source(tmp_path):
    state = _prepared_state(tmp_path)
    provider = FakeModelProvider()
    runner = PassingCommandRunner()
    engine = _engine(state, provider, runner)

    first = engine.execute(
        run_id=state["run"]["id"],
        workspace=state["workspace"],
    )
    resumed = engine.execute(
        run_id=state["run"]["id"],
        workspace=state["workspace"],
    )

    assert first.succeeded and resumed.succeeded
    assert len(provider.requests) == 1

    (state["workspace"] / "apps/web/generated.ts").write_text(
        "export const unrecordedTamper = true;\n"
    )
    with pytest.raises(
        WorkspaceValidationError, match="protected scaffold file changed"
    ):
        engine.execute(
            run_id=state["run"]["id"],
            workspace=state["workspace"],
        )


def test_execution_rejects_unrecorded_workspace_files_and_next_type_shims(
    tmp_path,
):
    state = _prepared_state(tmp_path)
    engine = _engine(state, FakeModelProvider(), PassingCommandRunner())

    (state["workspace"] / ".npmrc").write_text(
        "registry=https://attacker.invalid/\n"
    )
    with pytest.raises(WorkspaceValidationError, match="unrecorded file"):
        engine.execute(
            run_id=state["run"]["id"],
            workspace=state["workspace"],
        )
    (state["workspace"] / ".npmrc").unlink()

    (state["workspace"] / "apps/web/next-env.d.ts").write_text(
        '/// <reference path="../../attacker.d.ts" />\n'
    )
    with pytest.raises(
        WorkspaceValidationError, match="unsupported code"
    ):
        engine.execute(
            run_id=state["run"]["id"],
            workspace=state["workspace"],
        )


def test_concurrent_execution_is_rejected_by_durable_owner_lease(tmp_path):
    state = _prepared_state(tmp_path)
    provider = BlockingModelProvider()
    engine = _engine(state, provider, PassingCommandRunner())
    outcome = {}

    thread = threading.Thread(
        target=lambda: outcome.setdefault(
            "report",
            engine.execute(
                run_id=state["run"]["id"],
                workspace=state["workspace"],
            ),
        )
    )
    thread.start()
    assert provider.started.wait(2)

    with pytest.raises(ConcurrentExecutionError, match="active execution owner"):
        engine.execute(
            run_id=state["run"]["id"],
            workspace=state["workspace"],
        )

    provider.release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert outcome["report"].succeeded


def test_canceled_provider_cannot_write_source_after_scheduler_returns(tmp_path):
    state = _prepared_state(tmp_path)
    provider = BlockingModelProvider()
    engine = _engine(state, provider, PassingCommandRunner())
    cancellation = CancellationToken()
    outcome = {}

    thread = threading.Thread(
        target=lambda: outcome.setdefault(
            "report",
            engine.execute(
                run_id=state["run"]["id"],
                workspace=state["workspace"],
                cancellation=cancellation,
            ),
        )
    )
    thread.start()
    assert provider.started.wait(2)
    cancellation.cancel("test cancellation")
    thread.join(3)

    assert not thread.is_alive()
    assert outcome["report"].status == "canceled"
    provider.release.set()
    time.sleep(0.1)
    assert not (state["workspace"] / "apps/web/generated.ts").exists()


def test_verification_cancellation_reaps_descendants_before_run_returns(
    tmp_path, monkeypatch
):
    state = _prepared_state(tmp_path)
    argv, started, child_pid, late_marker = (
        _term_ignoring_verification_argv(tmp_path, "verification")
    )
    runner = _direct_process_command_runner(monkeypatch)
    config = RunEngineConfig(
        task_timeout_seconds=5,
        lint_argv=argv,
        static_argv=argv,
        unit_argv=argv,
        build_argv=argv,
        acceptance_argv=argv,
    )
    engine = RunEngine(
        state["store"],
        gateway=_gateway(FakeModelProvider()),
        command_runner=runner,
        provider="fake",
        model="fake-code-model",
        config=config,
    )
    cancellation = CancellationToken()
    outcome = {}

    def execute() -> None:
        try:
            outcome["report"] = engine.execute(
                run_id=state["run"]["id"],
                workspace=state["workspace"],
                cancellation=cancellation,
            )
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 2
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.is_file()
    cancellation.cancel("verification canceled by test")
    thread.join(3)

    assert not thread.is_alive()
    assert "error" not in outcome
    assert outcome["report"].status == "canceled"
    assert runner.wait_for_idle(0.1)
    _assert_process_tree_is_dead(child_pid)
    time.sleep(0.65)
    assert not late_marker.exists()


def _assert_process_tree_is_dead(child_pid: Path) -> None:
    """Assert the reaped grandchild is no longer running.

    The runner's contract is that no *live* process-group member survives; a
    SIGKILLed grandchild is reparented, so its ``/proc`` entry can linger
    briefly as a zombie before init collects it. Poll for the entry, and accept
    the zombie state -- asserting on the raw entry fails under load.
    """

    entry = Path("/proc", child_pid.read_text())
    deadline = time.monotonic() + 2
    while entry.exists() and time.monotonic() < deadline:
        if _process_state(entry) == "Z":
            return
        time.sleep(0.01)
    assert not entry.exists() or _process_state(entry) == "Z"


def _process_state(entry: Path) -> str | None:
    try:
        stat_line = (entry / "stat").read_text(errors="replace")
    except OSError:
        return None
    return stat_line[stat_line.rfind(")") + 2 :].split()[0]


def test_lease_loss_reaps_verification_descendants_before_return(
    tmp_path, monkeypatch
):
    state = _prepared_state(tmp_path)
    argv, started, child_pid, late_marker = (
        _term_ignoring_verification_argv(tmp_path, "lease-loss")
    )
    runner = _direct_process_command_runner(monkeypatch)
    config = RunEngineConfig(
        task_timeout_seconds=5,
        # The injected renewal failure below is what ends this lease. A lease
        # short enough to lapse on its own would race it, and a slow machine
        # would see a RevisionConflict escape instead of a clean cancellation.
        execution_lease_seconds=30,
        execution_heartbeat_seconds=0.05,
        lint_argv=argv,
        static_argv=argv,
        unit_argv=argv,
        build_argv=argv,
        acceptance_argv=argv,
    )
    engine = RunEngine(
        state["store"],
        gateway=_gateway(FakeModelProvider()),
        command_runner=runner,
        provider="fake",
        model="fake-code-model",
        config=config,
    )
    real_renew = state["store"].renew_run_execution
    renewal_failed = threading.Event()

    def fail_renewal_after_verification_starts(*args, **kwargs):
        if not started.exists():
            return real_renew(*args, **kwargs)
        renewal_failed.set()
        raise RuntimeError("injected lease renewal failure")

    monkeypatch.setattr(
        state["store"],
        "renew_run_execution",
        fail_renewal_after_verification_starts,
    )
    outcome = {}

    def execute() -> None:
        try:
            outcome["report"] = engine.execute(
                run_id=state["run"]["id"],
                workspace=state["workspace"],
            )
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=execute)
    thread.start()
    assert renewal_failed.wait(10)
    thread.join(10)

    assert not thread.is_alive()
    assert "error" not in outcome
    assert outcome["report"].status == "canceled"
    assert runner.wait_for_idle(0.1)
    _assert_process_tree_is_dead(child_pid)
    time.sleep(0.65)
    assert not late_marker.exists()


def test_stale_model_terminal_event_is_rejected_after_lease_takeover(
    tmp_path, monkeypatch
):
    state = _prepared_state(tmp_path)
    provider = BlockingModelProvider()
    model_events = _LeaseBoundModelEventSink(
        state["store"],
        state["run"]["id"],
    )
    terminal_attempted = threading.Event()
    terminal_rejected = threading.Event()

    def event_sink(event_type, payload):
        if event_type != "model.attempt.started":
            terminal_attempted.set()
        try:
            model_events(event_type, payload)
        except StoreError:
            if event_type != "model.attempt.started":
                terminal_rejected.set()
            raise

    gateway = _gateway(provider, event_sink=event_sink)
    engine = RunEngine(
        state["store"],
        gateway=gateway,
        command_runner=PassingCommandRunner(),
        provider=provider.name,
        model="fake-code-model",
        config=RunEngineConfig(
            execution_lease_seconds=30,
            execution_heartbeat_seconds=0.05,
        ),
        execution_owner_binding=model_events.bind,
    )
    real_renew = state["store"].renew_run_execution
    renewal_failed = threading.Event()
    owner_tokens: list[str] = []

    def fail_renewal_while_provider_is_blocked(*args, **kwargs):
        owner_tokens.append(kwargs["owner_token"])
        if not provider.started.is_set():
            return real_renew(*args, **kwargs)
        renewal_failed.set()
        raise RuntimeError("injected lease renewal failure")

    monkeypatch.setattr(
        state["store"],
        "renew_run_execution",
        fail_renewal_while_provider_is_blocked,
    )
    outcome = {}

    def execute() -> None:
        try:
            outcome["report"] = engine.execute(
                run_id=state["run"]["id"],
                workspace=state["workspace"],
            )
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=execute)
    thread.start()
    assert provider.started.wait(10)
    assert renewal_failed.wait(10)
    thread.join(10)
    assert not thread.is_alive()
    assert "error" not in outcome
    assert outcome["report"].status == "canceled"

    # A cancelled owner deliberately does not release, so the successor waits
    # for expiry. Collapse the lease explicitly rather than sizing it to lapse
    # mid-shutdown: a lease that short races the engine's own cancellation
    # bookkeeping, and a conflict there escapes as an error instead of the
    # clean cancellation this test is about.
    real_renew(
        state["run"]["id"],
        owner_token=owner_tokens[-1],
        lease_seconds=0.01,
    )
    takeover_deadline = time.monotonic() + 10
    successor = None
    while successor is None and time.monotonic() < takeover_deadline:
        try:
            successor = state["store"].claim_run_execution(
                state["run"]["id"]
            )
        except StoreError:
            time.sleep(0.02)
    assert successor is not None

    provider.release.set()
    assert terminal_attempted.wait(2)
    assert terminal_rejected.wait(2)
    assert state["store"].is_run_execution_owner(
        state["run"]["id"],
        successor.owner_token,
    )
    model_event_types = [
        event["event_type"]
        for event in state["store"].list_events(state["run"]["id"])
        if event["event_type"].startswith("model.attempt.")
    ]
    assert model_event_types == ["model.attempt.started"]
    assert not (state["workspace"] / "apps/web/generated.ts").exists()


def test_default_executor_recovers_budget_only_after_claiming_lease(
    tmp_path,
):
    state = _prepared_state(tmp_path, max_model_attempts=1)
    run_id = state["run"]["id"]
    task_id = state["store"].list_tasks(run_id)[0]["id"]
    predecessor = state["store"].claim_run_execution(run_id)
    maximum = Usage(
        model_attempts=1,
        input_tokens=32_000,
        output_tokens=8_000,
        cost_usd=Decimal("0.22"),
        execution_seconds=120,
    )
    state["store"].append_event(
        run_id,
        "model.attempt.started",
        {
            "schema_version": MODEL_ATTEMPT_EVENT_SCHEMA,
            "reservation_id": "predecessor-reservation",
            "run_id": run_id,
            "task_id": task_id,
            "correlation_id": "predecessor-call",
            "provider": "fake",
            "model": "fake-code-model",
            "role": "implementer",
            "attempt": 1,
            "maximum_usage": maximum.to_mapping(),
        },
        task_id=task_id,
        owner_token=predecessor.owner_token,
    )
    assert state["store"].release_run_execution(
        run_id,
        owner_token=predecessor.owner_token,
    )

    class CountingProvider(FakeModelProvider):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def generate(self, request):
            self.calls += 1
            return super().generate(request)

    provider = CountingProvider()
    snapshot_recovered = threading.Event()
    continue_construction = threading.Event()
    recovered_usage = {}

    def runtime_builder(budget, *, event_history, event_sink, route=None, cache_root=None):
        approved = RunBudget.from_mapping(budget)
        usage = recover_model_usage(event_history)
        recovered_usage["value"] = usage
        snapshot_recovered.set()
        assert continue_construction.wait(2)
        gateway = ModelGateway(
            [provider],
            BudgetLedger(approved, initial_usage=usage),
            event_sink=event_sink,
        )
        commands = SimpleNamespace(
            lint_argv=("unused-lint",),
            static_argv=("unused-static",),
            unit_argv=("unused-unit",),
            property_argv=("unused-property",),
            build_argv=("unused-build",),
            acceptance_argv=("unused-acceptance",),
            database_argv=("unused-database",),
            probe_argv=("unused-probe",),
        )
        return SimpleNamespace(
            gateway=gateway,
            executor=BubblewrapExecutor(
                executable="/usr/bin/bwrap"
            ),
            provider_name=provider.name,
            model="fake-code-model",
            bootstrapper=None,
            commands=commands,
        )

    executor = DefaultRunExecutor(
        state["store"],
        runtime_builder=runtime_builder,
    )
    outcome = {}

    def execute() -> None:
        try:
            outcome["report"] = executor.execute(
                run_id=run_id,
                workspace=state["workspace"],
            )
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=execute)
    thread.start()
    assert snapshot_recovered.wait(2)
    with pytest.raises(StoreError, match="active execution owner"):
        state["store"].claim_run_execution(run_id)
    continue_construction.set()
    thread.join(3)

    assert not thread.is_alive()
    assert "error" not in outcome
    assert not outcome["report"].succeeded
    assert recovered_usage["value"].model_attempts == 1
    assert provider.calls == 0
    assert [
        event["event_type"]
        for event in state["store"].list_events(run_id)
        if event["event_type"].startswith("model.attempt.")
    ] == ["model.attempt.started"]


def test_workspace_preparation_recording_is_fenced_after_takeover(tmp_path):
    state = _prepared_state(tmp_path)
    engine = _engine(
        state,
        FakeModelProvider(),
        PassingCommandRunner(),
    )
    old_lease = state["store"].claim_run_execution(
        state["run"]["id"]
    )
    prepared_result = ExecutionResult(
        argv=("pnpm", "install"),
        returncode=0,
        stdout="installed",
        stderr="",
        duration_seconds=0.1,
    )

    class Prepared:
        dependency_install = prepared_result
        browser_install = None

    assert state["store"].release_run_execution(
        state["run"]["id"],
        owner_token=old_lease.owner_token,
    )
    successor = state["store"].claim_run_execution(
        state["run"]["id"]
    )

    with pytest.raises(StoreError, match="execution owner"):
        engine._record_workspace_preparation(
            state["run"]["id"],
            Prepared(),
            owner_token=old_lease.owner_token,
        )

    assert state["store"].is_run_execution_owner(
        state["run"]["id"], successor.owner_token
    )
    assert not any(
        attachment["role"].startswith("bootstrap:")
        for attachment in state["store"].list_run_artifacts(
            state["run"]["id"]
        )
    )
    assert not any(
        event["event_type"] == "workspace.prepared"
        for event in state["store"].list_events(state["run"]["id"])
    )


class _SimulatedProcessDeath(BaseException):
    pass


class _CrashAfterApplySink:
    def __init__(self, durable):
        self.durable = durable

    def prepare(self, **kwargs):
        self.durable.prepare(**kwargs)

    def commit(self, **_kwargs):
        # BaseException models process disappearance: CodingWorker's ordinary
        # exception rollback path must not get a chance to run.
        raise _SimulatedProcessDeath()

    def abort(self, **kwargs):
        self.durable.abort(**kwargs)


class _CrashAfterDurableCommitSink(_CrashAfterApplySink):
    def commit(self, **kwargs):
        self.durable.commit(**kwargs)
        raise _SimulatedProcessDeath()


def test_process_crash_after_source_apply_is_journaled_and_resumed(tmp_path):
    state = _prepared_state(tmp_path)
    store = state["store"]
    run_id = state["run"]["id"]
    durable_task = store.list_tasks(run_id)[0]
    store.set_run_status(run_id, "running", expected_status="ready")
    durable_task = store.set_task_status(
        durable_task["id"],
        "running",
        expected_status="ready",
        increment_attempt=True,
    )
    lease = store.claim_run_execution(run_id)
    approval = ApprovalWitness(
        project_id=state["project"].id,
        project_revision=state["project"].revision,
        architecture_id=state["architecture"].id,
        architecture_revision=state["architecture"].revision,
    )
    first_provider = FakeModelProvider()
    durable_sink = _DurableSourceTransactionSink(
        store,
        run_id=run_id,
        owner_token=lease.owner_token,
    )
    worker = CodingWorker(
        _gateway(first_provider),
        workspace=state["workspace"],
        project=state["project"],
        architecture=state["architecture"],
        approval=approval,
        provider=first_provider.name,
        model="fake-code-model",
        transaction_sink=_CrashAfterApplySink(durable_sink),
        mutation_guard=lambda: store.is_run_execution_owner(
            run_id, lease.owner_token
        ),
    )

    with pytest.raises(_SimulatedProcessDeath):
        worker.run_task(
            run_id=run_id,
            durable_task_id=durable_task["id"],
            attempt=1,
            task=state["plan"].tasks[0],
        )

    generated_path = state["workspace"] / "apps/web/generated.ts"
    assert generated_path.is_file()
    interrupted = store.list_source_transactions(run_id)
    assert [record["status"] for record in interrupted] == ["prepared"]
    assert not any(
        attachment["role"] == "generated-source"
        for attachment in store.list_run_artifacts(run_id)
    )

    # The process is gone, so its lease is explicitly released in the test.
    assert store.release_run_execution(
        run_id, owner_token=lease.owner_token
    )
    resume_provider = FakeModelProvider()
    report = _engine(
        state,
        resume_provider,
        PassingCommandRunner(),
        max_task_attempts=2,
    ).execute(run_id=run_id, workspace=state["workspace"])

    assert report.succeeded
    assert generated_path.is_file()
    transactions = store.list_source_transactions(run_id)
    assert [(item["attempt"], item["status"]) for item in transactions] == [
        (1, "rolled_back"),
        (2, "committed"),
    ]
    events = store.list_events(run_id)
    recovery_sequence = next(
        event["sequence"]
        for event in events
        if event["event_type"] == "source.transaction.rolled_back"
        and event["payload"]["attempt"] == 1
    )
    scheduler_sequence = next(
        event["sequence"]
        for event in events
        if event["event_type"] == "scheduler.started"
    )
    assert recovery_sequence < scheduler_sequence
    assert len(resume_provider.requests) == 1


def test_process_crash_after_source_attach_resumes_from_recorded_bytes(
    tmp_path,
):
    state = _prepared_state(tmp_path)
    store = state["store"]
    run_id = state["run"]["id"]
    durable_task = store.list_tasks(run_id)[0]
    store.set_run_status(run_id, "running", expected_status="ready")
    durable_task = store.set_task_status(
        durable_task["id"],
        "running",
        expected_status="ready",
        increment_attempt=True,
    )
    lease = store.claim_run_execution(run_id)
    approval = ApprovalWitness(
        project_id=state["project"].id,
        project_revision=state["project"].revision,
        architecture_id=state["architecture"].id,
        architecture_revision=state["architecture"].revision,
    )
    first_provider = FakeModelProvider()
    worker = CodingWorker(
        _gateway(first_provider),
        workspace=state["workspace"],
        project=state["project"],
        architecture=state["architecture"],
        approval=approval,
        provider=first_provider.name,
        model="fake-code-model",
        transaction_sink=_CrashAfterDurableCommitSink(
            _DurableSourceTransactionSink(
                store,
                run_id=run_id,
                owner_token=lease.owner_token,
            )
        ),
        mutation_guard=lambda: store.is_run_execution_owner(
            run_id, lease.owner_token
        ),
    )
    with pytest.raises(_SimulatedProcessDeath):
        worker.run_task(
            run_id=run_id,
            durable_task_id=durable_task["id"],
            attempt=1,
            task=state["plan"].tasks[0],
        )

    generated_path = state["workspace"] / "apps/web/generated.ts"
    assert generated_path.is_file()
    assert store.list_source_transactions(run_id)[0]["status"] == "committed"
    assert any(
        attachment["role"] == "generated-source"
        for attachment in store.list_run_artifacts(run_id)
    )
    assert store.release_run_execution(
        run_id, owner_token=lease.owner_token
    )

    class ReplaceModelProvider(FakeModelProvider):
        def generate(self, request):
            response = super().generate(request)
            payload = dict(response.parsed)
            payload["files"] = [
                {
                    **payload["files"][0],
                    "operation": "replace",
                }
            ]
            return ModelResponse(
                text=json.dumps(payload),
                parsed=payload,
                provider=response.provider,
                model=response.model,
                usage=response.usage,
                provider_request_id=response.provider_request_id,
            )

    resume_provider = ReplaceModelProvider()
    report = _engine(
        state,
        resume_provider,
        PassingCommandRunner(),
        max_task_attempts=2,
    ).execute(run_id=run_id, workspace=state["workspace"])

    assert report.succeeded
    assert generated_path.is_file()
    assert [(item["attempt"], item["status"]) for item in
            store.list_source_transactions(run_id)] == [
        (1, "committed"),
        (2, "committed"),
    ]


def test_process_crash_between_multi_file_replaces_recovers_partial_tree(
    tmp_path, monkeypatch
):
    state = _prepared_state(tmp_path)
    store = state["store"]
    run_id = state["run"]["id"]
    durable_task = store.list_tasks(run_id)[0]
    store.set_run_status(run_id, "running", expected_status="ready")
    durable_task = store.set_task_status(
        durable_task["id"],
        "running",
        expected_status="ready",
        increment_attempt=True,
    )
    lease = store.claim_run_execution(run_id)

    class TwoFileProvider(FakeModelProvider):
        def generate(self, request):
            response = super().generate(request)
            payload = dict(response.parsed)
            payload["files"] = [
                {
                    "operation": "create",
                    "path": "apps/web/generated-a.ts",
                    "content": "export const a = true;\n",
                },
                {
                    "operation": "create",
                    "path": "apps/web/generated-b.ts",
                    "content": "export const b = true;\n",
                },
            ]
            return ModelResponse(
                text=json.dumps(payload),
                parsed=payload,
                provider=response.provider,
                model=response.model,
                usage=response.usage,
                provider_request_id=response.provider_request_id,
            )

    provider = TwoFileProvider()
    approval = ApprovalWitness(
        project_id=state["project"].id,
        project_revision=state["project"].revision,
        architecture_id=state["architecture"].id,
        architecture_revision=state["architecture"].revision,
    )
    worker = CodingWorker(
        _gateway(provider),
        workspace=state["workspace"],
        project=state["project"],
        architecture=state["architecture"],
        approval=approval,
        provider=provider.name,
        model="fake-code-model",
        transaction_sink=_DurableSourceTransactionSink(
            store,
            run_id=run_id,
            owner_token=lease.owner_token,
        ),
        mutation_guard=lambda: store.is_run_execution_owner(
            run_id, lease.owner_token
        ),
    )
    destinations = {
        state["workspace"] / "apps/web/generated-a.ts",
        state["workspace"] / "apps/web/generated-b.ts",
    }
    real_replace = coding_module.os.replace
    replacement_count = 0

    def die_on_second_source_replace(source, destination):
        nonlocal replacement_count
        if Path(destination) in destinations:
            replacement_count += 1
            if replacement_count == 2:
                raise _SimulatedProcessDeath()
        return real_replace(source, destination)

    monkeypatch.setattr(
        coding_module.os, "replace", die_on_second_source_replace
    )
    with pytest.raises(_SimulatedProcessDeath):
        worker.run_task(
            run_id=run_id,
            durable_task_id=durable_task["id"],
            attempt=1,
            task=state["plan"].tasks[0],
        )

    assert sum(path.exists() for path in destinations) == 1
    assert store.list_source_transactions(run_id)[0]["status"] == "prepared"
    assert store.release_run_execution(
        run_id, owner_token=lease.owner_token
    )
    successor = store.claim_run_execution(run_id)
    _recover_prepared_source_transactions(
        store,
        run_id=run_id,
        workspace=state["workspace"],
        plan=state["plan"],
        owner_token=successor.owner_token,
    )

    assert not any(path.exists() for path in destinations)
    assert store.list_source_transactions(run_id)[0]["status"] == "rolled_back"


def test_local_apply_rollback_is_resolved_before_same_process_retry(
    tmp_path, monkeypatch
):
    state = _prepared_state(tmp_path)
    generated_path = state["workspace"] / "apps/web/generated.ts"
    real_replace = coding_module.os.replace
    failed_once = False

    def fail_first_source_replace(source, destination):
        nonlocal failed_once
        if not failed_once and Path(destination) == generated_path:
            failed_once = True
            raise OSError("injected replace failure after durable prepare")
        return real_replace(source, destination)

    monkeypatch.setattr(coding_module.os, "replace", fail_first_source_replace)
    provider = FakeModelProvider()
    report = _engine(
        state,
        provider,
        PassingCommandRunner(),
        max_task_attempts=2,
    ).execute(
        run_id=state["run"]["id"],
        workspace=state["workspace"],
    )

    assert report.succeeded
    assert failed_once
    assert generated_path.is_file()
    transactions = state["store"].list_source_transactions(
        state["run"]["id"]
    )
    assert [(item["attempt"], item["status"]) for item in transactions] == [
        (1, "rolled_back"),
        (2, "committed"),
    ]

    # Reopening a terminal run still performs source recovery/validation.  A
    # stale prepared attempt-1 journal would reject attempt-2's committed bytes.
    reopened_provider = FakeModelProvider()
    reopened = _engine(
        state,
        reopened_provider,
        PassingCommandRunner(),
        max_task_attempts=2,
    ).execute(
        run_id=state["run"]["id"],
        workspace=state["workspace"],
    )
    assert reopened.succeeded
    assert not reopened_provider.requests


def _claim_once_expired(store, run_id, timeout=30.0):
    """Take ownership as soon as the incumbent lease lapses.

    Sleeping a fixed interval guesses at wall-clock timing the test does not
    control. Polling states the actual precondition: the takeover happens the
    moment expiry makes it legal.
    """

    deadline = time.monotonic() + timeout
    while True:
        try:
            return store.claim_run_execution(run_id)
        except StoreError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def test_successor_recovery_waits_for_stale_writer_workspace_lock(
    tmp_path, monkeypatch
):
    state = _prepared_state(tmp_path)
    store = state["store"]
    run_id = state["run"]["id"]
    durable_task = store.list_tasks(run_id)[0]
    store.set_run_status(run_id, "running", expected_status="ready")
    durable_task = store.set_task_status(
        durable_task["id"],
        "running",
        expected_status="ready",
        increment_attempt=True,
    )
    # Comfortable, then collapsed below once the old writer is definitely
    # stalled. A 50ms lease raced how long it takes to build a prompt and
    # prepare a transaction, which is not a duration this test controls.
    old_lease = store.claim_run_execution(run_id, lease_seconds=30)
    approval = ApprovalWitness(
        project_id=state["project"].id,
        project_revision=state["project"].revision,
        architecture_id=state["architecture"].id,
        architecture_revision=state["architecture"].revision,
    )
    provider = FakeModelProvider()
    old_worker = CodingWorker(
        _gateway(provider),
        workspace=state["workspace"],
        project=state["project"],
        architecture=state["architecture"],
        approval=approval,
        provider=provider.name,
        model="fake-code-model",
        transaction_sink=_DurableSourceTransactionSink(
            store,
            run_id=run_id,
            owner_token=old_lease.owner_token,
        ),
        mutation_guard=lambda: store.is_run_execution_owner(
            run_id, old_lease.owner_token
        ),
    )
    generated_path = state["workspace"] / "apps/web/generated.ts"
    real_replace = coding_module.os.replace
    old_applied = threading.Event()
    release_old = threading.Event()
    stalled = False

    def stall_old_source_replace(source, destination):
        nonlocal stalled
        result = real_replace(source, destination)
        if not stalled and Path(destination) == generated_path:
            stalled = True
            old_applied.set()
            assert release_old.wait(30)
        return result

    monkeypatch.setattr(
        coding_module.os, "replace", stall_old_source_replace
    )
    old_outcome = {}

    def run_old():
        try:
            old_worker.run_task(
                run_id=run_id,
                durable_task_id=durable_task["id"],
                attempt=1,
                task=state["plan"].tasks[0],
            )
        except BaseException as exc:
            old_outcome["error"] = exc

    old_thread = threading.Thread(target=run_old)
    old_thread.start()
    assert old_applied.wait(30), "the stalled writer never reached its replace"
    assert generated_path.is_file()
    store.renew_run_execution(
        run_id, owner_token=old_lease.owner_token, lease_seconds=0.01
    )
    successor = _claim_once_expired(store, run_id)
    recovery_outcome = {}
    recovery_started = threading.Event()

    def recover_as_successor():
        recovery_started.set()
        try:
            _recover_prepared_source_transactions(
                store,
                run_id=run_id,
                workspace=state["workspace"],
                plan=state["plan"],
                owner_token=successor.owner_token,
            )
        except BaseException as exc:
            recovery_outcome["error"] = exc

    recovery_thread = threading.Thread(target=recover_as_successor)
    recovery_thread.start()
    assert recovery_started.wait(30)
    # The successor must block on the stale writer's workspace lock rather than
    # tearing the file out from under it; a moment of observation is enough to
    # show it is waiting, and the join below proves it eventually finishes.
    time.sleep(0.2)
    assert recovery_thread.is_alive()
    assert generated_path.is_file()

    release_old.set()
    old_thread.join(30)
    recovery_thread.join(30)

    assert not old_thread.is_alive()
    assert not recovery_thread.is_alive()
    assert "error" in old_outcome
    assert not recovery_outcome
    assert not generated_path.exists()
    transaction = store.list_source_transactions(run_id)[0]
    assert transaction["status"] == "rolled_back"
    assert transaction["resolved_owner_token"] == successor.owner_token


def test_source_symlinks_are_rejected_but_the_pnpm_link_farm_is_not(tmp_path):
    state = _prepared_state(tmp_path)
    engine = _engine(state, FakeModelProvider(), PassingCommandRunner())
    workspace = state["workspace"]

    # A pnpm workspace install is a link farm: every workspace:* dependency
    # becomes a symlink under node_modules pointing back at a sibling package.
    # Rejecting those made every genuinely bootstrapped run unvalidatable,
    # which is why this went unnoticed -- no test had ever bootstrapped one.
    link_root = workspace / "apps/web/node_modules/@scope"
    link_root.mkdir(parents=True)
    (link_root / "contracts").symlink_to(
        workspace / "packages/contracts", target_is_directory=True
    )
    (workspace / "node_modules").mkdir(exist_ok=True)
    (workspace / "node_modules" / "self").symlink_to(
        workspace, target_is_directory=True
    )

    assert engine.execute(
        run_id=state["run"]["id"], workspace=workspace
    ).succeeded

    # Source is held to the original rule: a symlink there can disguise what a
    # gate reads, so it is still refused.
    (workspace / "packages/domain/src/escape.ts").symlink_to(
        workspace / "package.json"
    )
    with pytest.raises(WorkspaceValidationError, match="unsafe symbolic link"):
        engine.execute(run_id=state["run"]["id"], workspace=workspace)


def test_prior_failure_source_reads_back_the_gate_logs_the_store_already_holds(
    tmp_path,
):
    """The verification artifacts always carried each gate's exact output; the
    retry that needed it simply never read them back."""

    from richbuild.run_engine import _prior_failure_source

    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.priors")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="ready",
    )

    def _record(*, attempt, status, kind, stdout, node_id="web"):
        document = {
            "schema_version": "rich.command-verification/v1",
            "kind": kind,
            "status": status,
            "returncode": 0 if status == "passed" else 2,
            "stdout": stdout,
            "stderr": "",
        }
        artifact = store.put_artifact(
            json.dumps(document).encode(),
            media_type="application/vnd.rich.command-verification+json",
            metadata={
                "kind": kind,
                "status": status,
                "node_id": node_id,
                "attempt": attempt,
            },
        )
        store.attach_artifact(
            run["id"], artifact.digest, role=f"verification:{kind}"
        )

    _record(
        attempt=1,
        status="failed",
        kind="types",
        stdout=(
            "apps/web/note.tsx(4,9): error TS2304: Cannot find name 'noteText'.\n"
            "packages/domain/private.ts(2,2): error TS2551: hidden detail\n"
        ),
    )
    _record(attempt=1, status="passed", kind="lint", stdout="clean")
    _record(attempt=1, status="failed", kind="unit", stdout="", node_id="domain")
    _record(attempt=2, status="failed", kind="types", stdout="a later attempt")

    task = CompiledTask(
        task_id="run:implement:web",
        node_id="web",
        order=0,
        contract_id=None,
        dependency_ids=(),
        consumer_ids=(),
        requirement_ids=(),
        owned_paths=("apps/web/note.tsx",),
    )
    failures = _prior_failure_source(store, run["id"])(task, 2)

    assert [item.gate for item in failures] == ["types"], (
        "passed gates, other nodes, and attempts at or after this one are excluded"
    )
    assert "TS2304" in failures[0].diagnostics[0]
    assert all("private.ts" not in line for line in failures[0].diagnostics)
    assert failures[0].withheld_line_count == 1


def test_a_durable_cancellation_reaches_a_token_in_another_process(tmp_path):
    """The token the engine checks is in one process; the request arrives in
    another. The store is what they share."""

    from richbuild.execution import _DurableCancellation

    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.tok")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="running",
    )
    token = _DurableCancellation(store, run["id"])

    assert token.is_cancelled is False
    store.request_run_cancellation(run["id"], reason="stop please")
    token._checked_at = 0.0  # skip the hot-path throttle
    observed = token.is_cancelled

    assert observed is True
    assert token.reason == "stop please"


def test_a_cancellation_check_never_fails_the_run_it_guards(tmp_path):
    from richbuild.execution import _DurableCancellation

    class _Broken:
        def run_cancellation(self, run_id):
            raise RuntimeError("database is gone")

    token = _DurableCancellation(_Broken(), "run.x")
    token._checked_at = 0.0

    assert token.is_cancelled is False


def test_a_node_is_gated_on_its_own_obligation_suite_and_no_other(tmp_path):
    """Running the whole properties directory fails a node because a component
    built later has not written its module yet -- judging one node by another's
    absence."""

    from richbuild.run_engine import _VerifiedCodingHandler

    workspace = tmp_path / "workspace"
    (workspace / "tests" / "properties").mkdir(parents=True)
    (workspace / "tests/properties/contract-domain.test.ts").write_text("//\n")

    handler = object.__new__(_VerifiedCodingHandler)
    handler.workspace = workspace
    handler._properties = workspace / "tests" / "properties"

    assert (
        handler._property_suite("contract:domain")
        == "tests/properties/contract-domain.test.ts"
    )
    assert handler._property_suite("contract:web") is None, (
        "a component with no suite scaffolded runs no property gate"
    )
    assert handler._property_suite(None) is None


@pytest.mark.parametrize(
    "kind", ["static", "lint", "unit", "property", "build", "acceptance"]
)
def test_a_model_worker_cannot_self_certify_any_gate_even_as_non_blocking(kind):
    """The forbidden set must name every kind the trusted runner publishes.
    PROPERTY was missing from it while present in VerificationCommand's
    accepted set, so a worker could hand back a passed, non-blocking
    obligation claim and have it recorded beside the gate's own evidence."""

    from richbuild.run_engine import RunEngineError, _VerifiedCodingHandler
    from richbuild.scheduler import TaskEvidence, TaskResult

    claimed = TaskResult(
        evidence=(
            TaskEvidence(
                kind=kind,
                status="passed",
                summary="the worker says this held",
                blocking=False,
            ),
        )
    )
    handler = object.__new__(_VerifiedCodingHandler)
    handler.model_worker = lambda context: claimed

    with pytest.raises(RunEngineError, match="self-certify"):
        handler(SimpleNamespace(is_cancelled=False))


def test_a_failed_acceptance_names_its_failed_steps_leniently():
    """The failures line explains; only the coverage line decides. So a good
    line is read, a bad one is ignored, and a passing run has none."""
    from richbuild.executor import ExecutionResult
    from richbuild.run_engine import _observed_acceptance_failures

    good = json.dumps(
        {
            "schema_version": "rich.acceptance-failures/v1",
            "context": {},
            "failures": [
                {"scenario_id": "scenario.add", "step": "3 · Expect to see the text ‘Buy milk’", "message": "Timed out waiting for the text"},
                {"scenario_id": "scenario.add", "step": "3 · Expect to see the text ‘Buy milk’", "message": "duplicate"},
                "not an object",
                {"step": ""},
            ],
        }
    )
    stdout = f"noise\nRICH_ACCEPTANCE_FAILURES {good}\nRICH_ACCEPTANCE_FAILURES not json\n"
    result = ExecutionResult(argv=("pnpm",), returncode=1, stdout=stdout, stderr="", duration_seconds=1.0)

    failures = _observed_acceptance_failures(result)

    assert failures == [
        {
            "scenario_id": "scenario.add",
            "step": "3 · Expect to see the text ‘Buy milk’",
            "message": "Timed out waiting for the text",
        }
    ]
    assert _observed_acceptance_failures(
        ExecutionResult(argv=("pnpm",), returncode=0, stdout="clean\n", stderr="", duration_seconds=1.0)
    ) == []


def test_gates_see_the_shared_cache_read_only(tmp_path):
    """The bootstrap wrote it; no gate may. The browsers path follows the cache."""
    from richbuild.executor import BubblewrapExecutor, ExecutionResult, SandboxPolicy
    from richbuild.run_engine import BubblewrapCommandRunner, VerificationCommand

    seen = {}

    class RecordingExecutor(BubblewrapExecutor):
        def run(self, root, argv, policy=None, *, cancellation=None, deadline=None):
            assert isinstance(policy, SandboxPolicy)
            seen["policy"] = policy
            return ExecutionResult(argv=tuple(argv), returncode=0, stdout="", stderr="", duration_seconds=0.01)

    cache_root = tmp_path / "cache"
    runner = BubblewrapCommandRunner(RecordingExecutor(executable="/usr/bin/python3"), cache_root=cache_root)
    assert runner.playwright_browsers_path == "/opt/rich-cache/playwright"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    runner.run(workspace, VerificationCommand(kind="lint", argv=("pnpm", "run", "lint")))

    policy = seen["policy"]
    assert policy.environment["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/rich-cache/playwright"
    assert [(m.guest_path, m.writable) for m in policy.cache_mounts] == [
        ("/opt/rich-cache/pnpm-store", False),
        ("/opt/rich-cache/playwright", False),
    ]
    assert policy.network is False


class FailingAcceptanceRunner(PassingCommandRunner):
    """Every gate passes except the browser run, which names its failing step."""

    def run(self, workspace, command, **controls):
        result = super().run(workspace, command, **controls)
        if command.kind != "acceptance":
            return result
        assert command.acceptance_context is not None
        failures = {
            "schema_version": "rich.acceptance-failures/v1",
            "context": command.acceptance_context.to_dict(),
            "failures": [
                {
                    "scenario_id": "scenario.behavior",
                    "step": "2 · Expect to see ‘approved behavior’",
                    "message": (
                        "Error: \x1b[2mexpect(\x1b[22m\x1b[31mlocator\x1b[39m"
                        "\x1b[2m).\x1b[22mtoBeVisible failed"
                    ),
                }
            ],
        }
        return ExecutionResult(
            argv=result.argv,
            returncode=1,
            stdout=(
                "  ✘  1 [chromium] › tests/e2e/scenarios/behavior.spec.ts\n"
                f"RICH_ACCEPTANCE_FAILURES {json.dumps(failures)}\n"
            ),
            stderr="",
            duration_seconds=0.02,
        )


def _artifacts_with_role(state, role):
    return [
        record
        for record in state["store"].list_run_artifacts(state["run"]["id"])
        if record["role"] == role
    ]


def test_failed_acceptance_is_attributed_to_the_page_owner(tmp_path):
    from richbuild.run_engine import RunEngine, RunEngineConfig
    from richbuild.target_packs.nextjs import exercised_pages

    state = _prepared_state(tmp_path)
    provider = FakeModelProvider()
    runner = FailingAcceptanceRunner()
    engine = RunEngine(
        state["store"],
        gateway=_gateway(provider),
        command_runner=runner,
        provider=provider.name,
        model="fake-code-model",
        config=RunEngineConfig(
            max_task_attempts=1, exercised_paths=exercised_pages
        ),
    )

    report = engine.execute(
        run_id=state["run"]["id"], workspace=state["workspace"]
    )

    assert report.status == "failed"
    store = state["store"]
    verification = _artifacts_with_role(state, "verification:acceptance")[-1]
    # The fixture's one node owns apps/web, so the page's owner is the task
    # itself: recorded all the same, and the scheduler treats it as the
    # ordinary retry.
    assert verification["metadata"]["attributed_node_ids"] == ["app"]
    document = json.loads(
        store.get_artifact(verification["digest"]).path.read_text()
    )
    assert document["failed_steps"] == [
        {
            "scenario_id": "scenario.behavior",
            "step": "2 · Expect to see ‘approved behavior’",
            "message": "Error: expect(locator).toBeVisible failed",
        }
    ]
    result = _artifacts_with_role(state, "evidence-result:acceptance")[-1]
    evidence_document = json.loads(
        store.get_artifact(result["digest"]).path.read_text()
    )
    assert evidence_document["attributed_node_ids"] == ["app"]
    event_types = {
        event["event_type"] for event in store.list_events(state["run"]["id"])
    }
    assert "task.reopened" not in event_types


def test_without_a_pack_answer_nothing_is_attributed(tmp_path):
    state = _prepared_state(tmp_path)
    provider = FakeModelProvider()
    runner = FailingAcceptanceRunner()

    report = _engine(state, provider, runner).execute(
        run_id=state["run"]["id"], workspace=state["workspace"]
    )

    assert report.status == "failed"
    verification = _artifacts_with_role(state, "verification:acceptance")[-1]
    assert "attributed_node_ids" not in verification["metadata"]
    result = _artifacts_with_role(state, "evidence-result:acceptance")[-1]
    evidence_document = json.loads(
        state["store"].get_artifact(result["digest"]).path.read_text()
    )
    assert evidence_document["attributed_node_ids"] == []


def test_reopened_owner_reads_the_acceptance_failure_it_caused(tmp_path):
    from richbuild.run_engine import _prior_failure_source

    state = _prepared_state(tmp_path)
    store = state["store"]
    run_id = state["run"]["id"]
    document = {
        "schema_version": "rich.command-verification/v1",
        "kind": "acceptance",
        "status": "failed",
        "returncode": 1,
        "stdout": (
            "  ✘  1 [chromium] › tests/e2e/scenarios/x.spec.ts › Create a project\n"
            "    waiting for getByLabel('Project name')\n"
        ),
        "stderr": "",
        "failed_steps": [
            {
                "scenario_id": "scenario.behavior",
                "step": "2 · Type ‘x’ into the field labelled ‘Project name’",
                "message": "Error: locator.fill: Test timeout of 30000ms exceeded.",
            }
        ],
    }
    artifact = store.put_artifact(
        json.dumps(document).encode(),
        media_type="application/vnd.rich.command-verification+json",
        metadata={
            "kind": "acceptance",
            "status": "failed",
            "node_id": "app",
            "attempt": 1,
            "attributed_node_ids": ["web"],
        },
    )
    store.attach_artifact(
        run_id,
        artifact.digest,
        role="verification:acceptance",
        task_id=f"{run_id}:implement:app",
    )

    def task(node_id, owned):
        return CompiledTask(
            task_id=f"implement:{node_id}",
            node_id=node_id,
            order=0,
            contract_id=f"contract.{node_id}",
            dependency_ids=(),
            consumer_ids=("app",),
            requirement_ids=("requirement.behavior",),
            owned_paths=(owned,),
        )

    read = _prior_failure_source(store, run_id)
    (failure,) = read(task("web", "apps/web"), 2)
    assert failure.gate == "acceptance"
    assert failure.attempt == 1
    assert "failed acceptance on pages this task owns" in failure.summary
    assert failure.diagnostics[0] == (
        "scenario.behavior · 2 · Type ‘x’ into the field labelled "
        "‘Project name’: Error: locator.fill: Test timeout of 30000ms exceeded."
    )
    assert any("Project name" in line for line in failure.diagnostics[1:])
    # The same artifact says nothing to a task it was not attributed to.
    assert read(task("domain", "packages/domain"), 2) == ()


def test_attribution_spares_the_owner_of_a_page_that_passed(tmp_path):
    """Two scenarios on two pages with two owners; only the failing one's owner
    is named. The coverage line of a failed run is read leniently for this and
    for nothing else."""
    from richbuild.run_engine import (
        RunEngineConfig,
        _VerifiedCodingHandler,
        _lenient_observed_scenario_ids,
    )
    from richbuild.target_packs.nextjs import _route_segments, exercised_pages

    def scenario(scenario_id, requirement_id):
        return AcceptanceScenario(
            id=scenario_id,
            title=scenario_id,
            when=("it runs",),
            then=("it answers",),
            requirement_ids=(requirement_id,),
            oracle=(
                {"action": "open_requirement"},
                {"action": "assert_visible", "locator": {"kind": "text", "value": "ok"}},
            ),
        )

    project = ProjectSpec(
        id="project.two-pages",
        name="Two pages",
        goal="Attribute a failure to its page",
        audiences=("owner",),
        requirements=(
            Requirement(id="req.a", title="A", statement="Page A works."),
            Requirement(id="req.b", title="B", statement="Page B works."),
        ),
        acceptance_scenarios=(scenario("scenario.a", "req.a"), scenario("scenario.b", "req.b")),
    )
    routes = _route_segments(("req.a", "req.b"))

    def node(node_id, requirement_id):
        return ArchitectureNode(
            id=node_id,
            name=node_id,
            kind=NodeKind.MODULE,
            contract_id=f"contract.{node_id}",
            requirement_ids=(requirement_id,),
            owned_paths=(f"apps/web/src/app/capabilities/{routes[requirement_id]}",),
        )

    def contract(node_id, requirement_id):
        return Contract(
            id=f"contract.{node_id}",
            node_id=node_id,
            operations=(
                OperationContract(
                    id=f"operation.{node_id}.show",
                    name="show",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    requirement_ids=(requirement_id,),
                ),
            ),
        )

    architecture = ArchitectureSpec(
        id="architecture.two-pages",
        project_id=project.id,
        root_node_id="owner-a",
        target_pack="nextjs-app-router",
        nodes=(node("owner-a", "req.a"), node("owner-b", "req.b")),
        edges=(
            ArchitectureEdge(
                id="contains:owner-b",
                kind=EdgeKind.CONTAINS,
                source_node_id="owner-a",
                target_node_id="owner-b",
            ),
        ),
        contracts=(contract("owner-a", "req.a"), contract("owner-b", "req.b")),
        project_spec_revision=project.revision,
    )
    handler = _VerifiedCodingHandler(
        lambda context: None,
        command_runner=PassingCommandRunner(),
        workspace=tmp_path,
        project=project,
        root_node_id="owner-a",
        config=RunEngineConfig(exercised_paths=exercised_pages),
        architecture=architecture,
    )
    failed = [{"scenario_id": "scenario.a", "step": "2 · Expect to see ‘ok’", "message": "not found"}]
    assert handler._acceptance_owners(
        failed, expected=("scenario.a", "scenario.b"), observed=("scenario.b",)
    ) == ("owner-a",)
    # Without the passed list every expected scenario counts as failed and both
    # owners are named -- the case the lenient read exists to avoid.
    assert handler._acceptance_owners(
        failed, expected=("scenario.a", "scenario.b"), observed=()
    ) == ("owner-a", "owner-b")

    coverage = json.dumps({"schema_version": "rich.acceptance-coverage/v1", "scenario_ids": ["scenario.b"]})
    stdout = f"  ✘  1 scenario.a\nRICH_ACCEPTANCE_COVERAGE {coverage}\n"
    assert _lenient_observed_scenario_ids(
        ExecutionResult(argv=("x",), returncode=1, stdout=stdout, stderr="", duration_seconds=0.1)
    ) == ("scenario.b",)
    assert _lenient_observed_scenario_ids(
        ExecutionResult(argv=("x",), returncode=1, stdout="RICH_ACCEPTANCE_COVERAGE {not json\n", stderr="", duration_seconds=0.1)
    ) == ()


# --------------------------------------------------------------------------
# Persistence: a data component means every gate that runs the software gets a
# fresh, migrated database first, and the browser's run is followed by the
# probe. Model output was never evidence; now neither is a reload.
# --------------------------------------------------------------------------


class OwnedPathProvider(FakeModelProvider):
    """Writes one file into whichever path the task it is asked for owns."""

    def generate(self, request):
        self.requests.append(request)
        prompt = request.user_prompt
        context = json.loads(prompt[prompt.rindex("\n{") + 1 :])
        owned = context["write_authority"]["owned_paths"][0]
        payload = {
            "summary": "Implemented the allocated component",
            "files": [
                {
                    "operation": "create",
                    "path": f"{owned}/generated.ts",
                    "content": "export const generated = true;\n",
                }
            ],
        }
        return ModelResponse(
            text=json.dumps(payload),
            parsed=payload,
            provider=self.name,
            model=request.model,
            usage=Usage(
                model_attempts=1,
                input_tokens=200,
                output_tokens=100,
                cost_usd=Decimal("0.01"),
                execution_seconds=0.05,
            ),
            provider_request_id=f"fake-{len(self.requests)}",
        )


class DatabaseAwareRunner(PassingCommandRunner):
    """Passes every gate and answers the two trusted database steps the way
    the pack's migrator and probe would -- with knobs for each way a sandbox
    could disagree with the host."""

    def __init__(
        self,
        *,
        tables=None,
        prepare_returncode=0,
        reported_sha=None,
        probe_journal=None,
    ):
        super().__init__()
        self.tables = {"todos": 1} if tables is None else tables
        self.prepare_returncode = prepare_returncode
        self.reported_sha = reported_sha
        self.probe_journal = probe_journal

    def _migrations(self, workspace):
        import hashlib

        entries = []
        for path in sorted((workspace / "packages/db/migrations").glob("*.sql")):
            sha = self.reported_sha or hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append({"file": path.name, "sha256": sha, "applied": True})
        return entries

    def run(self, workspace, command, *, cancellation=None, deadline=None):
        from richbuild.run_engine import DATABASE_PREPARE, DatabaseStep

        if not isinstance(command, DatabaseStep):
            return super().run(
                workspace, command, cancellation=cancellation, deadline=deadline
            )
        self.commands.append(command)
        directory = workspace / ".rich/runtime/db"
        if command.kind == DATABASE_PREPARE:
            assert not directory.exists(), (
                "the engine removes the previous database before preparing"
            )
            directory.mkdir(parents=True)
            report = {
                "schema_version": "rich.database-migrations/v1",
                "engine": {
                    "name": "pglite",
                    "server_version": "PostgreSQL 18.3 (PGlite 0.5.8)",
                },
                "migrations": self._migrations(workspace),
            }
            return ExecutionResult(
                argv=command.argv,
                returncode=self.prepare_returncode,
                stdout=f"RICH_DATABASE_MIGRATIONS {json.dumps(report)}\n",
                stderr="",
                duration_seconds=0.01,
            )
        assert directory.exists(), "the probe reads what the prepare step created"
        journal = (
            self.probe_journal
            if self.probe_journal is not None
            else [
                {"file": entry["file"], "sha256": entry["sha256"]}
                for entry in self._migrations(workspace)
            ]
        )
        report = {
            "schema_version": "rich.database-probe/v1",
            "engine": {
                "name": "pglite",
                "version": "0.5.8",
                "server_version": "PostgreSQL 18.3 (PGlite 0.5.8)",
            },
            "migrations": journal,
            "tables": self.tables,
        }
        return ExecutionResult(
            argv=command.argv,
            returncode=0,
            stdout=f"RICH_DATABASE_PROBE {json.dumps(report)}\n",
            stderr="",
            duration_seconds=0.01,
        )


def _persisting_state(tmp_path):
    """A run whose architecture has a data component: the planner's own."""

    from richbuild.planner import plan_nextjs_architecture

    store = RichStore(tmp_path / "state")
    project_record = store.create_project("Todo", project_id="project.todo")
    project = ProjectSpec(
        id=project_record["id"],
        name="Todo",
        goal="Keep a todo list that is stored in the database.",
        audiences=("members",),
        requirements=(
            Requirement(
                id="requirement.todo",
                title="Todo list",
                statement="A member adds a todo and it is stored.",
            ),
        ),
        acceptance_scenarios=(
            AcceptanceScenario(
                id="scenario.todo",
                title="A todo persists",
                given=("The list is empty.",),
                when=("A member adds 'Buy milk'.",),
                then=("'Buy milk' is listed.",),
                requirement_ids=("requirement.todo",),
                oracle=(
                    {"action": "open_requirement"},
                    {
                        "action": "assert_visible",
                        "locator": {"kind": "text", "value": "Buy milk"},
                    },
                ),
            ),
        ),
    )
    architecture = plan_nextjs_architecture(project).architecture
    assert any(node.kind is NodeKind.DATA for node in architecture.nodes)
    spec_revision = store.save_revision(
        project.id,
        kind="product_spec",
        schema_version=project.schema_version,
        document=project.to_dict(),
        expected_revision=0,
    )
    architecture_revision = store.save_revision(
        project.id,
        kind="architecture",
        schema_version=architecture.schema_version,
        document=architecture.to_dict(),
        expected_revision=1,
    )
    approval = store.decide_approval(
        store.request_approval(
            project.id,
            gate="architecture",
            request={
                "revision_id": architecture_revision.id,
                "spec_revision_id": spec_revision.id,
                "target_pack": architecture.target_pack,
                "node_ids": sorted(architecture.node_index),
            },
        )["id"],
        approved=True,
        decision={"actor": "test-approver"},
    )
    run = store.create_run(
        project.id,
        spec_revision_id=spec_revision.id,
        architecture_revision_id=architecture_revision.id,
        run_id="run.todo",
        status="ready",
        budget={
            "max_model_attempts": 8,
            "max_input_tokens": 100_000,
            "max_output_tokens": 100_000,
            "max_cost_usd": "20",
            "max_execution_seconds": 2_000,
        },
    )
    plan = compile_architecture(architecture, project)
    for task in plan.tasks:
        store.create_task(
            run["id"],
            node_id=task.node_id,
            kind="implement",
            task_id=f"{run['id']}:{task.task_id}",
            status="ready",
            dependency_task_ids=tuple(
                f"{run['id']}:implement:{dependency_id}"
                for dependency_id in task.dependency_ids
            ),
        )
    workspace = tmp_path / "workspace"
    NextJsTargetPack(
        NextJsTargetPackConfig(
            project_name="todo", project_spec=project, architecture=architecture
        )
    ).scaffold(workspace)
    manifest = store.put_artifact(
        (workspace / ".rich/target-pack.json").read_bytes(),
        media_type="application/vnd.rich.target-pack-manifest+json",
    )
    store.attach_artifact(run["id"], manifest.digest, role="scaffold_manifest")
    store.append_event(
        run["id"],
        "run.prepared",
        {"architecture_approval_id": approval["id"], "task_count": len(plan.tasks)},
    )
    store.append_event(
        run["id"],
        "scaffold.completed",
        {
            "destination": str(workspace.absolute()),
            "manifest_digest": manifest.digest,
        },
    )
    return {
        "store": store,
        "project": project,
        "architecture": architecture,
        "approval": approval,
        "run": run,
        "plan": plan,
        "workspace": workspace,
    }


def _evidence_record(state, kind):
    record = _artifacts_with_role(state, f"evidence:{kind}")[-1]
    return json.loads(state["store"].get_artifact(record["digest"]).path.read_text())


def _verification_document(state, kind):
    record = _artifacts_with_role(state, f"verification:{kind}")[-1]
    return json.loads(state["store"].get_artifact(record["digest"]).path.read_text())


def test_a_data_component_is_prepared_before_each_gate_that_runs_it_and_probed_after_acceptance(
    tmp_path,
):
    import hashlib

    from richbuild.run_engine import (
        DATABASE_PREPARE,
        DATABASE_PROBE,
        DatabaseStep,
        RunEngine,
        RunEngineConfig,
    )

    state = _persisting_state(tmp_path)
    provider = OwnedPathProvider()
    runner = DatabaseAwareRunner(tables={"projects": 0, "todos": 1})
    engine = RunEngine(
        state["store"],
        gateway=_gateway(provider),
        command_runner=runner,
        provider=provider.name,
        model="fake-code-model",
        config=RunEngineConfig(max_task_attempts=1),
    )

    report = engine.execute(run_id=state["run"]["id"], workspace=state["workspace"])

    assert report.succeeded, report
    kinds = [
        (command.kind, isinstance(command, DatabaseStep)) for command in runner.commands
    ]
    for index, (kind, is_step) in enumerate(kinds):
        if kind in {"unit", "property", "acceptance"}:
            assert kinds[index - 1] == (DATABASE_PREPARE, True), (
                f"{kind} at {index} was not preceded by a fresh database: {kinds}"
            )
        if kind in {"lint", "static", "build"}:
            assert kinds[index - 1] != (DATABASE_PREPARE, True), (
                f"{kind} runs no software and gets no database: {kinds}"
            )
        if kind == "acceptance":
            assert kinds[index + 1] == (DATABASE_PROBE, True), kinds
    assert sum(1 for kind, is_step in kinds if kind == DATABASE_PROBE) == 1, (
        "one probe, after the one browser run"
    )
    prepare_argv = [c.argv for c in runner.commands if c.kind == DATABASE_PREPARE][0]
    assert prepare_argv == RunEngineConfig().database_argv
    assert [c.argv for c in runner.commands if c.kind == DATABASE_PROBE] == [
        RunEngineConfig().probe_argv
    ]

    digest = hashlib.sha256(
        (state["workspace"] / "packages/db/migrations/0000_initial.sql").read_bytes()
    ).hexdigest()
    expected_set = [{"file": "0000_initial.sql", "sha256": digest}]
    acceptance = _evidence_record(state, "acceptance")
    database = acceptance["metadata"]["details"]["database"]
    assert database["migrations"] == expected_set, (
        "the migration digest set travels with the acceptance evidence"
    )
    assert database["tables"] == {"projects": 0, "todos": 1}
    assert database["rows"] == 1
    assert database["engine"]["name"] == "pglite"
    assert database["directory"] == ".rich/runtime/db"
    assert "1 row(s) across 2 table(s)" in acceptance["metadata"]["summary"]
    unit = _evidence_record(state, "unit")
    assert unit["metadata"]["details"]["database"]["migrations"] == expected_set
    assert "tables" not in unit["metadata"]["details"]["database"]
    document = _verification_document(state, "acceptance")
    assert document["database_preparation"]["status"] == "passed"
    assert document["database_preparation"]["report"]["migrations"] == expected_set
    assert document["database_probe"]["status"] == "passed"
    assert document["database_probe"]["report"]["tables"] == {"projects": 0, "todos": 1}


def test_a_data_component_that_persisted_nothing_fails_acceptance_closed(tmp_path):
    from richbuild.run_engine import RunEngine, RunEngineConfig

    state = _persisting_state(tmp_path)
    provider = OwnedPathProvider()
    runner = DatabaseAwareRunner(tables={"projects": 0, "todos": 0})
    engine = RunEngine(
        state["store"],
        gateway=_gateway(provider),
        command_runner=runner,
        provider=provider.name,
        model="fake-code-model",
        config=RunEngineConfig(max_task_attempts=1),
    )

    report = engine.execute(run_id=state["run"]["id"], workspace=state["workspace"])

    # The browser passed every scenario and the reporter said so. That is not
    # enough: a reload proves a record outlived the request, not that it
    # reached the database, and the tables say it did not.
    assert not report.succeeded
    acceptance = _evidence_record(state, "acceptance")
    assert acceptance["status"] == "failed"
    assert "persisted nothing" in acceptance["metadata"]["summary"]
    assert acceptance["acceptance_scenario_ids"] == []
    document = _verification_document(state, "acceptance")
    assert document["status"] == "failed"
    assert document["observed_acceptance_scenario_ids"] == []
    assert document["database_probe"]["status"] == "failed"
    events = {e["event_type"] for e in state["store"].list_events(state["run"]["id"])}
    assert "run.succeeded" not in events


def test_a_probe_failure_is_attributed_to_the_owners_of_the_pages_the_browser_ran(
    tmp_path,
):
    """M7's live proof: told only not to import a database driver, the web
    worker kept the todos in a `globalThis` array. `next start` is one
    long-lived process, so the array survived `page.reload()`, every browser
    step passed, and only the probe -- reading a database whose tables were
    all empty -- caught it. The root ran the browser but owns no page, so
    retrying it three times could never change the outcome. The owners of the
    pages the scenarios exercised are the ones that can."""

    from richbuild.run_engine import RunEngine, RunEngineConfig
    from richbuild.target_packs.nextjs import exercised_pages

    state = _persisting_state(tmp_path)
    provider = OwnedPathProvider()
    runner = DatabaseAwareRunner(tables={"projects": 0, "todos": 0})
    engine = RunEngine(
        state["store"],
        gateway=_gateway(provider),
        command_runner=runner,
        provider=provider.name,
        model="fake-code-model",
        config=RunEngineConfig(
            max_task_attempts=1, exercised_paths=exercised_pages
        ),
    )

    report = engine.execute(run_id=state["run"]["id"], workspace=state["workspace"])

    assert not report.succeeded
    verification = _artifacts_with_role(state, "verification:acceptance")[-1]
    assert verification["metadata"]["status"] == "failed"
    # `web` owns apps/web, where the scenario's page lives. The acceptance
    # command itself passed, so there is no failing step to name an owner:
    # the probe's verdict has to name them from the pages the browser opened.
    assert verification["metadata"]["attributed_node_ids"] == ["web"]
    acceptance = _evidence_record(state, "acceptance")
    assert "persisted nothing" in acceptance["metadata"]["summary"]


def test_a_migration_report_that_disagrees_with_the_files_on_disk_fails_the_gate(
    tmp_path,
):
    from richbuild.run_engine import RunEngine, RunEngineConfig

    state = _persisting_state(tmp_path)
    provider = OwnedPathProvider()
    runner = DatabaseAwareRunner(reported_sha="0" * 64)
    engine = RunEngine(
        state["store"],
        gateway=_gateway(provider),
        command_runner=runner,
        provider=provider.name,
        model="fake-code-model",
        config=RunEngineConfig(max_task_attempts=1),
    )

    report = engine.execute(run_id=state["run"]["id"], workspace=state["workspace"])

    # The sandbox ran model-authored SQL and reported what it journaled. That
    # report is a command result, not a claim the host takes on trust: the
    # host computed the set from the files, and the two must agree exactly.
    assert not report.succeeded
    unit = _evidence_record(state, "unit")
    assert unit["status"] == "failed"
    assert "not the set on disk" in unit["metadata"]["summary"]
    assert "database" not in unit["metadata"]["details"]
    steps = [c.kind for c in runner.commands if not hasattr(c, "expected_acceptance_scenario_ids")]
    assert steps and set(steps) == {"database-prepare"}, steps
    assert not {"unit", "property"} & {c.kind for c in runner.commands}, (
        "a gate whose database could not be prepared is not run"
    )


def test_the_probe_must_find_the_journal_the_prepare_step_recorded(tmp_path):
    from richbuild.run_engine import RunEngine, RunEngineConfig

    state = _persisting_state(tmp_path)
    provider = OwnedPathProvider()
    runner = DatabaseAwareRunner(
        probe_journal=[{"file": "0000_initial.sql", "sha256": "f" * 64}]
    )
    engine = RunEngine(
        state["store"],
        gateway=_gateway(provider),
        command_runner=runner,
        provider=provider.name,
        model="fake-code-model",
        config=RunEngineConfig(max_task_attempts=1),
    )

    report = engine.execute(run_id=state["run"]["id"], workspace=state["workspace"])

    assert not report.succeeded
    acceptance = _evidence_record(state, "acceptance")
    assert acceptance["status"] == "failed"
    assert "different migration journal" in acceptance["metadata"]["summary"]


def test_a_failed_preparation_withholds_the_gate_it_serves(tmp_path):
    from richbuild.run_engine import RunEngine, RunEngineConfig

    state = _persisting_state(tmp_path)
    provider = OwnedPathProvider()
    runner = DatabaseAwareRunner(prepare_returncode=1)
    engine = RunEngine(
        state["store"],
        gateway=_gateway(provider),
        command_runner=runner,
        provider=provider.name,
        model="fake-code-model",
        config=RunEngineConfig(max_task_attempts=1),
    )

    report = engine.execute(run_id=state["run"]["id"], workspace=state["workspace"])

    assert not report.succeeded
    unit = _evidence_record(state, "unit")
    assert unit["status"] == "failed"
    assert unit["metadata"]["summary"] == (
        "unit database preparation failed: the database-prepare step exited with 1"
    )
    document = _verification_document(state, "unit")
    assert document["database_preparation"]["status"] == "failed"
    assert document["database_preparation"]["returncode"] == 1
    assert "unit" not in [c.kind for c in runner.commands]


def test_an_application_without_a_data_component_runs_no_database_step(tmp_path):
    from richbuild.run_engine import DatabaseStep

    state = _prepared_state(tmp_path)
    provider = FakeModelProvider()
    runner = PassingCommandRunner()
    engine = _engine(state, provider, runner)

    report = engine.execute(run_id=state["run"]["id"], workspace=state["workspace"])

    assert report.succeeded
    assert not any(isinstance(c, DatabaseStep) for c in runner.commands)
    acceptance = _evidence_record(state, "acceptance")
    assert "database" not in acceptance["metadata"]["details"]


def test_gates_see_the_database_only_where_the_software_runs(tmp_path):
    """Build is deliberately blind: the deployed build has no database at
    build time either, so a page that reads one while being prerendered must
    fail here, not in production. Lint and typecheck run no code."""

    from richbuild.executor import BubblewrapExecutor, SandboxPolicy
    from richbuild.run_engine import (
        DATABASE_PREPARE,
        DATABASE_PROBE,
        BubblewrapCommandRunner,
        DatabaseStep,
        VerificationCommand,
    )

    seen = {}

    class RecordingExecutor(BubblewrapExecutor):
        def run(self, root, argv, policy=None, *, cancellation=None, deadline=None):
            assert isinstance(policy, SandboxPolicy)
            seen["policy"] = policy
            return ExecutionResult(
                argv=tuple(argv), returncode=0, stdout="", stderr="", duration_seconds=0.01
            )

    runner = BubblewrapCommandRunner(RecordingExecutor(executable="/usr/bin/python3"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    for kind in ("lint", "static", "build"):
        assert "RICH_DATABASE_DIR" not in runner.environment_for(kind)
        assert ".rich/runtime/db" not in runner.writable_paths_for(kind)
        runner.run(workspace, VerificationCommand(kind=kind, argv=("pnpm", "run", kind)))
        assert "RICH_DATABASE_DIR" not in seen["policy"].environment
        assert ".rich/runtime/db" not in seen["policy"].writable_paths
    for kind in ("unit", "property", "acceptance"):
        environment = runner.environment_for(kind)
        assert environment["RICH_DATABASE_DIR"] == "/workspace/.rich/runtime/db"
        assert runner.writable_paths_for(kind)[-1] == ".rich/runtime/db"
    for kind in (DATABASE_PREPARE, DATABASE_PROBE):
        runner.run(workspace, DatabaseStep(kind, ("node", "step.mjs")))
        policy = seen["policy"]
        assert policy.environment["RICH_DATABASE_DIR"] == "/workspace/.rich/runtime/db"
        assert ".rich/runtime/db" in policy.writable_paths
        assert policy.network is False
        # Node's ceiling, not the browser's: the steps are plain Node.
        assert policy.max_memory_bytes == runner.max_memory_bytes
    # 24 GiB: two 8 GiB V8 cages (a `--import` loader runs on a worker
    # thread) plus PGlite's WebAssembly heap measured 15.65 GiB, and a plain
    # next build 16.0 GiB, under the previous 16 GiB ceiling.
    assert runner.max_memory_bytes == 24 * 1024**3
    with pytest.raises(ValueError):
        DatabaseStep("database-anything", ("node",))
    with pytest.raises(ValueError):
        BubblewrapCommandRunner(
            RecordingExecutor(executable="/usr/bin/python3"),
            database_directory="../outside",
        )
