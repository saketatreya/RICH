from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from richbuild.control_plane import ApprovalRequired, ControlPlane
from richbuild.preview import PreviewResult, create_deployment_snapshot
from richbuild.store import RichStore


def _answers():
    return {
        "goal": "A persistent todo application for signed-in teams",
        "audiences": ["technical founders"],
        "roles": ["Members manage tasks only in their own team."],
        "capabilities": [
            {
                "id": "req.todo",
                "title": "Manage todos",
                "statement": "A member adds a task that remains after refresh.",
            }
        ],
        "data_policy": ["Tasks remain until explicitly deleted."],
        "quality_constraints": [
            {
                "id": "req.a11y",
                "title": "Keyboard access",
                "statement": "All todo actions are keyboard accessible.",
            }
        ],
        "scenarios": [
            {
                "id": "scenario.todo",
                "title": "Add todo",
                "when": ["A member adds Buy milk."],
                "then": ["Buy milk remains after refresh."],
                "requirement_ids": ["req.todo"],
                "oracle": [
                    {"action": "navigate", "value": "/"},
                    {
                        "action": "fill",
                        "locator": {"kind": "label", "value": "New task"},
                        "value": "Buy milk",
                    },
                    {
                        "action": "click",
                        "locator": {
                            "kind": "role",
                            "value": "button",
                            "name": "Add task",
                        },
                    },
                    {"action": "reload"},
                    {
                        "action": "assert_visible",
                        "locator": {"kind": "text", "value": "Buy milk"},
                    },
                ],
            },
            {
                "id": "scenario.a11y",
                "title": "Keyboard todo",
                "when": ["A member uses only the keyboard."],
                "then": ["They can add and complete a todo."],
                "requirement_ids": ["req.a11y"],
                "oracle": [
                    {"action": "navigate", "value": "/"},
                    {"action": "keyboard", "value": "Tab"},
                    {
                        "action": "assert_visible",
                        "locator": {"kind": "role", "value": "textbox"},
                    },
                ],
            },
        ],
    }


def _budget():
    return {
        "max_model_attempts": 20,
        "max_input_tokens": 320_000,
        "max_output_tokens": 160_000,
        "max_cost_usd": "10.00",
        "max_execution_seconds": 2_400,
    }


def _approved_architecture(control_plane):
    project = control_plane.create_project(project_id="project.todo", name="Founder Todo")
    spec = control_plane.submit_interview(
        project_id=project["id"],
        project_name=project["name"],
        answers=_answers(),
        expected_revision=0,
    )
    control_plane.decide_approval(
        spec.approval["id"], approved=True, actor="founder"
    )
    architecture = control_plane.propose_architecture(
        project_id=project["id"],
        spec_revision_id=spec.revision.id,
        spec_approval_id=spec.approval["id"],
        expected_revision=1,
    )
    control_plane.decide_approval(
        architecture.approval["id"], approved=True, actor="founder"
    )
    return project, spec, architecture


class RecordingPreviewOrchestrator:
    def __init__(self):
        self.created = []
        self.created_package_json = []
        self.destroyed = []

    def create(self, request):
        self.created.append(request)
        self.created_package_json.append(
            (request.source_dir / "package.json").read_text()
        )
        return PreviewResult(
            run_id=request.run_id,
            provider="vercel",
            deployment_id="dpl_approved",
            preview_url="https://approved-preview.vercel.app",
            database_provider="neon",
            database_project_id=request.neon_project_id,
            database_branch_id="br_approved",
            database_branch_name=request.neon_branch_name,
            expires_at=request.expires_at.isoformat(),
        )

    def destroy(self, request, result):
        self.destroyed.append((request, result))


def _scaffolded_preview_run(tmp_path, orchestrator):
    control_plane = ControlPlane(
        RichStore(tmp_path / "state"),
        preview_orchestrator=orchestrator,
    )
    _, _, architecture = _approved_architecture(control_plane)
    prepared = control_plane.prepare_run(
        architecture_approval_id=architecture.approval["id"],
        budget=_budget(),
    )
    destination = tmp_path / "generated"
    control_plane.scaffold_run(
        run_id=prepared.run["id"],
        destination=destination,
    )
    control_plane.store.set_run_status(
        prepared.run["id"],
        "running",
        expected_status="ready",
    )
    control_plane.store.set_run_status(
        prepared.run["id"],
        "verifying",
        expected_status="running",
    )
    control_plane.store.set_run_status(
        prepared.run["id"],
        "succeeded",
        expected_status="verifying",
    )
    snapshot = control_plane.store.put_artifact(
        create_deployment_snapshot(destination),
        media_type="application/vnd.rich.release-source+zip",
    )
    control_plane.store.attach_artifact(
        prepared.run["id"], snapshot.digest, role="source:release-snapshot"
    )
    return control_plane, prepared, destination


