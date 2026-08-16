"""The loop RICH exists for, end to end, with a real model.

Everything else in the suite verifies one link. This runs the whole chain on
one machine: an approved intent becomes an architecture, the architecture is
scaffolded into a frozen target pack, a real model authors source into paths it
is allowed to touch, and independent sandboxed gates decide whether any of it
is true.

It is skipped by default because it spends real model quota, downloads a
locked dependency graph, and takes minutes. Run it deliberately:

    python -m pytest --run-live --basetemp=.rich/live-loop \\
        tests/test_v2_closed_loop_live.py
"""

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from rich_v2.compiler import compile_architecture
from rich_v2.interview import AdaptiveInterview, InterviewState
from rich_v2.planner import plan_nextjs_architecture
from rich_v2.run_engine import BubblewrapCommandRunner, RunEngine, RunEngineConfig
from rich_v2.runtime import CLAUDE_CODE_ROUTE, default_run_runtime
from rich_v2.store import RichStore
from rich_v2.target_packs.nextjs import NextJsTargetPack, NextJsTargetPackConfig


ORACLE = (
    {"action": "navigate", "value": "/"},
    {
        "action": "assert_visible",
        "locator": {"kind": "text", "value": "Launch checklist"},
    },
)


def _project():
    return AdaptiveInterview(
        InterviewState(
            "project.closed-loop",
            "Closed Loop",
            answers={
                "goal": "Publish an accessible project launch checklist.",
                "audiences": ["technical founders"],
                "capabilities": [
                    {
                        "id": "req.checklist",
                        "title": "Launch checklist",
                        "statement": (
                            "A founder can review the approved launch checklist."
                        ),
                    }
                ],
                "quality_constraints": [
                    {
                        "id": "req.a11y",
                        "title": "Keyboard access",
                        "statement": "The checklist is operable with a keyboard.",
                    }
                ],
                "scenarios": [
                    {
                        "id": "scenario.checklist",
                        "title": "Review checklist",
                        "when": ["A founder opens the checklist."],
                        "then": ["The approved checklist is visible."],
                        "requirement_ids": ["req.checklist"],
                        "oracle": [
                            {"action": "open_requirement"},
                            {
                                "action": "assert_visible",
                                "locator": {
                                    "kind": "text",
                                    "value": (
                                        "A founder can review the approved "
                                        "launch checklist."
                                    ),
                                },
                            },
                        ],
                    },
                    {
                        "id": "scenario.keyboard",
                        "title": "Keyboard navigation",
                        "when": ["A founder uses only a keyboard."],
                        "then": ["The checklist remains operable."],
                        "requirement_ids": ["req.a11y"],
                        "oracle": [
                            {"action": "navigate", "value": "/"},
                            {
                                "action": "press",
                                "locator": {
                                    "kind": "role",
                                    "value": "link",
                                    "name": "Keyboard access",
                                },
                                "value": "Enter",
                            },
                            {
                                "action": "assert_visible",
                                "locator": {
                                    "kind": "text",
                                    "value": (
                                        "The checklist is operable with a keyboard."
                                    ),
                                },
                            },
                        ],
                    },
                ],
            },
        )
    ).compile()


def _prepare(tmp_path):
    """Bring a run to the exact state RunEngine.execute expects to resume."""

    project = _project()
    architecture = plan_nextjs_architecture(project).architecture
    architecture.validate_against_project(project)
    plan = compile_architecture(architecture, project)

    store = RichStore(tmp_path / "state")
    record = store.create_project(project.name, project_id=project.id)
    assert record["id"] == project.id
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
        decision={"actor": "closed-loop-live-test"},
    )
    run = store.create_run(
        project.id,
        spec_revision_id=spec_revision.id,
        architecture_revision_id=architecture_revision.id,
        run_id="run.closed-loop",
        status="ready",
        budget={
            "max_model_attempts": 12,
            "max_input_tokens": 400_000,
            "max_output_tokens": 200_000,
            # A ceiling, not an estimate. A run that needs more than this has
            # gone wrong in a way that should stop rather than keep spending.
            "max_cost_usd": "5",
            "max_execution_seconds": 3_600,
        },
    )
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
            project_name="closed-loop",
            project_spec=project,
            architecture=architecture,
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
        "run": run,
        "plan": plan,
        "workspace": workspace,
        "approval": approval,
    }


