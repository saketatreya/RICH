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
    _attach_acceptance_evidence(control_plane.store, prepared.run["id"], destination)
    return control_plane, prepared, destination


def _attach_acceptance_evidence(store, run_id, destination, *, migrations=None):
    """What a succeeded run with a data component always has: passed
    acceptance evidence recording the migration set the gates ran against."""

    from richbuild.canonical import canonical_json_bytes
    from richbuild.models import Evidence
    from richbuild.preview import migration_digests

    recorded = (
        [entry.as_dict() for entry in migration_digests(destination)]
        if migrations is None
        else migrations
    )
    result = store.put_artifact(
        canonical_json_bytes({"schema_version": "rich.command-verification/v1"}),
        media_type="application/vnd.rich.evidence-result+json",
    )
    record = Evidence(
        id="evidence.acceptance.fixture",
        run_id=run_id,
        kind="acceptance",
        status="passed",
        requirement_ids=("req.todo", "req.a11y"),
        acceptance_scenario_ids=("scenario.todo", "scenario.a11y"),
        artifact_ids=(result.digest,),
        metadata={
            "summary": "acceptance command passed; the database holds 1 row(s)",
            "details": {
                "database": {
                    "directory": ".rich/runtime/db",
                    "engine": {
                        "name": "pglite",
                        "server_version": "PostgreSQL 18.3 (PGlite 0.5.8) on wasm32",
                    },
                    "migrations": recorded,
                    "tables": {"todos": 1},
                    "rows": 1,
                }
            },
        },
    )
    artifact = store.put_artifact(
        canonical_json_bytes(record.to_dict()),
        media_type="application/vnd.rich.evidence+json",
    )
    store.attach_artifact(run_id, artifact.digest, role="evidence:acceptance")


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


def _amended_spec_revision(store, control_plane, project, spec, architecture):
    """A second approved spec revision with one requirement reworded."""

    document = dict(spec.revision.document)
    requirements = [dict(item) for item in document["requirements"]]
    requirements[0] = {
        **requirements[0],
        "statement": requirements[0]["statement"].replace(".", " today."),
    }
    document["requirements"] = requirements
    return store.save_revision(
        project["id"],
        kind="product_spec",
        schema_version=document["schema_version"],
        document=document,
        expected_revision=store.get_project(project["id"])["current_revision"],
    )


def test_a_change_plan_reads_without_deciding_anything(tmp_path):
    store = RichStore(tmp_path / "state")
    control_plane = ControlPlane(store)
    project, spec, architecture = _approved_architecture(control_plane)
    amended = _amended_spec_revision(store, control_plane, project, spec, architecture)
    store.put_generation_memo(
        "e" * 64,
        payload=b'{"schema":"rich.generation-memo/v1","bundle":{"summary":"s","files":[]}}',
        project_id=project["id"],
        node_id="domain",
        provider="anthropic",
        model="claude-sonnet-5",
        run_id="run.1",
        task_id="run.1:implement:domain",
    )

    planned = control_plane.plan_change(
        project_id=project["id"],
        from_spec_revision_id=spec.revision.id,
        to_spec_revision_id=amended.id,
        from_architecture_revision_id=architecture.revision.id,
        to_architecture_revision_id=architecture.revision.id,
    )

    assert planned["change"]["requirements"]["modified"], "the amendment was seen"
    assert "forgotten" not in planned, "planning decides nothing"
    assert store.get_generation_memo("e" * 64) is not None, "and forgets nothing"


