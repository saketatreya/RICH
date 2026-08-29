import base64
from datetime import datetime, timedelta, timezone
import json
import threading
import time

import pytest

from richbuild.api import Application
from richbuild.preview import PreviewResult, create_deployment_snapshot
from richbuild.scheduler import SchedulerReport
from richbuild.store import RichStore


def _headers(key):
    return {"Idempotency-Key": key}


class FakePreviewOrchestrator:
    def create(self, request):
        return PreviewResult(
            run_id=request.run_id,
            provider="vercel",
            deployment_id="dpl_api",
            preview_url="https://api-preview.vercel.app",
            database_provider="neon",
            database_project_id=request.neon_project_id,
            database_branch_id="br_api",
            database_branch_name=request.neon_branch_name,
            expires_at=request.expires_at.isoformat(),
        )

    def destroy(self, _request, _result):
        return None


class FakeRunExecutor:
    def __init__(self):
        self.called = threading.Event()
        self.requests = []

    def execute(self, **request):
        self.requests.append(request)
        self.called.set()
        return SchedulerReport(
            run_id=request["run_id"],
            status="succeeded",
            task_statuses=(("app", "succeeded"),),
            task_attempts=(("app", 1),),
        )


def test_mutations_require_idempotency_key(tmp_path):
    application = Application(RichStore(tmp_path))

    response = application.handle(
        "POST",
        "/v1/projects",
        body={"project_id": "project.demo", "name": "Demo"},
    )

    assert response.status == 428
    assert response.body["error"] == "IdempotencyKeyRequired"


def test_completed_mutation_replays_and_key_cannot_change_request(tmp_path):
    application = Application(RichStore(tmp_path))
    request = {"project_id": "project.demo", "name": "Demo"}

    first = application.handle(
        "POST",
        "/v1/projects",
        body=request,
        headers=_headers("create-demo"),
    )
    replay = application.handle(
        "POST",
        "/v1/projects",
        body=request,
        headers=_headers("create-demo"),
    )
    conflict = application.handle(
        "POST",
        "/v1/projects",
        body={"project_id": "project.other", "name": "Other"},
        headers=_headers("create-demo"),
    )

    assert first.status == replay.status == 201
    assert first.body == replay.body
    assert conflict.status == 409
    assert conflict.body["error"] == "RevisionConflict"


def test_health_and_project_reads_are_nonmutating(tmp_path):
    application = Application(RichStore(tmp_path))
    application.handle(
        "POST",
        "/v1/projects",
        body={"project_id": "project.demo", "name": "Demo"},
        headers=_headers("create-demo"),
    )

    health = application.handle("GET", "/v1/health")
    project = application.handle("GET", "/v1/projects/project.demo")

    assert health.body["status"] == "ok"
    assert project.body["project"]["name"] == "Demo"


def test_run_execution_is_started_in_background_and_reports_durable_events(
    tmp_path,
):
    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.demo")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="ready",
    )
    workspaces = tmp_path / "workspaces"
    workspace = workspaces / "demo"
    workspace.mkdir(parents=True)
    executor = FakeRunExecutor()
    application = Application(
        store,
        workspace_root=workspaces,
        run_executor=executor,
    )

    response = application.handle(
        "POST",
        f"/v1/runs/{run['id']}/executions",
        body={"workspace": "demo"},
        headers=_headers("execute-demo"),
    )

    assert response.status == 202
    assert response.body["execution"]["status"] == "accepted"
    assert executor.called.wait(2)
    assert executor.requests[0]["workspace"] == workspace.resolve()
    deadline = time.monotonic() + 2
    event_types = set()
    while time.monotonic() < deadline:
        event_types = {
            event["event_type"] for event in store.list_events(run["id"])
        }
        if "run.execution_finished" in event_types:
            break
        time.sleep(0.01)
    assert "run.execution_requested" in event_types
    assert "run.execution_finished" in event_types


def test_local_api_rejects_dns_rebinding_and_cross_origin_mutations(tmp_path):
    application = Application(RichStore(tmp_path))

    hostile_host = application.handle(
        "GET",
        "/v1/health",
        headers={"Host": "attacker.example"},
    )
    hostile_origin = application.handle(
        "POST",
        "/v1/projects",
        body={"project_id": "project.demo", "name": "Demo"},
        headers={
            "Host": "127.0.0.1:8767",
            "Origin": "https://attacker.example",
            "Idempotency-Key": "hostile-origin",
        },
    )

    assert hostile_host.status == 403
    assert hostile_host.body["error"] == "UntrustedHost"
    assert hostile_origin.status == 403
    assert hostile_origin.body["error"] == "UntrustedOrigin"


def test_api_scaffold_destinations_stay_inside_configured_workspace(tmp_path):
    application = Application(
        RichStore(tmp_path / "state"),
        workspace_root=tmp_path / "workspaces",
    )

    assert application._workspace_destination("demo") == (
        tmp_path / "workspaces" / "demo"
    )
    with pytest.raises(ValueError, match="workspace root"):
        application._workspace_destination(str(tmp_path / "outside"))
    with pytest.raises(ValueError, match="workspace root"):
        application._workspace_destination("../escape")


def test_unexpected_api_errors_are_not_disclosed(tmp_path, monkeypatch):
    application = Application(RichStore(tmp_path))

    def crash(**_kwargs):
        raise RuntimeError("sensitive internal path /private/secret")

    monkeypatch.setattr(application.control_plane, "create_project", crash)
    response = application.handle(
        "POST",
        "/v1/projects",
        body={"project_id": "project.demo", "name": "Demo"},
        headers=_headers("internal-error"),
    )

    assert response.status == 500
    assert response.body == {
        "error": "InternalServerError",
        "message": "an unexpected internal error occurred",
    }


