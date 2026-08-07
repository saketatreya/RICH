from datetime import datetime, timedelta, timezone
import threading
import time

import pytest

from rich_v2.api import V2Application
from rich_v2.preview import PreviewResult, create_deployment_snapshot
from rich_v2.scheduler import SchedulerReport
from rich_v2.store import RichStore


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
    application = V2Application(RichStore(tmp_path))

    response = application.handle(
        "POST",
        "/v2/projects",
        body={"project_id": "project.demo", "name": "Demo"},
    )

    assert response.status == 428
    assert response.body["error"] == "IdempotencyKeyRequired"


def test_completed_mutation_replays_and_key_cannot_change_request(tmp_path):
    application = V2Application(RichStore(tmp_path))
    request = {"project_id": "project.demo", "name": "Demo"}

    first = application.handle(
        "POST",
        "/v2/projects",
        body=request,
        headers=_headers("create-demo"),
    )
    replay = application.handle(
        "POST",
        "/v2/projects",
        body=request,
        headers=_headers("create-demo"),
    )
    conflict = application.handle(
        "POST",
        "/v2/projects",
        body={"project_id": "project.other", "name": "Other"},
        headers=_headers("create-demo"),
    )

    assert first.status == replay.status == 201
    assert first.body == replay.body
    assert conflict.status == 409
    assert conflict.body["error"] == "RevisionConflict"


def test_health_and_project_reads_are_nonmutating(tmp_path):
    application = V2Application(RichStore(tmp_path))
    application.handle(
        "POST",
        "/v2/projects",
        body={"project_id": "project.demo", "name": "Demo"},
        headers=_headers("create-demo"),
    )

    health = application.handle("GET", "/v2/health")
    project = application.handle("GET", "/v2/projects/project.demo")

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
    application = V2Application(
        store,
        workspace_root=workspaces,
        run_executor=executor,
    )

    response = application.handle(
        "POST",
        f"/v2/runs/{run['id']}/executions",
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
    application = V2Application(RichStore(tmp_path))

    hostile_host = application.handle(
        "GET",
        "/v2/health",
        headers={"Host": "attacker.example"},
    )
    hostile_origin = application.handle(
        "POST",
        "/v2/projects",
        body={"project_id": "project.demo", "name": "Demo"},
        headers={
            "Host": "127.0.0.1:8765",
            "Origin": "https://attacker.example",
            "Idempotency-Key": "hostile-origin",
        },
    )

    assert hostile_host.status == 403
    assert hostile_host.body["error"] == "UntrustedHost"
    assert hostile_origin.status == 403
    assert hostile_origin.body["error"] == "UntrustedOrigin"


def test_api_scaffold_destinations_stay_inside_configured_workspace(tmp_path):
    application = V2Application(
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
    application = V2Application(RichStore(tmp_path))

    def crash(**_kwargs):
        raise RuntimeError("sensitive internal path /private/secret")

    monkeypatch.setattr(application.control_plane, "create_project", crash)
    response = application.handle(
        "POST",
        "/v2/projects",
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
    application = V2Application(
        store, preview_orchestrator=FakePreviewOrchestrator()
    )

    requested = application.handle(
        "POST",
        f"/v2/runs/{run['id']}/preview-requests",
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
        f"/v2/previews/{preview['id']}/deployments",
        body={"approval_id": approval["id"]},
        headers=_headers("preview-deploy-before-approval"),
    )
    decided = application.handle(
        "POST",
        f"/v2/approvals/{approval['id']}/decisions",
        body={"approved": True, "actor": "founder"},
        headers=_headers("preview-approve"),
    )
    deployed = application.handle(
        "POST",
        f"/v2/previews/{preview['id']}/deployments",
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
        "GET", f"/v2/runs/{run['id']}/previews"
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
    application = V2Application(
        store, preview_orchestrator=FakePreviewOrchestrator()
    )

    response = application.handle(
        "POST",
        f"/v2/runs/{run['id']}/preview-requests",
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