def test_applying_a_change_forgets_exactly_the_stale_components(tmp_path):
    store = RichStore(tmp_path / "state")
    control_plane = ControlPlane(store)
    project, spec, architecture = _approved_architecture(control_plane)
    amended = _amended_spec_revision(store, control_plane, project, spec, architecture)
    for index, node in enumerate(("domain", "web", "app")):
        store.put_generation_memo(
            f"{index}" + "e" * 63,
            payload=b'{"schema":"rich.generation-memo/v1","bundle":{"summary":"s","files":[]}}',
            project_id=project["id"],
            node_id=node,
            provider="anthropic",
            model="claude-sonnet-5",
            run_id="run.1",
            task_id=f"run.1:implement:{node}",
        )

    applied = control_plane.apply_change(
        project_id=project["id"],
        from_spec_revision_id=spec.revision.id,
        to_spec_revision_id=amended.id,
        from_architecture_revision_id=architecture.revision.id,
        to_architecture_revision_id=architecture.revision.id,
    )

    stale = set(applied["change"]["stale"])
    assert stale, "an amendment has to make something stale"
    assert set(applied["forgotten"]) == stale
    for index, node in enumerate(("domain", "web", "app")):
        remembered = store.get_generation_memo(f"{index}" + "e" * 63)
        assert (remembered is None) == (node in stale), node


def test_a_revision_from_another_project_is_refused(tmp_path):
    store = RichStore(tmp_path / "state")
    control_plane = ControlPlane(store)
    project, spec, architecture = _approved_architecture(control_plane)
    other = store.create_project("Other", project_id="project.other")

    with pytest.raises(ValueError, match="not a product_spec of that project"):
        control_plane.plan_change(
            project_id=other["id"],
            from_spec_revision_id=spec.revision.id,
            to_spec_revision_id=spec.revision.id,
            from_architecture_revision_id=architecture.revision.id,
            to_architecture_revision_id=architecture.revision.id,
        )


def test_project_state_restores_everything_a_surface_needs(tmp_path):
    control_plane = ControlPlane(RichStore(tmp_path / "state"))
    control_plane.create_project(project_id="project.blank", name="Blank")

    blank = control_plane.project_state("project.blank")

    assert blank["project"]["id"] == "project.blank"
    assert blank["spec"] is None and blank["architecture"] is None
    assert blank["runs"] == [] and blank["prepared"] is None
    assert blank["scaffold"] is None and blank["interview"] is None

    project, spec, architecture = _approved_architecture(control_plane)
    control_plane.save_interview_draft(
        project["id"], document={"form": {"goal": "draft"}}, expected_draft_revision=0
    )
    prepared = control_plane.prepare_run(
        architecture_approval_id=architecture.approval["id"], budget=_budget()
    )
    control_plane.scaffold_run(
        run_id=prepared.run["id"], destination=tmp_path / "generated"
    )

    state = control_plane.project_state(project["id"])

    assert state["spec"]["revision"]["id"] == spec.revision.id
    assert state["spec"]["approval"]["status"] == "approved"
    assert state["spec"]["spec"]["name"] == spec.spec.name
    assert state["architecture"]["revision"]["id"] == architecture.revision.id
    assert state["architecture"]["approval"]["status"] == "approved"
    assert state["architecture"]["decisions"] == list(architecture.proposal.decisions)
    assert state["architecture"]["architecture"]["nodes"]
    assert [run["id"] for run in state["runs"]] == [prepared.run["id"]]
    assert state["prepared"]["run"]["id"] == prepared.run["id"]
    assert state["prepared"]["plan_artifact_digest"] == prepared.plan_artifact.digest
    assert state["prepared"]["compiled"]["tasks"]
    assert {task["node_id"] for task in state["prepared"]["tasks"]} == {
        task.node_id for task in prepared.compiled.tasks
    }
    assert state["scaffold"]["destination"] == str((tmp_path / "generated").absolute())
    assert state["scaffold"]["manifest"]["content_digest"]
    assert state["interview"]["document"] == {"form": {"goal": "draft"}}


def test_submitting_the_interview_marks_its_draft_but_keeps_it(tmp_path):
    control_plane = ControlPlane(RichStore(tmp_path))
    project = control_plane.create_project(project_id="project.todo", name="Todo")
    control_plane.save_interview_draft(
        project["id"], document={"form": _answers()}, expected_draft_revision=0
    )

    spec = control_plane.submit_interview(
        project_id=project["id"],
        project_name=project["name"],
        answers=_answers(),
        expected_revision=0,
    )

    draft = control_plane.get_interview_draft(project["id"])
    assert draft["submitted_revision_id"] == spec.revision.id
    assert draft["document"] == {"form": _answers()}