def test_preview_http_flow_requires_approval_and_exposes_safe_result(tmp_path):
    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.demo")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="ready",
    )
    source = tmp_path / "generated"
    source.mkdir()
    (source / "package.json").write_text('{"name":"demo"}')
    store.append_event(
        run["id"],
        "scaffold.completed",
        {"destination": str(source.resolve())},
    )
    store.set_run_status(run["id"], "running", expected_status="ready")
    store.set_run_status(run["id"], "verifying", expected_status="running")
    store.set_run_status(
        run["id"], "succeeded", expected_status="verifying"
    )
    release = store.put_artifact(
        create_deployment_snapshot(source),
        media_type="application/vnd.rich.release-source+zip",
    )
    store.attach_artifact(
        run["id"], release.digest, role="source:release-snapshot"
    )
    application = Application(
        store, preview_orchestrator=FakePreviewOrchestrator()
    )

    requested = application.handle(
        "POST",
        f"/v1/runs/{run['id']}/preview-requests",
        body={
            "source_dir": str(source),
            "neon_project_id": "neon-project-1",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(days=1)
            ).isoformat(),
        },
        headers=_headers("preview-request"),
    )
    preview = requested.body["preview"]
    approval = requested.body["approval"]
    blocked = application.handle(
        "POST",
        f"/v1/previews/{preview['id']}/deployments",
        body={"approval_id": approval["id"]},
        headers=_headers("preview-deploy-before-approval"),
    )
    decided = application.handle(
        "POST",
        f"/v1/approvals/{approval['id']}/decisions",
        body={"approved": True, "actor": "founder"},
        headers=_headers("preview-approve"),
    )
    deployed = application.handle(
        "POST",
        f"/v1/previews/{preview['id']}/deployments",
        body={"approval_id": approval["id"]},
        headers=_headers("preview-deploy"),
    )

    assert requested.status == 201
    assert blocked.status == 403
    assert decided.status == 200
    assert deployed.status == 201
    assert deployed.body["result"]["preview_url"] == (
        "https://api-preview.vercel.app"
    )
    assert "connection_uri" not in repr(deployed.body)
    assert application.handle(
        "GET", f"/v1/runs/{run['id']}/previews"
    ).body["previews"][0]["status"] == "ready"


def test_preview_api_rejects_client_selected_secret_handles(tmp_path):
    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.demo")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="succeeded",
    )
    source = tmp_path / "generated"
    source.mkdir()
    (source / "package.json").write_text('{"name":"demo"}')
    store.append_event(
        run["id"],
        "scaffold.completed",
        {"destination": str(source.resolve())},
    )
    release = store.put_artifact(
        create_deployment_snapshot(source),
        media_type="application/vnd.rich.release-source+zip",
    )
    store.attach_artifact(
        run["id"], release.digest, role="source:release-snapshot"
    )
    application = Application(
        store, preview_orchestrator=FakePreviewOrchestrator()
    )

    response = application.handle(
        "POST",
        f"/v1/runs/{run['id']}/preview-requests",
        body={
            "source_dir": str(source),
            "neon_project_id": "neon-project-1",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(days=1)
            ).isoformat(),
            "neon_token_handle": "env:HOME",
        },
        headers=_headers("preview-bad-secret-handle"),
    )

    assert response.status == 400
    assert response.body["error"] == "ValueError"


# --------------------------------------------------------------------------
# Reading what the machine made. Without these the human can approve or veto
# a build without ever seeing a line of what it produced.
# --------------------------------------------------------------------------


def _run_with_artifact(tmp_path, payload, *, role="generated-source", media_type=None):
    store = RichStore(tmp_path)
    store.create_project("Inspect", project_id="project.inspect")
    run = store.create_run(
        "project.inspect",
        spec_revision_id=None,
        architecture_revision_id=None,
        run_id="run.inspect",
        status="ready",
        budget={
            "max_model_attempts": 1,
            "max_input_tokens": 1,
            "max_output_tokens": 1,
            "max_cost_usd": "1",
            "max_execution_seconds": 1,
        },
    )
    task = store.create_task(
        run["id"],
        node_id="domain",
        kind="implement",
        task_id="run.inspect:implement:domain",
        status="ready",
    )
    artifact = store.put_artifact(
        payload,
        media_type=media_type or "application/json",
        metadata={"node_id": "domain"},
    )
    store.attach_artifact(
        run["id"], artifact.digest, role=role, task_id=task["id"]
    )
    return Application(store), run["id"], artifact.digest


def test_a_generated_source_artifact_can_be_read_back_by_digest(tmp_path):
    bundle = {
        "schema_version": "rich.generated-source/v1",
        "summary": "Implemented the domain operations",
        "files": [
            {
                "operation": "create",
                "path": "packages/domain/src/operations.ts",
                "size": 31,
                "sha256": "a" * 64,
                "content": "export const operations = {};\n",
            }
        ],
    }
    application, run_id, digest = _run_with_artifact(
        tmp_path, json.dumps(bundle).encode()
    )

    response = application.handle(
        "GET", f"/v1/runs/{run_id}/artifacts/{digest}"
    )

    assert response.status == 200
    assert response.body["content"] == bundle
    assert response.body["artifact"]["digest"] == digest
    assert response.body["artifact"]["metadata"]["node_id"] == "domain"
    assert response.body["artifact"]["attachments"] == [
        {"role": "generated-source", "task_id": "run.inspect:implement:domain"}
    ]


def test_a_non_json_artifact_comes_back_as_bytes_not_a_decode_error(tmp_path):
    application, run_id, digest = _run_with_artifact(
        tmp_path, b"\xff\xfe not utf-8 at all", media_type="text/plain"
    )

    response = application.handle(
        "GET", f"/v1/runs/{run_id}/artifacts/{digest}"
    )

    assert response.status == 200
    assert "content" not in response.body
    assert base64.b64decode(response.body["content_base64"]) == b"\xff\xfe not utf-8 at all"


def test_an_artifact_is_readable_only_through_a_run_that_produced_it(tmp_path):
    application, run_id, digest = _run_with_artifact(tmp_path, b'{"ok": true}')
    other = application.store.create_run(
        "project.inspect",
        spec_revision_id=None,
        architecture_revision_id=None,
        run_id="run.other",
        status="ready",
        budget={
            "max_model_attempts": 1,
            "max_input_tokens": 1,
            "max_output_tokens": 1,
            "max_cost_usd": "1",
            "max_execution_seconds": 1,
        },
    )

    response = application.handle(
        "GET", f"/v1/runs/{other['id']}/artifacts/{digest}"
    )

    # The digest alone would be capability enough, but scoping to a run keeps
    # this from becoming an open read oracle over the whole content store.
    assert response.status == 404
    assert application.handle(
        "GET", f"/v1/runs/{run_id}/artifacts/{digest}"
    ).status == 200