def test_architecture_cannot_be_proposed_before_spec_approval(tmp_path):
    control_plane = ControlPlane(RichStore(tmp_path))
    project = control_plane.create_project(
        project_id="project.todo", name="Founder Todo"
    )
    spec = control_plane.submit_interview(
        project_id=project["id"],
        project_name=project["name"],
        answers=_answers(),
        expected_revision=0,
    )

    with pytest.raises(ApprovalRequired, match="requested"):
        control_plane.propose_architecture(
            project_id=project["id"],
            spec_revision_id=spec.revision.id,
            spec_approval_id=spec.approval["id"],
            expected_revision=1,
        )


def test_approved_flow_compiles_durable_tasks_and_scaffolds(tmp_path):
    state = tmp_path / "state"
    control_plane = ControlPlane(RichStore(state))
    _, _, architecture = _approved_architecture(control_plane)

    prepared = control_plane.prepare_run(
        architecture_approval_id=architecture.approval["id"],
        budget=_budget(),
    )
    destination = tmp_path / "generated"
    scaffold = control_plane.scaffold_run(
        run_id=prepared.run["id"],
        destination=destination,
        package_scope="@founder",
    )

    assert prepared.run["status"] == "ready"
    assert prepared.compiled.tasks[-1].node_id == "app"
    assert len(prepared.tasks) == len(prepared.compiled.tasks)
    assert scaffold.manifest.target_pack == "nextjs-app-router"
    assert (destination / "apps/web/src/app/page.tsx").is_file()
    event_types = [
        event["event_type"]
        for event in control_plane.store.list_events(prepared.run["id"])
    ]
    assert event_types[0] == "run.prepared"
    assert event_types[-1] == "scaffold.completed"


def test_wrong_revision_approval_cannot_authorize_architecture(tmp_path):
    control_plane = ControlPlane(RichStore(tmp_path))
    project = control_plane.create_project(
        project_id="project.todo", name="Founder Todo"
    )
    spec = control_plane.submit_interview(
        project_id=project["id"],
        project_name=project["name"],
        answers=_answers(),
        expected_revision=0,
    )
    control_plane.decide_approval(
        spec.approval["id"], approved=True, actor="founder"
    )

    with pytest.raises(ApprovalRequired, match="different revision"):
        control_plane.propose_architecture(
            project_id=project["id"],
            spec_revision_id="revision_not_approved",
            spec_approval_id=spec.approval["id"],
            expected_revision=1,
        )


def test_preview_is_bound_to_approval_source_and_durable_lifecycle(tmp_path):
    orchestrator = RecordingPreviewOrchestrator()
    control_plane, prepared, destination = _scaffolded_preview_run(
        tmp_path, orchestrator
    )
    submission = control_plane.request_preview(
        run_id=prepared.run["id"],
        source_dir=destination,
        neon_project_id="neon-project-1",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )

    with pytest.raises(ApprovalRequired, match="requested"):
        control_plane.deploy_preview(
            preview_id=submission.preview["id"],
            approval_id=submission.approval["id"],
        )

    control_plane.decide_approval(
        submission.approval["id"], approved=True, actor="founder"
    )
    deployed = control_plane.deploy_preview(
        preview_id=submission.preview["id"],
        approval_id=submission.approval["id"],
    )

    assert deployed.preview["status"] == "ready"
    assert deployed.result.preview_url == "https://approved-preview.vercel.app"
    assert "connection_uri" not in repr(deployed.preview)
    reopened = RichStore(tmp_path / "state").get_preview(
        submission.preview["id"]
    )
    assert reopened["result"]["database_branch_id"] == "br_approved"

    destroyed = control_plane.destroy_preview(
        preview_id=submission.preview["id"]
    )
    assert destroyed["status"] == "destroyed"
    assert destroyed["destroyed_at"] is not None
    assert len(orchestrator.destroyed) == 1