class _ScriptedInterviewer:
    """Answers each turn with the next scripted outcome."""

    def __init__(self, *outcomes):
        from richbuild.interviewer import InterviewOutcome

        self.outcomes = list(outcomes)
        self.calls = []
        self._outcome_type = InterviewOutcome

    def turn(self, *, project_id, project_name, transcript, answers):
        self.calls.append({"transcript": list(transcript), "answers": answers})
        return self.outcomes.pop(0)


def _outcome(status, **fields):
    from richbuild.interviewer import InterviewOutcome

    base = {
        "summary": "",
        "questions": (),
        "answers": None,
        "rejections": (),
        "attempts": 1,
        "source": "model",
    }
    base.update(fields)
    return InterviewOutcome(status=status, **base)


def test_interview_turn_records_the_conversation_and_the_answers_on_the_draft(tmp_path):
    interviewer = _ScriptedInterviewer(
        _outcome(
            "questions",
            summary="Two things first.",
            questions=({"prompt": "Who can see whose items?", "why": "Roles decide isolation."},),
        ),
        _outcome("complete", summary="A todo list.", answers=_answers()),
    )
    control_plane = ControlPlane(RichStore(tmp_path), interviewer=interviewer)
    project = control_plane.create_project(project_id="project.todo", name="Todo")

    first = control_plane.interview_turn(
        project["id"], message="A todo list for my team.", expected_draft_revision=0
    )

    assert first["outcome"]["status"] == "questions"
    assert first["outcome"]["questions"][0]["prompt"] == "Who can see whose items?"
    assert first["draft"]["draft_revision"] == 1
    transcript = first["draft"]["document"]["transcript"]
    assert [turn["role"] for turn in transcript] == ["user", "interviewer"]
    assert transcript[0]["text"] == "A todo list for my team."
    assert first["draft"]["document"]["answers"] is None
    assert interviewer.calls[0]["answers"] is None

    second = control_plane.interview_turn(
        project["id"], message="Everyone on the team sees everything.", expected_draft_revision=1
    )

    assert second["outcome"]["status"] == "complete"
    assert second["draft"]["draft_revision"] == 2
    assert second["draft"]["document"]["answers"] == _answers()
    # The interviewer saw the whole conversation, including its own question.
    assert [turn["role"] for turn in interviewer.calls[1]["transcript"]] == [
        "user", "interviewer", "user",
    ]
    # And what it produced compiles into a spec exactly as the form's answers do.
    spec = control_plane.submit_interview(
        project_id=project["id"],
        project_name=project["name"],
        answers=second["draft"]["document"]["answers"],
        expected_revision=0,
    )
    assert spec.spec.requirements


def test_interview_turn_without_an_interviewer_asks_the_fixed_questions(tmp_path):
    control_plane = ControlPlane(RichStore(tmp_path))
    project = control_plane.create_project(project_id="project.todo", name="Todo")

    result = control_plane.interview_turn(
        project["id"], message="A todo list.", expected_draft_revision=0
    )

    assert result["outcome"]["source"] == "form-fallback"
    assert result["outcome"]["status"] == "questions"
    assert result["outcome"]["questions"]


def test_interview_turn_guards_the_draft_revision_and_the_message(tmp_path):
    from richbuild.store import RevisionConflict

    control_plane = ControlPlane(RichStore(tmp_path))
    project = control_plane.create_project(project_id="project.todo", name="Todo")
    control_plane.interview_turn(project["id"], message="Hello.", expected_draft_revision=0)

    with pytest.raises(RevisionConflict):
        control_plane.interview_turn(project["id"], message="Again.", expected_draft_revision=0)
    with pytest.raises(ValueError, match="needs a message"):
        control_plane.interview_turn(project["id"], message="   ", expected_draft_revision=1)