def test_an_unknown_digest_is_not_found(tmp_path):
    application, run_id, _ = _run_with_artifact(tmp_path, b'{"ok": true}')

    response = application.handle(
        "GET", f"/v1/runs/{run_id}/artifacts/{'b' * 64}"
    )

    assert response.status == 404


def test_source_transactions_are_listable_so_a_diff_can_be_reconstructed(tmp_path):
    store = RichStore(tmp_path)
    store.create_project("Inspect", project_id="project.inspect")
    run = store.create_run(
        "project.inspect",
        spec_revision_id=None,
        architecture_revision_id=None,
        run_id="run.inspect",
        status="ready",
        budget={
            "max_model_attempts": 1,
            "max_input_tokens": 1,
            "max_output_tokens": 1,
            "max_cost_usd": "1",
            "max_execution_seconds": 1,
        },
    )
    application = Application(store)

    response = application.handle(
        "GET", f"/v1/runs/{run['id']}/source-transactions"
    )

    # The journal records each file's *original* bytes alongside the intended
    # new digest, which is the only thing that makes a real before/after view
    # possible rather than a "here is the new file" view.
    assert response.status == 200
    assert response.body["source_transactions"] == []


def test_a_journal_is_readable_through_the_run_that_wrote_it(tmp_path):
    """The before-bytes live on the transaction, not on the run.

    A write-ahead journal records each file's content as it was *before* the
    write, and it is referenced by digest on the source transaction rather than
    attached to the run. Scoping reads to attachments alone would authorize the
    new file and hide the old one, which is the half that makes a diff a diff.
    """

    store = RichStore(tmp_path)
    store.create_project("Journal", project_id="project.journal")
    run = store.create_run(
        "project.journal",
        spec_revision_id=None,
        architecture_revision_id=None,
        run_id="run.journal",
        status="ready",
        budget={
            "max_model_attempts": 1,
            "max_input_tokens": 1,
            "max_output_tokens": 1,
            "max_cost_usd": "1",
            "max_execution_seconds": 1,
        },
    )
    task = store.create_task(
        run["id"],
        node_id="domain",
        kind="implement",
        task_id="run.journal:implement:domain",
        status="ready",
    )
    lease = store.claim_run_execution(run["id"])
    store.set_task_status(
        task["id"], "running", expected_status="ready", increment_attempt=True
    )
    journal = {
        "schema_version": "rich.source-transaction/v1",
        "files": [
            {
                "path": "packages/domain/src/index.ts",
                "operation": "replace",
                "original": {
                    "existed": True,
                    "content_base64": base64.b64encode(b"export const a = 1;\n").decode(),
                },
            }
        ],
    }
    journal_artifact = store.put_artifact(
        json.dumps(journal).encode(), media_type="application/json"
    )
    generated_artifact = store.put_artifact(
        json.dumps({"files": []}).encode(), media_type="application/json"
    )
    store.prepare_source_transaction(
        run["id"],
        task_id=task["id"],
        attempt=1,
        owner_token=lease.owner_token,
        journal_digest=journal_artifact.digest,
        generated_digest=generated_artifact.digest,
    )
    application = Application(store)

    listed = application.handle(
        "GET", f"/v1/runs/{run['id']}/source-transactions"
    )
    fetched = application.handle(
        "GET", f"/v1/runs/{run['id']}/artifacts/{journal_artifact.digest}"
    )

    assert listed.status == 200
    assert listed.body["source_transactions"][0]["journal_digest"] == (
        journal_artifact.digest
    )
    # Never attached to the run, and still readable through it.
    assert journal_artifact.digest not in {
        attachment["digest"]
        for attachment in store.list_run_artifacts(run["id"])
    }
    assert fetched.status == 200
    original = fetched.body["content"]["files"][0]["original"]
    assert base64.b64decode(original["content_base64"]) == b"export const a = 1;\n"


# --------------------------------------------------------------------------
# Editing the machine's proposal. Without this, rejecting one is a dead end:
# the only route to a different architecture is to change the spec until the
# planner happens to emit one, which is a veto rather than a review.
# --------------------------------------------------------------------------


def _approved_spec(application):
    application.handle(
        "POST",
        "/v1/projects",
        body={"project_id": "project.edit", "name": "Edit"},
        headers=_headers("k-project"),
    )
    submission = application.handle(
        "POST",
        "/v1/projects/project.edit/spec-submissions",
        body={
            "project_name": "Edit",
            "expected_revision": 0,
            "answers": {
                "goal": "Publish an accessible launch checklist.",
                "audiences": ["technical founders"],
                "capabilities": [
                    {
                        "id": "req.checklist",
                        "title": "Launch checklist",
                        "statement": "A founder can review the approved checklist.",
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
                            {"action": "navigate", "value": "/"},
                            {
                                "action": "assert_visible",
                                "locator": {"kind": "text", "value": "Checklist"},
                            },
                        ],
                    },
                    {
                        "id": "scenario.a11y",
                        "title": "Keyboard",
                        "when": ["A founder uses a keyboard."],
                        "then": ["The checklist responds."],
                        "requirement_ids": ["req.a11y"],
                        "oracle": [
                            {"action": "navigate", "value": "/"},
                            {
                                "action": "assert_visible",
                                "locator": {"kind": "role", "value": "link"},
                            },
                        ],
                    },
                ],
            },
        },
        headers=_headers("k-spec"),
    ).body
    application.handle(
        "POST",
        f"/v1/approvals/{submission['approval']['id']}/decisions",
        body={"approved": True, "actor": "founder", "reason": "looks right"},
        headers=_headers("k-decide"),
    )
    return submission