def _require_login():
    if shutil.which("claude") is None:
        pytest.skip("live test; the `claude` CLI is not on PATH")
    if not (Path.home() / ".claude" / ".credentials.json").exists():
        pytest.skip("live test; run `claude` once to log in first")


@pytest.mark.live
def test_a_real_model_authors_source_that_independent_gates_judge(tmp_path):
    _require_login()
    state = _prepare(tmp_path)
    events: list[tuple[str, dict]] = []

    runtime = default_run_runtime(
        state["store"].get_run(state["run"]["id"])["budget"],
        event_sink=lambda kind, payload: events.append((kind, dict(payload))),
        route=CLAUDE_CODE_ROUTE,
    )
    assert runtime.provider_name == "anthropic-claude-code"

    engine = RunEngine(
        state["store"],
        gateway=runtime.gateway,
        command_runner=BubblewrapCommandRunner(runtime.executor, timeout_seconds=900),
        provider=runtime.provider_name,
        model=runtime.model,
        workspace_preparer=runtime.bootstrapper,
        config=RunEngineConfig(
            max_task_attempts=2,
            task_timeout_seconds=1_800,
            # Route-specific: the CLI adds harness overhead on top of
            # generation and cannot cap output before the fact.
            coding_limits=runtime.coding_limits,
            lint_argv=runtime.commands.lint_argv,
            static_argv=runtime.commands.static_argv,
            unit_argv=runtime.commands.unit_argv,
            build_argv=runtime.commands.build_argv,
            acceptance_argv=runtime.commands.acceptance_argv,
        ),
    )

    report = engine.execute(
        run_id=state["run"]["id"],
        workspace=state["workspace"],
        architecture_approval_id=state["approval"]["id"],
    )

    durable = state["store"].list_events(state["run"]["id"])
    kinds = [event["event_type"] for event in durable]

    # Whatever the verdict, these must hold. A model call happened, it was
    # billed, and the answer came from the pinned model over the named route.
    attempts = [kind for kind, _ in events if kind == "model.attempt.started"]
    assert attempts, "no model attempt was ever recorded"
    assert runtime.ledger.usage.model_attempts >= 1
    assert runtime.ledger.usage.cost_usd > Decimal("0")
    assert runtime.ledger.usage.output_tokens > 0

    published = [
        event
        for event in durable
        if event["event_type"] == "evidence.recorded"
        and event.get("payload", {}).get("status") == "passed"
    ]
    kinds_published = {event["payload"]["kind"] for event in published}
    # Generation is never evidence of behavior. A run may record that source
    # was produced and applied, but the gate kinds are what a verdict rests on,
    # and at least one of them has to have actually run.
    assert kinds_published & {"lint", "static", "unit", "build", "acceptance"}, (
        f"nothing was independently verified: {sorted(kinds_published)}"
    )
    generation = [
        event for event in published if event["payload"]["kind"] == "generation"
    ]
    for event in generation:
        assert "verification was not run" in event["payload"]["summary"]

    # The model may or may not have produced code the gates accept -- that is
    # the open question this test exists to answer, and either answer is
    # informative. What must not happen is a run reporting success without
    # having run the gates at all.
    if report.succeeded:
        assert "run.succeeded" in kinds
        verified = {
            event["payload"]["kind"]
            for event in published
            if event["payload"]["kind"]
            in {"lint", "static", "unit", "build", "acceptance"}
        }
        assert verified == {"lint", "static", "unit", "build", "acceptance"}, (
            f"succeeded without the full gate set: {sorted(verified)}"
        )
    else:
        assert any(
            kind in {"run.failed", "run.canceled"} for kind in kinds
        ), kinds