def test_preview_deploys_immutable_approved_snapshot_if_live_source_changes(tmp_path):
    orchestrator = RecordingPreviewOrchestrator()
    control_plane, prepared, destination = _scaffolded_preview_run(
        tmp_path, orchestrator
    )
    approved_package = (destination / "package.json").read_text()
    submission = control_plane.request_preview(
        run_id=prepared.run["id"],
        source_dir=destination,
        neon_project_id="neon-project-1",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    control_plane.decide_approval(
        submission.approval["id"], approved=True, actor="founder"
    )
    (destination / "package.json").write_text('{"name":"changed"}')

    deployed = control_plane.deploy_preview(
        preview_id=submission.preview["id"],
        approval_id=submission.approval["id"],
    )

    assert deployed.preview["status"] == "ready"
    assert orchestrator.created_package_json == [approved_package]
    assert (
        submission.preview["request"]["source_snapshot_digest"]
        == submission.approval["request"]["source_snapshot_digest"]
    )


def test_preview_request_rejects_source_changed_after_release_evidence(tmp_path):
    control_plane, prepared, destination = _scaffolded_preview_run(
        tmp_path, RecordingPreviewOrchestrator()
    )
    (destination / "package.json").write_text('{"name":"not-the-release"}\n')

    with pytest.raises(ValueError, match="verified release snapshot"):
        control_plane.request_preview(
            run_id=prepared.run["id"],
            source_dir=destination,
            neon_project_id="neon-project-1",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )


def test_rebuilding_one_node_forgets_only_that_node(tmp_path):
    """The Canvas is shaped around this operation; v2's only granularity used
    to be the whole run."""

    store = RichStore(tmp_path / "state")
    control_plane = ControlPlane(store)
    project = store.create_project("Demo", project_id="project.rebuild")
    for index, node in enumerate(("web", "domain")):
        store.put_generation_memo(
            f"{index}" + "c" * 63,
            payload=b'{"schema":"rich.generation-memo/v1","bundle":{"summary":"s","files":[]}}',
            project_id=project["id"],
            node_id=node,
            provider="anthropic",
            model="claude-sonnet-5",
            run_id="run.1",
            task_id=f"run.1:implement:{node}",
        )

    result = control_plane.rebuild_node(project_id=project["id"], node_id="web")

    assert result["forgotten_generations"] == 1
    assert result["node_id"] == "web"
    assert store.get_generation_memo("0" + "c" * 63) is None
    assert store.get_generation_memo("1" + "c" * 63) is not None, "sibling kept"


def test_rebuilding_an_unknown_node_of_a_known_architecture_is_refused(tmp_path):
    store = RichStore(tmp_path / "state")
    control_plane = ControlPlane(store)
    project, _, architecture = _approved_architecture(control_plane)

    with pytest.raises(ValueError, match="not in that architecture"):
        control_plane.rebuild_node(
            project_id=project["id"],
            node_id="nonexistent",
            architecture_revision_id=architecture.revision.id,
        )
    # Without the architecture named, nothing is claimed about the node.
    assert (
        control_plane.rebuild_node(
            project_id=project["id"], node_id="nonexistent"
        )["forgotten_generations"]
        == 0
    )
    # And a real node of that architecture is accepted.
    assert control_plane.rebuild_node(
        project_id=project["id"],
        node_id="web",
        architecture_revision_id=architecture.revision.id,
    )["node_id"] == "web"


def test_the_workspace_boundary_belongs_to_the_control_plane(tmp_path):
    """It used to live in the HTTP dispatch table, so every other caller --
    the CLI, a future surface -- inherited nothing."""

    store = RichStore(tmp_path / "state")
    root = tmp_path / "workspaces"
    root.mkdir()
    confined = ControlPlane(store, workspace_root=root)
    unconfined = ControlPlane(store)

    assert confined._confined("build", label="d") == (root / "build").resolve()
    for escape in ("../outside", "/etc", str(tmp_path / "elsewhere")):
        with pytest.raises(ValueError, match="inside the configured workspace root"):
            confined._confined(escape, label="d")
    with pytest.raises(ValueError, match="cannot be the workspace root"):
        confined._confined(str(root), label="d")

    # An operator at their own shell can already write anywhere this process
    # can, so the CLI's control plane deliberately sets no root.
    assert unconfined._confined("../outside", label="d") == Path("../outside")


def test_scaffold_and_execute_both_refuse_an_escaping_destination(tmp_path):
    store = RichStore(tmp_path / "state")
    root = tmp_path / "workspaces"
    root.mkdir()

    class _Executor:
        def __init__(self):
            self.calls = []

        def execute(self, *, run_id, workspace, architecture_approval_id=None):
            self.calls.append(workspace)
            return "ok"

    executor = _Executor()
    control_plane = ControlPlane(store, run_executor=executor, workspace_root=root)

    with pytest.raises(ValueError, match="inside the configured workspace root"):
        control_plane.scaffold_run(run_id="run.x", destination="../escape")
    with pytest.raises(ValueError, match="inside the configured workspace root"):
        control_plane.execute_run(run_id="run.x", workspace="../escape")
    assert executor.calls == [], "the escape never reached the engine"