def test_a_human_edited_architecture_becomes_its_own_revision(tmp_path):
    application = Application(RichStore(tmp_path))
    spec = _approved_spec(application)
    proposed = application.handle(
        "POST",
        "/v1/projects/project.edit/architecture-submissions",
        body={
            "spec_revision_id": spec["revision"]["id"],
            "spec_approval_id": spec["approval"]["id"],
            "expected_revision": 1,
        },
        headers=_headers("k-arch"),
    ).body

    edited = json.loads(json.dumps(proposed["architecture"]))
    for node in edited["nodes"]:
        if node["id"] == "web":
            node["name"] = "Renamed by a human"

    response = application.handle(
        "POST",
        "/v1/projects/project.edit/architecture-revisions",
        body={
            "spec_revision_id": spec["revision"]["id"],
            "spec_approval_id": spec["approval"]["id"],
            "expected_revision": 2,
            "architecture": edited,
            "decisions": ["Renamed the web node."],
        },
        headers=_headers("k-revise"),
    )

    assert response.status == 201
    names = {
        node["id"]: node["name"] for node in response.body["architecture"]["nodes"]
    }
    assert names["web"] == "Renamed by a human"
    # A new immutable revision needing its own approval; the earlier one is
    # untouched, because approving one revision never authorizes another.
    assert response.body["revision"]["id"] != proposed["revision"]["id"]
    assert response.body["approval"]["status"] == "requested"
    assert response.body["decisions"] == ["Renamed the web node."]


def test_an_edited_architecture_faces_exactly_the_same_validators(tmp_path):
    application = Application(RichStore(tmp_path))
    spec = _approved_spec(application)
    proposed = application.handle(
        "POST",
        "/v1/projects/project.edit/architecture-submissions",
        body={
            "spec_revision_id": spec["revision"]["id"],
            "spec_approval_id": spec["approval"]["id"],
            "expected_revision": 1,
        },
        headers=_headers("k-arch"),
    ).body

    broken = json.loads(json.dumps(proposed["architecture"]))
    # A plausible human edit: delete the behaviors that serve one requirement,
    # because you decided this layer should not handle it. The graph stays
    # structurally sound; it just stops delivering the whole product.
    for node in broken["nodes"]:
        node["requirement_ids"] = [
            value for value in node["requirement_ids"] if value != "req.a11y"
        ]
    for contract in broken["contracts"]:
        for collection in ("operations", "invariants", "obligations"):
            contract[collection] = [
                behavior
                for behavior in contract.get(collection, [])
                if behavior["requirement_ids"] != ["req.a11y"]
            ]

    response = application.handle(
        "POST",
        "/v1/projects/project.edit/architecture-revisions",
        body={
            "spec_revision_id": spec["revision"]["id"],
            "spec_approval_id": spec["approval"]["id"],
            "expected_revision": 2,
            "architecture": broken,
        },
        headers=_headers("k-broken"),
    )

    # Letting a human author the document changes who proposes, not what is
    # checked -- and the rejection names the fixable thing, because that
    # message is the human's feedback too.
    assert response.status == 400
    assert "req.a11y" in response.body["message"]


def test_editing_an_architecture_still_needs_the_spec_approval(tmp_path):
    application = Application(RichStore(tmp_path))
    spec = _approved_spec(application)
    proposed = application.handle(
        "POST",
        "/v1/projects/project.edit/architecture-submissions",
        body={
            "spec_revision_id": spec["revision"]["id"],
            "spec_approval_id": spec["approval"]["id"],
            "expected_revision": 1,
        },
        headers=_headers("k-arch"),
    ).body

    response = application.handle(
        "POST",
        "/v1/projects/project.edit/architecture-revisions",
        body={
            "spec_revision_id": spec["revision"]["id"],
            # The architecture approval is not a substitute for the spec one.
            "spec_approval_id": proposed["approval"]["id"],
            "expected_revision": 2,
            "architecture": proposed["architecture"],
        },
        headers=_headers("k-wrong-gate"),
    )

    assert response.status == 403


class RecordingArchitect:
    """Stand in for a model, so the draft/apply loop is testable offline."""

    def __init__(self, transform=None):
        self.calls = []
        self.transform = transform

    def propose(self, spec, *, target_pack, repair=None, previous=None):
        self.previous = previous
        from richbuild.planner import plan_nextjs_architecture

        from dataclasses import replace

        self.calls.append({"spec": spec.id, "target_pack": target_pack, "repair": repair})
        # Stands in for a model, so it reports itself as one; the baseline's own
        # source is what a fallback looks like and must stay distinguishable.
        proposal = replace(plan_nextjs_architecture(spec), source="model")
        if self.transform is None:
            return proposal
        return self.transform(proposal)


def test_a_draft_records_nothing_until_a_human_applies_it(tmp_path):
    store = RichStore(tmp_path)
    architect = RecordingArchitect()
    application = Application(store, architect=architect)
    spec = _approved_spec(application)
    before = store.get_project("project.edit")["current_revision"]

    drafted = application.handle(
        "POST",
        "/v1/projects/project.edit/architecture-drafts",
        body={
            "spec_revision_id": spec["revision"]["id"],
            "spec_approval_id": spec["approval"]["id"],
        },
        headers=_headers("k-draft"),
    )

    # 200, not 201, and the durable store is untouched: no revision, no
    # approval, no bump to the counter. A proposal the control plane stored on
    # the model's say-so would be a decision, not a suggestion.
    assert drafted.status == 200
    assert drafted.body["source"] == "model"
    assert store.get_project("project.edit")["current_revision"] == before
    assert store.list_revisions("project.edit", kind="architecture") == []
    assert store.list_approvals("project.edit", status="requested") == []


def test_a_correction_reaches_the_architect_verbatim(tmp_path):
    architect = RecordingArchitect()
    application = Application(RichStore(tmp_path), architect=architect)
    spec = _approved_spec(application)

    application.handle(
        "POST",
        "/v1/projects/project.edit/architecture-drafts",
        body={
            "spec_revision_id": spec["revision"]["id"],
            "spec_approval_id": spec["approval"]["id"],
            "repair": "the checklist logic belongs in its own component",
        },
        headers=_headers("k-repair"),
    )

    # A human's correction and the validator's rejection travel one channel.
    assert architect.calls[0]["repair"] == (
        "the checklist logic belongs in its own component"
    )
    assert architect.calls[0]["target_pack"] == "nextjs-app-router"


def test_without_an_architect_a_draft_falls_back_and_says_so(tmp_path):
    application = Application(RichStore(tmp_path))
    spec = _approved_spec(application)

    drafted = application.handle(
        "POST",
        "/v1/projects/project.edit/architecture-drafts",
        body={
            "spec_revision_id": spec["revision"]["id"],
            "spec_approval_id": spec["approval"]["id"],
        },
        headers=_headers("k-fallback"),
    )

    # A template and a model are not interchangeable, and a reviewer should not
    # have to guess which one answered.
    assert drafted.status == 200
    assert drafted.body["source"] == "planner"