def test_a_redraft_starts_from_the_last_approved_design(tmp_path):
    """The architect is handed the approved spec and architecture it is
    redrafting from, so a layer the amendment does not touch can keep its
    contract exactly -- and before any design is approved it is handed nothing."""

    class RecordingArchitect:
        def __init__(self):
            self.previous = []

        def propose(self, spec, *, target_pack, repair=None, previous=None):
            from richbuild.planner import plan_nextjs_architecture

            self.previous.append(previous)
            return plan_nextjs_architecture(spec)

    architect = RecordingArchitect()
    control_plane = ControlPlane(RichStore(tmp_path), architect=architect)
    project = control_plane.create_project(project_id="project.todo", name="Founder Todo")
    spec = control_plane.submit_interview(
        project_id=project["id"], project_name=project["name"], answers=_answers(), expected_revision=0
    )
    control_plane.decide_approval(spec.approval["id"], approved=True, actor="founder")
    assert control_plane.approved_designs(project["id"]) == []

    first = control_plane.draft_architecture(
        project_id=project["id"], spec_revision_id=spec.revision.id, spec_approval_id=spec.approval["id"]
    )
    assert architect.previous == [None]

    recorded = control_plane.revise_architecture(
        project_id=project["id"],
        spec_revision_id=spec.revision.id,
        spec_approval_id=spec.approval["id"],
        expected_revision=1,
        document=first.architecture.to_dict(),
    )
    control_plane.decide_approval(recorded.approval["id"], approved=True, actor="founder")
    designs = control_plane.approved_designs(project["id"])
    assert [d["architecture_revision_id"] for d in designs] == [recorded.revision.id]
    assert designs[0]["spec_revision_id"] == spec.revision.id

    control_plane.draft_architecture(
        project_id=project["id"], spec_revision_id=spec.revision.id, spec_approval_id=spec.approval["id"]
    )
    previous = architect.previous[1]
    assert previous is not None
    assert previous.architecture.to_dict() == first.architecture.to_dict()
    assert previous.project.to_dict() == spec.spec.to_dict()
    assert control_plane.project_state(project["id"])["approved_designs"] == designs


class RecordingPusher:
    """Stands in for git: records what it was asked to push and answers like a push."""

    def __init__(self):
        self.calls = []

    def __call__(self, snapshot, *, run_id, snapshot_digest, target, committed_at):
        from richbuild.repository import RepositoryPush

        self.calls.append(
            {
                "snapshot": snapshot,
                "run_id": run_id,
                "snapshot_digest": snapshot_digest,
                "target": target,
                "committed_at": committed_at,
            }
        )
        return RepositoryPush(
            run_id=run_id,
            remote=target.remote,
            branch=target.branch,
            commit_sha="f" * 40,
            snapshot_digest=snapshot_digest,
            file_count=3,
            committed_at=committed_at.isoformat(),
            repository_url=target.repository_url,
            created_repository=target.create,
            already_current=False,
        )


def test_repository_push_sends_the_stored_snapshot_and_records_a_receipt(tmp_path):
    orchestrator = RecordingPreviewOrchestrator()
    control_plane, prepared, _destination = _scaffolded_preview_run(
        tmp_path, orchestrator
    )
    pusher = RecordingPusher()
    control_plane._repository_pusher = pusher
    run_id = prepared.run["id"]
    control_plane.store.append_event(run_id, "run.execution_finished", {"status": "succeeded"})

    push = control_plane.push_repository(
        run_id=run_id, remote="https://github.com/maya/tracker.git", create=True
    )

    (call,) = pusher.calls
    release = [
        record
        for record in control_plane.store.list_run_artifacts(run_id)
        if record["role"] == "source:release-snapshot"
    ][-1]
    stored = control_plane.store.get_artifact(release["digest"])
    assert call["snapshot"] == stored.path.read_bytes()
    assert call["snapshot_digest"] == stored.digest
    assert call["target"].token_handle == "github.token"
    assert call["target"].create is True and call["target"].private is True
    finished = [
        event
        for event in control_plane.store.list_events(run_id)
        if event["event_type"] == "run.execution_finished"
    ][-1]
    assert call["committed_at"].isoformat() == finished["created_at"]
    assert push["commit_sha"] == "f" * 40
    assert push["repository_url"] == "https://github.com/maya/tracker"
    receipts = [
        record
        for record in control_plane.store.list_run_artifacts(run_id)
        if record["role"] == "repository-push"
    ]
    assert len(receipts) == 1
    assert control_plane.list_repository_pushes(run_id) == [
        {**push, "receipt_digest": receipts[0]["digest"]}
    ]


