import json
from decimal import Decimal
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

import rich_v2.coding as coding_module
from rich_v2.budget import BudgetLedger, RunBudget, Usage
from rich_v2.coding import ApprovalWitness, CodingWorker
from rich_v2.compiler import compile_architecture
from rich_v2.execution import (
    DefaultRunExecutor,
    _LeaseBoundModelEventSink,
)
from rich_v2.executor import BubblewrapExecutor, ExecutionResult
from rich_v2.models import (
    AcceptanceScenario,
    ArchitectureNode,
    ArchitectureSpecV2,
    ContractV2,
    NodeKind,
    OperationContract,
    ProjectSpecV2,
    Requirement,
)
from rich_v2.providers import (
    MODEL_ATTEMPT_EVENT_SCHEMA,
    ModelGateway,
    ModelResponse,
    recover_model_usage,
)
from rich_v2.run_engine import (
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
from rich_v2.scheduler import CancellationToken
from rich_v2.store import RichStore, StoreError
from rich_v2.target_packs.nextjs import (
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
    project = ProjectSpecV2(
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
    contract = ContractV2(
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
    architecture = ArchitectureSpecV2(
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
    assert not Path("/proc", child_pid.read_text()).exists()
    time.sleep(0.65)
    assert not late_marker.exists()


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
        execution_lease_seconds=0.3,
        execution_heartbeat_seconds=0.1,
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
    assert renewal_failed.wait(2)
    thread.join(3)

    assert not thread.is_alive()
    assert "error" not in outcome
    assert outcome["report"].status == "canceled"
    assert runner.wait_for_idle(0.1)
    assert not Path("/proc", child_pid.read_text()).exists()
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
            execution_lease_seconds=0.3,
            execution_heartbeat_seconds=0.1,
        ),
        execution_owner_binding=model_events.bind,
    )
    real_renew = state["store"].renew_run_execution
    renewal_failed = threading.Event()

    def fail_renewal_while_provider_is_blocked(*args, **kwargs):
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
    assert provider.started.wait(2)
    assert renewal_failed.wait(2)

    takeover_deadline = time.monotonic() + 2
    successor = None
    while successor is None and time.monotonic() < takeover_deadline:
        try:
            successor = state["store"].claim_run_execution(
                state["run"]["id"]
            )
        except StoreError:
            time.sleep(0.02)
    assert successor is not None
    thread.join(2)
    assert not thread.is_alive()
    assert "error" not in outcome
    assert outcome["report"].status == "canceled"

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

    def runtime_builder(budget, *, event_history, event_sink):
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
            build_argv=("unused-build",),
            acceptance_argv=("unused-acceptance",),
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
    old_lease = store.claim_run_execution(run_id, lease_seconds=0.05)
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
            assert release_old.wait(3)
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
    assert old_applied.wait(2)
    assert generated_path.is_file()
    time.sleep(0.07)
    successor = store.claim_run_execution(run_id)
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
    assert recovery_started.wait(1)
    time.sleep(0.05)
    assert recovery_thread.is_alive()
    assert generated_path.is_file()

    release_old.set()
    old_thread.join(3)
    recovery_thread.join(3)

    assert not old_thread.is_alive()
    assert not recovery_thread.is_alive()
    assert "error" in old_outcome
    assert not recovery_outcome
    assert not generated_path.exists()
    transaction = store.list_source_transactions(run_id)[0]
    assert transaction["status"] == "rolled_back"
    assert transaction["resolved_owner_token"] == successor.owner_token