def test_a_draft_that_does_not_validate_is_refused_before_a_human_sees_it(tmp_path):
    def strip_a_requirement(proposal):
        document = json.loads(json.dumps(proposal.architecture.to_dict()))
        for node in document["nodes"]:
            node["requirement_ids"] = [
                value for value in node["requirement_ids"] if value != "req.a11y"
            ]
        for contract in document["contracts"]:
            for collection in ("operations", "invariants", "obligations"):
                contract[collection] = [
                    behavior
                    for behavior in contract.get(collection, [])
                    if behavior["requirement_ids"] != ["req.a11y"]
                ]
        from dataclasses import replace

        from richbuild.models import ArchitectureSpec

        return replace(
            proposal, architecture=ArchitectureSpec.from_dict(document)
        )

    application = Application(
        RichStore(tmp_path), architect=RecordingArchitect(strip_a_requirement)
    )
    spec = _approved_spec(application)

    drafted = application.handle(
        "POST",
        "/v1/projects/project.edit/architecture-drafts",
        body={
            "spec_revision_id": spec["revision"]["id"],
            "spec_approval_id": spec["approval"]["id"],
        },
        headers=_headers("k-invalid-draft"),
    )

    # Drafting skips the revision, never the validators. Showing a human a
    # proposal that could not be applied would waste their review.
    assert drafted.status == 400
    assert "req.a11y" in drafted.body["message"]


def test_execution_status_reports_a_run_leased_by_another_process(tmp_path):
    """The Canvas and `rich serve` are different processes over one state
    directory; a run either of them started must not look idle to the other."""

    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.leased")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="ready",
    )
    application = Application(store, workspace_root=tmp_path / "workspaces")

    before = application.handle(
        "GET", f"/v1/runs/{run['id']}/execution", headers={}
    )
    lease = store.claim_run_execution(run["id"], lease_seconds=60)
    during = application.handle(
        "GET", f"/v1/runs/{run['id']}/execution", headers={}
    )
    store.release_run_execution(run["id"], owner_token=lease.owner_token)
    after = application.handle(
        "GET", f"/v1/runs/{run['id']}/execution", headers={}
    )

    assert before.body["execution"]["active"] is False
    assert during.body["execution"]["active"] is True
    assert during.body["execution"]["owned_here"] is False, (
        "this process holds no lease, so it must not claim to own the run"
    )
    assert after.body["execution"]["active"] is False
    assert lease.owner_token not in json.dumps(during.body), (
        "the owner token is the authority to write; a status read must not leak it"
    )


def test_node_rebuild_route_forgets_one_node(tmp_path):
    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.api.rebuild")
    store.put_generation_memo(
        "d" * 64,
        payload=b'{"schema":"rich.generation-memo/v1","bundle":{"summary":"s","files":[]}}',
        project_id=project["id"],
        node_id="web",
        provider="anthropic",
        model="claude-sonnet-5",
        run_id="run.1",
        task_id="run.1:implement:web",
    )
    application = Application(store, workspace_root=tmp_path / "workspaces")

    response = application.handle(
        "POST",
        f"/v1/projects/{project['id']}/node-rebuilds",
        body={"node_id": "web"},
        headers=_headers("rebuild-web"),
    )
    missing_key = application.handle(
        "POST",
        f"/v1/projects/{project['id']}/node-rebuilds",
        body={"node_id": "web"},
        headers={},
    )

    assert response.status == 200, "nothing is created; permission is withdrawn"
    assert response.body["rebuild"]["forgotten_generations"] == 1
    assert store.get_generation_memo("d" * 64) is None
    assert missing_key.status == 428, "it is a mutation like any other"


def test_a_run_can_be_cancelled_durably_from_any_surface(tmp_path):
    """The process that started a run is often not the one being asked to stop
    it, so the request has to outlive whichever server hears it."""

    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.cancel")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="running",
    )
    application = Application(store, workspace_root=tmp_path / "workspaces")

    assert store.run_cancellation(run["id"]) is None
    response = application.handle(
        "POST",
        f"/v1/runs/{run['id']}/cancellation",
        body={"reason": "operator changed their mind"},
        headers=_headers("cancel-once"),
    )

    assert response.status == 202, "cooperative: it is asked, not killed"
    standing = store.run_cancellation(run["id"])
    assert standing["reason"] == "operator changed their mind"
    assert "run.cancellation_requested" in {
        event["event_type"] for event in store.list_events(run["id"])
    }

    # A second request never overwrites the first reason.
    application.handle(
        "POST",
        f"/v1/runs/{run['id']}/cancellation",
        body={"reason": "something else"},
        headers=_headers("cancel-twice"),
    )
    assert store.run_cancellation(run["id"])["reason"] == (
        "operator changed their mind"
    )


def test_the_interview_reports_what_it_still_needs_and_adapts_to_the_answers(
    tmp_path,
):
    """AdaptiveInterview was always adaptive and nothing called it before
    compile(), so the interview behaved like a fixed form."""

    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.interview")
    application = Application(store, workspace_root=tmp_path / "workspaces")

    def ask(answers, key="ask"):
        return application.handle(
            "POST",
            f"/v1/projects/{project['id']}/interview-questions",
            body={
                "project_name": "Demo",
                "answers": answers,
                "limit": 10,
            },
            headers=_headers(key),
        )

    empty = ask({})
    identity = ask(
        {
            "goal": "A todo app where each user signs in to see their own tasks",
            "audiences": ["operators"],
            "capabilities": [{"id": "req.a", "title": "T", "statement": "S."}],
        },
        key="ask-identity",
    )
    anonymous = ask(
        {
            "goal": "A static page listing published recipes for anyone to read",
            "audiences": ["visitors"],
            "capabilities": [{"id": "req.a", "title": "T", "statement": "S."}],
        },
        key="ask-anon",
    )

    assert empty.status == 200, "nothing is stored; it reads and reports"
    assert empty.body["complete"] is False
    assert "goal" in {item["id"] for item in empty.body["questions"]}

    identity_ids = {item["id"] for item in identity.body["questions"]}
    anonymous_ids = {item["id"] for item in anonymous.body["questions"]}
    assert "roles" in identity_ids, "signing in implies who may do what"
    assert "roles" not in anonymous_ids, "a page with no sign-in is not asked"
    for question in empty.body["questions"]:
        assert question["rationale"], "a question that cannot say why it asks"