def test_repository_push_needs_a_succeeded_run_and_an_acceptable_target(tmp_path):
    orchestrator = RecordingPreviewOrchestrator()
    control_plane, prepared, _destination = _scaffolded_preview_run(
        tmp_path, orchestrator
    )
    pusher = RecordingPusher()
    control_plane._repository_pusher = pusher
    run_id = prepared.run["id"]
    with pytest.raises(ValueError, match="https:// or file://"):
        control_plane.push_repository(run_id=run_id, remote="git@github.com:maya/x.git")
    with pytest.raises(ValueError, match="<owner>/<repository>"):
        control_plane.push_repository(run_id=run_id, remote="https://github.com/maya")
    assert pusher.calls == []
    unfinished = control_plane.prepare_run(
        architecture_approval_id=control_plane.store.list_approvals(
            prepared.run["project_id"]
        )[-1]["id"],
        budget=_budget(),
    )
    with pytest.raises(ValueError, match="succeeded run"):
        control_plane.push_repository(
            run_id=unfinished.run["id"], remote="https://github.com/maya/x.git"
        )



def test_a_preview_carries_the_migration_set_the_run_verified_and_refuses_another(
    tmp_path,
):
    from datetime import datetime, timedelta, timezone

    from richbuild.preview import migration_digests

    orchestrator = RecordingPreviewOrchestrator()
    control_plane, prepared, destination = _scaffolded_preview_run(
        tmp_path, orchestrator
    )
    migrations = destination / "packages/db/migrations"
    assert migrations.is_dir(), "the fixture's architecture has a data component"

    submission = control_plane.request_preview(
        run_id=prepared.run["id"],
        source_dir=destination,
        neon_project_id="neon-project",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    recorded = [entry.as_dict() for entry in migration_digests(destination)]
    assert submission.preview["request"]["migration_digests"] == recorded
    assert submission.preview["request"]["gate_engine"].startswith("PostgreSQL 18.3")
    control_plane.decide_approval(
        submission.approval["id"], approved=True, actor="founder"
    )
    deployed = control_plane.deploy_preview(
        preview_id=submission.preview["id"], approval_id=submission.approval["id"]
    )
    assert deployed.preview["status"] == "ready"
    request = orchestrator.created[-1]
    assert [entry.as_dict() for entry in request.migration_digests] == recorded
    assert request.gate_engine.startswith("PostgreSQL 18.3")

    # A source whose migrations are not the verified set is refused before
    # anyone is asked to approve. The release snapshot still matches -- the
    # migration is part of it -- so this is a second, independent hold.
    other = RecordingPreviewOrchestrator()
    control_plane, prepared, destination = _scaffolded_preview_run(
        tmp_path / "other", other
    )
    (destination / "packages/db/migrations/0001_more.sql").write_text(
        'CREATE TABLE "more" (id uuid PRIMARY KEY);\n'
    )
    control_plane.store.attach_artifact(
        prepared.run["id"],
        control_plane.store.put_artifact(
            create_deployment_snapshot(destination),
            media_type="application/vnd.rich.release-source+zip",
        ).digest,
        role="source:release-snapshot",
    )
    with pytest.raises(ValueError, match="not the set the run verified"):
        control_plane.request_preview(
            run_id=prepared.run["id"],
            source_dir=destination,
            neon_project_id="neon-project",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    assert other.created == []
