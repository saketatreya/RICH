"""M7's proof: a real model builds a todo list, and the row outlives the reload.

The drive's step 6 -- "reloads: it is still there" -- end to end with a real
model over the CLI route: an approved intent with a data component becomes
the deterministic planner's architecture, the pack scaffolds it with the
protected engine-selecting factory, the protected migrator and the probe, a
real model authors the schema, the migration, the domain and the page into
the paths it owns, and the independent gates decide -- with a fresh, migrated
database before every gate that runs the software and the probe after the
browser has run the approved oracle:

    open_requirement -> fill "Todo" -> click "Add" -> assert_visible "Buy milk"
    -> reload -> assert_visible "Buy milk"

What must hold, and is asserted rather than tolerated: the run succeeded;
acceptance coverage is exact; the probe counted at least one row; the
acceptance evidence carries the migration digest set the preview will be
held to; and the data component's property suite ran against the in-sandbox
database.

It is skipped by default: it spends `claude` quota (a few dollars), downloads
the locked dependency graph and Chromium unless RICH_LIVE_CACHE_ROOT holds
them, and takes minutes. Run it deliberately, never on a tmpfs:

    RICH_LIVE_CACHE_ROOT=.rich/live-cache python -m pytest --run-live \\
        --basetemp=.rich/live-m7 -q -s tests/test_persistence_live.py
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import time

import pytest

from liveutil import live_cache_root, require_claude_login

from richbuild.compiler import compile_architecture
from richbuild.executor import SandboxUnavailable, trusted_node_pnpm_runtime
from richbuild.interview import AdaptiveInterview, InterviewState
from richbuild.models import NodeKind
from richbuild.planner import plan_nextjs_architecture
from richbuild.preview import migration_digests
from richbuild.run_engine import BubblewrapCommandRunner, RunEngine, RunEngineConfig
from richbuild.runtime import CLAUDE_CODE_ROUTE, default_run_runtime
from richbuild.store import RichStore
from richbuild.target_packs.nextjs import (
    NextJsTargetPack,
    NextJsTargetPackConfig,
    exercised_pages,
)

TODO_REQUIREMENT = "req.todo"
A11Y_REQUIREMENT = "req.a11y"
SCENARIO = "scenario.todo-persists"
KEYBOARD_SCENARIO = "scenario.keyboard"


def _project():
    return AdaptiveInterview(
        InterviewState(
            "project.persistence",
            "Todo",
            answers={
                "goal": (
                    "A todo list whose items are stored in the database, so a "
                    "member's todos are still there after a page reload."
                ),
                "audiences": ["members"],
                "data_policy": ["A todo is kept until a member deletes it."],
                "capabilities": [
                    {
                        "id": TODO_REQUIREMENT,
                        "title": "Todo list",
                        "statement": (
                            "A member types a todo into the field labelled "
                            "'Todo', presses the 'Add' button, and the todo is "
                            "stored; after a reload it is still listed."
                        ),
                    }
                ],
                "quality_constraints": [
                    {
                        "id": A11Y_REQUIREMENT,
                        "title": "Keyboard access",
                        "statement": "The todo list is operable with a keyboard.",
                    }
                ],
                "scenarios": [
                    {
                        "id": KEYBOARD_SCENARIO,
                        "title": "Keyboard navigation",
                        "when": ["A member uses only a keyboard."],
                        "then": ["The todo list remains operable."],
                        "requirement_ids": [A11Y_REQUIREMENT],
                        "oracle": [
                            {"action": "open_requirement"},
                            {
                                "action": "assert_visible",
                                "locator": {
                                    "kind": "text",
                                    "value": "The todo list is operable with a keyboard.",
                                },
                            },
                        ],
                    },
                    {
                        "id": SCENARIO,
                        "title": "A todo survives a reload",
                        "given": ["The todo list is empty."],
                        "when": ["A member adds 'Buy milk'.", "They reload the page."],
                        "then": ["'Buy milk' is still listed."],
                        "requirement_ids": [TODO_REQUIREMENT],
                        "oracle": [
                            {"action": "open_requirement"},
                            {
                                "action": "fill",
                                "locator": {"kind": "label", "value": "Todo"},
                                "value": "Buy milk",
                            },
                            {
                                "action": "click",
                                "locator": {
                                    "kind": "role",
                                    "value": "button",
                                    "name": "Add",
                                },
                            },
                            {
                                "action": "assert_visible",
                                "locator": {"kind": "text", "value": "Buy milk"},
                            },
                            {"action": "reload"},
                            {
                                "action": "assert_visible",
                                "locator": {"kind": "text", "value": "Buy milk"},
                            },
                        ],
                    }
                ],
            },
        )
    ).compile()


def _prepare(tmp_path: Path):
    """Bring a run to the exact state RunEngine.execute expects to resume."""

    project = _project()
    architecture = plan_nextjs_architecture(project).architecture
    assert any(node.kind is NodeKind.DATA for node in architecture.nodes), (
        "the todo list persists, so the planner allocates a data component"
    )
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
        decision={"actor": "persistence-live-test"},
    )
    run = store.create_run(
        project.id,
        spec_revision_id=spec_revision.id,
        architecture_revision_id=architecture_revision.id,
        run_id="run.persistence",
        status="ready",
        budget={
            "max_model_attempts": 12,
            "max_input_tokens": 400_000,
            "max_output_tokens": 200_000,
            # A ceiling, not an estimate: four components, up to three
            # attempts each, on the CLI route's per-attempt ceiling.
            "max_cost_usd": "8",
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
        {"destination": str(workspace.absolute()), "manifest_digest": manifest.digest},
    )
    return {
        "store": store,
        "run": run,
        "plan": plan,
        "workspace": workspace,
        "approval": approval,
        "project": project,
        "architecture": architecture,
    }


def _artifact_documents(store, run_id, role):
    return [
        json.loads(store.get_artifact(record["digest"]).path.read_text("utf-8"))
        for record in store.list_run_artifacts(run_id)
        if record["role"] == role
    ]


@pytest.mark.live
def test_a_real_model_persists_a_todo_and_the_gates_prove_the_row_outlived_the_reload(
    tmp_path,
):
    require_claude_login()
    if shutil.which("bwrap") is None:
        pytest.skip("live test; Bubblewrap is not on PATH")
    try:
        trusted_node_pnpm_runtime()
    except SandboxUnavailable as exc:
        pytest.skip(f"live test; {exc}")

    state = _prepare(tmp_path)
    store = state["store"]
    run_id = state["run"]["id"]
    cache = live_cache_root()
    events: list[tuple[str, dict]] = []
    runtime = default_run_runtime(
        store.get_run(run_id)["budget"],
        event_sink=lambda kind, payload: events.append((kind, dict(payload))),
        route=CLAUDE_CODE_ROUTE,
        cache_root=cache,
    )
    assert runtime.provider_name == "anthropic-claude-code"
    engine = RunEngine(
        store,
        gateway=runtime.gateway,
        command_runner=BubblewrapCommandRunner(
            runtime.executor, timeout_seconds=900, cache_root=cache
        ),
        provider=runtime.provider_name,
        model=runtime.model,
        workspace_preparer=runtime.bootstrapper,
        config=RunEngineConfig(
            max_task_attempts=3,
            task_timeout_seconds=1_800,
            coding_limits=runtime.coding_limits,
            lint_argv=runtime.commands.lint_argv,
            static_argv=runtime.commands.static_argv,
            unit_argv=runtime.commands.unit_argv,
            property_argv=runtime.commands.property_argv,
            build_argv=runtime.commands.build_argv,
            acceptance_argv=runtime.commands.acceptance_argv,
            database_argv=runtime.commands.database_argv,
            probe_argv=runtime.commands.probe_argv,
            exercised_paths=exercised_pages,
        ),
    )

    started = time.monotonic()
    report = engine.execute(
        run_id=run_id,
        workspace=state["workspace"],
        architecture_approval_id=state["approval"]["id"],
    )
    wall = time.monotonic() - started

    durable = store.list_events(run_id)
    evidence = [
        event for event in durable if event["event_type"] == "evidence.recorded"
    ]
    verifications = _artifact_documents(store, run_id, "verification:acceptance")
    acceptance_records = [
        record
        for record in _artifact_documents(store, run_id, "evidence:acceptance")
        if record["status"] == "passed"
    ]
    probe_lines = [
        line
        for document in verifications
        for line in document.get("database_probe", {}).get("stdout", "").splitlines()
        if line.startswith("RICH_DATABASE_PROBE ")
    ]
    gate_seconds = {}
    for kind in ("lint", "static", "unit", "property", "build", "acceptance"):
        documents = _artifact_documents(store, run_id, f"verification:{kind}")
        gate_seconds[kind] = [
            round(document.get("duration_seconds", 0.0), 1) for document in documents
        ]
    model_failures = [
        payload for kind, payload in events if kind == "model.attempt.failed"
    ]
    measurements = {
        "succeeded": report.succeeded,
        "status": report.status,
        "wall_seconds": round(wall, 1),
        "cache_root": str(cache) if cache else None,
        "model_attempts": runtime.ledger.usage.model_attempts,
        "cost_usd": str(runtime.ledger.usage.cost_usd),
        "input_tokens": runtime.ledger.usage.input_tokens,
        "output_tokens": runtime.ledger.usage.output_tokens,
        "gate_seconds": gate_seconds,
        "evidence": [
            f"{event['payload']['kind']}:{event['payload']['status']}"
            f"[{event.get('task_id', '').rsplit(':', 1)[-1]}]"
            for event in evidence
        ],
        "model_failures": [
            str(payload.get("error") or payload.get("message") or payload)[:300]
            for payload in model_failures
        ],
        "probe_lines": probe_lines,
        "acceptance_summaries": [
            event["payload"]["summary"]
            for event in evidence
            if event["payload"]["kind"] == "acceptance"
        ],
    }
    summary = json.dumps(measurements, indent=2)
    (tmp_path / "persistence-live-measurements.json").write_text(summary, "utf-8")
    print(f"\nPERSISTENCE LIVE MEASUREMENTS\n{summary}")

    # Whatever the verdict: a real model was called, over the named route,
    # and billed.
    assert runtime.ledger.usage.model_attempts >= 1
    assert runtime.ledger.usage.output_tokens > 0

    assert report.succeeded, (
        "the run did not succeed; the gates' own words:\n"
        + "\n".join(
            f"- {event['payload']['kind']} {event['payload']['status']}: "
            f"{event['payload']['summary']}"
            for event in evidence
            if event["payload"]["status"] != "passed"
        )
        + "\n"
        + summary
    )
    assert "run.succeeded" in {event["event_type"] for event in durable}

    # Coverage is exact: every approved scenario, bound to this attempt.
    record = acceptance_records[-1]
    assert record["acceptance_scenario_ids"] == sorted([KEYBOARD_SCENARIO, SCENARIO])
    database = record["metadata"]["details"]["database"]
    # The probe counted the row the browser created; a reload alone would
    # not have proved it reached the database.
    assert database["rows"] >= 1, database
    assert sum(database["tables"].values()) == database["rows"]
    assert database["engine"]["name"] == "pglite"
    assert "PostgreSQL" in database["engine"]["server_version"]
    # The migration set the preview will be held to is the one on disk.
    assert database["migrations"] == [
        entry.as_dict() for entry in migration_digests(state["workspace"])
    ]
    assert database["migrations"], "a data component with no migration persists nothing"
    assert len(probe_lines) >= 1
    assert json.loads(probe_lines[-1][len("RICH_DATABASE_PROBE ") :])["tables"] == (
        database["tables"]
    )

    # The data component's property suite ran against the in-sandbox
    # database: its property evidence passed and carries the migration set
    # it was prepared with.
    data_task = f"{run_id}:implement:data"
    data_property = [
        event
        for event in evidence
        if event.get("task_id") == data_task
        and event["payload"]["kind"] == "property"
        and event["payload"]["status"] == "passed"
    ]
    assert data_property, "the data node's property suite did not run and pass"
    property_records = [
        record
        for record in _artifact_documents(store, run_id, "evidence:property")
        if record["status"] == "passed" and record["task_id"] == data_task
    ]
    assert property_records[-1]["metadata"]["details"]["database"]["migrations"] == (
        database["migrations"]
    )
    # Every gate ran again over the whole application at the root.
    assert {"lint", "static", "unit", "property", "build", "acceptance"} <= {
        event["payload"]["kind"]
        for event in evidence
        if event["payload"]["status"] == "passed"
    }