def test_projects_can_be_listed_most_recently_touched_first(tmp_path):
    """The control plane offered to "create or select a project" and could only
    do the first: a second project was reachable only by remembering its id."""

    store = RichStore(tmp_path / "state")
    application = Application(store, workspace_root=tmp_path / "workspaces")

    empty = application.handle("GET", "/v1/projects", headers={})
    store.create_project("First", project_id="project.first")
    store.create_project("Second", project_id="project.second")
    store.save_revision(
        "project.first",
        kind="product_spec",
        schema_version="2.0",
        document={"schema_version": "2.0"},
        expected_revision=0,
    )
    listed = application.handle("GET", "/v1/projects", headers={})

    assert empty.status == 200
    assert empty.body["projects"] == [], "an empty state is a fine first run"
    ids = [item["id"] for item in listed.body["projects"]]
    assert set(ids) == {"project.first", "project.second"}
    assert ids[0] == "project.first", "the one just touched comes first"
    assert listed.body["projects"][0]["current_revision"] == 1


def test_change_plans_read_and_change_applications_decide(tmp_path):
    """Two routes, one distinction: a plan is for looking at, an application
    withdraws permission to replay."""

    store = RichStore(tmp_path / "state")
    application = Application(store, workspace_root=tmp_path / "workspaces")
    project = store.create_project("Demo", project_id="project.change.api")
    spec_document = {
        "schema_version": "2.0",
        "id": project["id"],
        "name": "Demo",
        "goal": "Ship a reviewed checklist.",
        "audiences": ["founders"],
        "requirements": [
            {
                "id": "req.one",
                "title": "One",
                "statement": "A founder reviews the checklist.",
                "kind": "functional",
                "priority": "must",
                "source": "user",
            }
        ],
        "acceptance_scenarios": [
            {
                "id": "scenario.one",
                "title": "It holds",
                "given": ["The application is available."],
                "when": ["A founder opens it."],
                "then": ["The checklist is visible."],
                "requirement_ids": ["req.one"],
                "oracle": [
                    {"action": "open_requirement"},
                    {
                        "action": "assert_visible",
                        "locator": {"kind": "text", "value": "A founder reviews the checklist."},
                    },
                ],
            }
        ],
        "constraints": [],
        "revision": 1,
    }
    from richbuild.models import ProjectSpec
    from richbuild.planner import plan_nextjs_architecture

    spec = ProjectSpec.from_dict(spec_document)
    architecture = plan_nextjs_architecture(spec).architecture
    spec_revision = store.save_revision(
        project["id"], kind="product_spec", schema_version="2.0",
        document=spec.to_dict(), expected_revision=0,
    )
    architecture_revision = store.save_revision(
        project["id"], kind="architecture", schema_version=architecture.schema_version,
        document=architecture.to_dict(), expected_revision=1,
    )
    body = {
        "from_spec_revision_id": spec_revision.id,
        "to_spec_revision_id": spec_revision.id,
        "from_architecture_revision_id": architecture_revision.id,
        "to_architecture_revision_id": architecture_revision.id,
    }

    planned = application.handle(
        "POST", f"/v1/projects/{project['id']}/change-plans",
        body=body, headers=_headers("plan-1"),
    )
    applied = application.handle(
        "POST", f"/v1/projects/{project['id']}/change-applications",
        body=body, headers=_headers("apply-1"),
    )

    assert planned.status == 200 and applied.status == 200
    assert planned.body["change"]["stale"] == [], "nothing changed"
    assert "forgotten" not in planned.body
    assert applied.body["forgotten"] == {}, "and so nothing was forgotten"


def test_a_settled_run_says_how_to_try_again(tmp_path):
    """It keeps its verdict -- that is what makes it evidence -- but an
    operator whose run died on a missing credential should not have to work
    out the way forward for themselves."""

    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.settled")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="failed",
    )
    application = Application(store, workspace_root=tmp_path / "workspaces")
    (tmp_path / "workspaces" / "w").mkdir(parents=True)

    response = application.handle(
        "POST",
        f"/v1/runs/{run['id']}/executions",
        body={"workspace": "w"},
        headers=_headers("settled-run"),
    )

    assert response.status == 400
    message = response.body["message"]
    assert "keeps its result" in message
    assert "prepare a new run" in message


def test_project_state_and_interview_draft_routes(tmp_path):
    application = Application(RichStore(tmp_path))
    application.handle(
        "POST",
        "/v1/projects",
        body={"project_id": "project.demo", "name": "Demo"},
        headers=_headers("create-demo"),
    )

    empty = application.handle("GET", "/v1/projects/project.demo/interview")
    assert empty.status == 200 and empty.body["draft"] is None

    denied = application.handle(
        "PUT",
        "/v1/projects/project.demo/interview",
        body={"document": {"goal": "x"}, "expected_draft_revision": 0},
    )
    assert denied.status == 428

    saved = application.handle(
        "PUT",
        "/v1/projects/project.demo/interview",
        body={"document": {"goal": "x"}, "expected_draft_revision": 0},
        headers=_headers("save-1"),
    )
    assert saved.status == 200 and saved.body["draft"]["draft_revision"] == 1

    stale = application.handle(
        "PUT",
        "/v1/projects/project.demo/interview",
        body={"document": {"goal": "y"}, "expected_draft_revision": 0},
        headers=_headers("save-2"),
    )
    assert stale.status == 409 and stale.body["error"] == "RevisionConflict"

    state = application.handle("GET", "/v1/projects/project.demo/state")
    assert state.status == 200
    assert state.body["interview"]["document"] == {"goal": "x"}
    assert state.body["spec"] is None and state.body["runs"] == []


def test_release_snapshot_is_served_as_zip_bytes(tmp_path):
    store = RichStore(tmp_path)
    project = store.create_project("Demo", project_id="project.demo")
    run = store.create_run(
        project["id"], spec_revision_id=None, architecture_revision_id=None, status="ready"
    )
    application = Application(store)

    missing = application.handle("GET", f"/v1/runs/{run['id']}/release")
    assert missing.status == 404

    release = store.put_artifact(
        b"PK\x05\x06zip-bytes", media_type="application/zip", metadata={}
    )
    store.attach_artifact(run["id"], release.digest, role="source:release-snapshot")

    response = application.handle("GET", f"/v1/runs/{run['id']}/release")

    assert response.status == 200
    assert response.raw == b"PK\x05\x06zip-bytes"
    assert response.content_type == "application/zip"
    headers = dict(response.headers)
    assert headers["Content-Disposition"] == (
        f'attachment; filename="rich-release-{release.digest[:12]}.zip"'
    )
    assert headers["X-RICH-Release-Digest"] == release.digest


def test_interview_turns_route_revises_the_draft_and_needs_a_key(tmp_path):
    from richbuild.interviewer import InterviewOutcome

    class Interviewer:
        def turn(self, *, project_id, project_name, transcript, answers):
            return InterviewOutcome(
                status="questions",
                summary="One thing first.",
                questions=({"prompt": "Who uses it?", "why": "Audiences shape the journeys."},),
                answers=None,
                rejections=(),
                attempts=1,
                source="model",
            )

    application = Application(RichStore(tmp_path), interviewer=Interviewer())
    application.handle(
        "POST",
        "/v1/projects",
        body={"project_id": "project.demo", "name": "Demo"},
        headers=_headers("create-demo"),
    )

    denied = application.handle(
        "POST",
        "/v1/projects/project.demo/interview-turns",
        body={"message": "A todo list.", "expected_draft_revision": 0},
    )
    assert denied.status == 428

    turn = application.handle(
        "POST",
        "/v1/projects/project.demo/interview-turns",
        body={"message": "A todo list.", "expected_draft_revision": 0},
        headers=_headers("turn-1"),
    )
    assert turn.status == 200
    assert turn.body["outcome"]["status"] == "questions"
    assert turn.body["draft"]["draft_revision"] == 1
    assert turn.body["draft"]["document"]["transcript"][1]["text"] == "One thing first."

    stale = application.handle(
        "POST",
        "/v1/projects/project.demo/interview-turns",
        body={"message": "Everyone.", "expected_draft_revision": 0},
        headers=_headers("turn-2"),
    )
    assert stale.status == 409


def test_interview_turns_route_falls_back_to_the_fixed_questions(tmp_path):
    application = Application(RichStore(tmp_path))
    application.handle(
        "POST",
        "/v1/projects",
        body={"project_id": "project.demo", "name": "Demo"},
        headers=_headers("create-demo"),
    )

    turn = application.handle(
        "POST",
        "/v1/projects/project.demo/interview-turns",
        body={"message": "A todo list.", "expected_draft_revision": 0},
        headers=_headers("turn-1"),
    )

    assert turn.status == 200
    assert turn.body["outcome"]["source"] == "form-fallback"


def test_acceptance_vocabulary_is_the_models_own(tmp_path):
    from richbuild.models import AcceptanceAction, AcceptanceStep, ModelValidationError

    application = Application(RichStore(tmp_path))

    response = application.handle("GET", "/v1/vocabulary/acceptance")

    assert response.status == 200
    vocabulary = response.body
    assert [entry["action"] for entry in vocabulary["actions"]] == [
        action.value for action in AcceptanceAction
    ]
    assert "button" in vocabulary["roles"] and "label" in vocabulary["locator_kinds"]
    # What the vocabulary says an action takes is what the constructor accepts.
    for entry in vocabulary["actions"]:
        step = {"action": entry["action"]}
        if "locator" in entry["takes"]:
            step["locator"] = {"kind": "label", "value": "New item"}
        if "value" in entry["takes"]:
            step["value"] = "/" if entry["action"] in vocabulary["path_actions"] else "x"
        assert AcceptanceStep.from_dict(step).action.value == entry["action"]
        if "value" not in entry["takes"]:
            with pytest.raises(ModelValidationError):
                AcceptanceStep.from_dict({**step, "value": "x"})


def test_usage_and_timeline_read_the_durable_run(tmp_path):
    store = RichStore(tmp_path)
    project = store.create_project("Demo", project_id="project.demo")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        budget={
            "max_model_attempts": 4,
            "max_input_tokens": 128_000,
            "max_output_tokens": 32_000,
            "max_cost_usd": "2.50",
            "max_execution_seconds": 480,
        },
        status="ready",
    )
    store.append_event(run["id"], "run.prepared", {"task_count": 2})
    store.append_event(run["id"], "task.failed", {"summary": "lint command exited with 1"})
    application = Application(store)

    usage = application.handle("GET", f"/v1/runs/{run['id']}/usage")
    timeline = application.handle("GET", f"/v1/runs/{run['id']}/timeline")
    later = application.handle(
        "GET", f"/v1/runs/{run['id']}/timeline", query={"after": ["1"]}
    )

    assert usage.status == 200
    from decimal import Decimal

    # Money is a decimal string; the store canonicalizes its scale, so compare
    # as decimals rather than as the digits a person typed.
    assert Decimal(usage.body["budget"]["max_cost_usd"]) == Decimal("2.50")
    assert usage.body["used"]["model_attempts"] == 0
    assert Decimal(usage.body["remaining"]["cost_usd"]) == Decimal("2.50")
    assert usage.body["remaining"]["model_attempts"] == 4
    assert timeline.status == 200 and timeline.body["settled"] is False
    texts = [line["text"] for line in timeline.body["lines"]]
    assert any("run.prepared" in text for text in texts)
    assert any("task.failed" in text and "lint command exited with 1" in text for text in texts)
    assert [line["sequence"] for line in later.body["lines"]] == [2]
    assert application.handle("GET", "/v1/runs/run_missing/usage").status == 404


def test_an_execution_the_host_cannot_run_is_refused_before_it_is_accepted(tmp_path):
    """The drive found a run whose status never moved: the executor died on
    SandboxUnavailable in a background thread one second after the request
    was accepted. An executor that can say why it cannot run is asked first."""

    class UnavailableExecutor:
        def availability(self):
            return "Bubblewrap is required and was not found on PATH"

        def execute(self, **request):  # pragma: no cover - must not be reached
            raise AssertionError("an unavailable executor must not be started")

    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.demo")
    run = store.create_run(
        project["id"], spec_revision_id=None, architecture_revision_id=None, status="ready"
    )
    workspaces = tmp_path / "workspaces"
    (workspaces / "demo").mkdir(parents=True)
    application = Application(store, workspace_root=workspaces, run_executor=UnavailableExecutor())

    response = application.handle(
        "POST",
        f"/v1/runs/{run['id']}/executions",
        body={"workspace": "demo"},
        headers=_headers("execute-demo"),
    )

    assert response.status == 503
    assert response.body["error"] == "RunExecutionUnavailable"
    assert "Bubblewrap" in response.body["message"]
    assert [event["event_type"] for event in store.list_events(run["id"])] == []


def test_an_execution_that_dies_records_why(tmp_path):
    class DyingExecutor:
        def __init__(self):
            self.done = threading.Event()

        def execute(self, **request):
            try:
                raise RuntimeError("the lease could not be claimed\nsecond line")
            finally:
                self.done.set()

    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.demo")
    run = store.create_run(
        project["id"], spec_revision_id=None, architecture_revision_id=None, status="ready"
    )
    workspaces = tmp_path / "workspaces"
    (workspaces / "demo").mkdir(parents=True)
    executor = DyingExecutor()
    application = Application(store, workspace_root=workspaces, run_executor=executor)

    response = application.handle(
        "POST",
        f"/v1/runs/{run['id']}/executions",
        body={"workspace": "demo"},
        headers=_headers("execute-demo"),
    )
    assert response.status == 202
    assert executor.done.wait(5)
    deadline = time.monotonic() + 5
    events = []
    while time.monotonic() < deadline:
        events = store.list_events(run["id"])
        if any(event["event_type"] == "run.execution_error" for event in events):
            break
        time.sleep(0.05)

    error = next(event for event in events if event["event_type"] == "run.execution_error")
    assert error["payload"] == {
        "error_type": "RuntimeError",
        "message": "the lease could not be claimed",
    }
    timeline = application.handle("GET", f"/v1/runs/{run['id']}/timeline")
    assert any(
        "RuntimeError: the lease could not be claimed" in line["text"]
        for line in timeline.body["lines"]
    )


def test_health_names_the_version_that_answers(tmp_path):
    from richbuild import __version__

    application = Application(RichStore(tmp_path))
    health = application.handle("GET", "/v1/health")
    assert health.body["version"] == __version__


def _succeeded_run_with_release(tmp_path):
    store = RichStore(tmp_path / "state")
    project = store.create_project("Demo", project_id="project.push")
    run = store.create_run(
        project["id"],
        spec_revision_id=None,
        architecture_revision_id=None,
        status="ready",
    )
    source = tmp_path / "generated"
    source.mkdir()
    (source / "package.json").write_text('{"name":"demo"}')
    store.set_run_status(run["id"], "running", expected_status="ready")
    store.set_run_status(run["id"], "verifying", expected_status="running")
    store.set_run_status(run["id"], "succeeded", expected_status="verifying")
    release = store.put_artifact(
        create_deployment_snapshot(source),
        media_type="application/vnd.rich.release-source+zip",
    )
    store.attach_artifact(run["id"], release.digest, role="source:release-snapshot")
    return store, run, release


def test_repository_push_over_http_records_and_lists_the_push(tmp_path):
    from richbuild.repository import RepositoryPush

    store, run, release = _succeeded_run_with_release(tmp_path)
    calls = []

    def pusher(snapshot, *, run_id, snapshot_digest, target, committed_at):
        calls.append(target)
        return RepositoryPush(
            run_id=run_id,
            remote=target.remote,
            branch=target.branch,
            commit_sha="e" * 40,
            snapshot_digest=snapshot_digest,
            file_count=1,
            committed_at=committed_at.isoformat(),
            repository_url=target.repository_url,
            created_repository=False,
            already_current=False,
        )

    application = Application(store, repository_pusher=pusher)
    response = application.handle(
        "POST",
        f"/v1/runs/{run['id']}/repository-pushes",
        body={"remote": "https://github.com/maya/tracker", "branch": "release"},
        headers=_headers("push-1"),
    )
    assert response.status == 201, response.body
    assert response.body["push"]["commit_sha"] == "e" * 40
    assert response.body["push"]["snapshot_digest"] == release.digest
    assert calls[0].branch == "release"
    assert calls[0].create is False and calls[0].private is True

    listed = application.handle(
        "GET", f"/v1/runs/{run['id']}/repository-pushes", headers=_headers("push-list")
    )
    assert listed.status == 200
    assert [push["commit_sha"] for push in listed.body["pushes"]] == ["e" * 40]

    refused = application.handle(
        "POST",
        f"/v1/runs/{run['id']}/repository-pushes",
        body={"remote": "git@github.com:maya/tracker.git"},
        headers=_headers("push-2"),
    )
    assert refused.status == 400
    assert "https:// or file://" in refused.body["message"]
    handled = application.handle(
        "POST",
        f"/v1/runs/{run['id']}/repository-pushes",
        body={"remote": "https://github.com/maya/tracker", "token_handle": "x"},
        headers=_headers("push-3"),
    )
    assert handled.status == 400
    assert len(calls) == 1


def test_a_failed_push_is_a_gateway_error_without_the_secret(tmp_path):
    from richbuild.repository import RepositoryError

    store, run, _release = _succeeded_run_with_release(tmp_path)

    def pusher(snapshot, **_kwargs):
        raise RepositoryError("git push failed: remote refused [REDACTED_SECRET]")

    application = Application(store, repository_pusher=pusher)
    response = application.handle(
        "POST",
        f"/v1/runs/{run['id']}/repository-pushes",
        body={"remote": "https://github.com/maya/tracker"},
        headers=_headers("push-fail"),
    )
    assert response.status == 502
    assert response.body["error"] == "RepositoryError"
    assert "remote refused" in response.body["message"]


def test_health_says_whether_a_repository_token_is_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    application = Application(RichStore(tmp_path))
    assert application.handle("GET", "/v1/health").body["repository_push"] == {
        "token_configured": False
    }
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    assert application.handle("GET", "/v1/health").body["repository_push"] == {
        "token_configured": True
    }
