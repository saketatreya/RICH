"""Versioned JSON API for the local RICH control plane."""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .budget import BudgetExceeded
from .control_plane import (
    ApprovalRequired,
    ArchitectProposer,
    ControlPlane,
    InterviewerProposer,
    RunExecutionUnavailable,
    RunExecutor,
)
from .execution import DefaultRunExecutor
from .run_engine import ConcurrentExecutionError
from .preview import (
    PreviewError,
    PreviewOrchestrator,
    default_preview_orchestrator,
)
from .runtime import API_ROUTE, CLAUDE_CODE_ROUTE, default_architect, default_interviewer
from .store import (
    STORE_SCHEMA_VERSION,
    IdempotencyReplay,
    NotFoundError,
    RevisionConflict,
    RichStore,
    StoreError,
)
from .target_packs.nextjs import DestinationNotEmptyError

# Serve without any model route: deterministic planner, fixed questions.
NO_MODEL_ROUTE = "none"


MAX_BODY_BYTES = 2 * 1024 * 1024
# A generated-source bundle is capped at 1 MiB by CodingLimits; verification
# logs are capped at 256 KiB. This leaves room for both and still refuses to
# stream something unbounded into a browser.
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status: int
    body: dict[str, Any]
    # A response is JSON unless it carries raw bytes -- a release ZIP is the
    # one thing the API hands back that is not a document about the system.
    raw: bytes | None = None
    content_type: str = "application/json"
    headers: tuple[tuple[str, str], ...] = ()


class _ExecutionManager:
    """Launch long local builds once while durable state remains authoritative."""

    def __init__(self, store: RichStore, control_plane: ControlPlane):
        self.store = store
        self.control_plane = control_plane
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def start(
        self,
        *,
        run_id: str,
        workspace: Path,
        architecture_approval_id: str | None,
    ) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run["status"] not in {"ready", "running", "verifying"}:
            # A settled run keeps its verdict: that is what makes it evidence.
            # But an operator whose run died on a missing credential should not
            # have to work out for themselves that the way forward is a new run
            # over the same approvals, so the message says it.
            raise ValueError(
                f"run is {run['status']!r} and cannot be executed. A settled "
                "run keeps its result; prepare a new run over the same "
                "approved spec and architecture revisions to try again."
            )
        with self._lock:
            if run_id in self._active:
                return {
                    "run_id": run_id,
                    "status": "running",
                    "active": True,
                }
            self._active.add(run_id)
        self.store.append_event(
            run_id,
            "run.execution_requested",
            {"workspace": str(workspace)},
        )

        def execute() -> None:
            try:
                report = self.control_plane.execute_run(
                    run_id=run_id,
                    workspace=workspace,
                    architecture_approval_id=architecture_approval_id,
                )
            except Exception as exc:
                self.store.append_event(
                    run_id,
                    "run.execution_error",
                    {"error_type": type(exc).__name__},
                )
            else:
                self.store.append_event(
                    run_id,
                    "run.execution_finished",
                    {
                        "status": report.status,
                        "task_statuses": dict(report.task_statuses),
                    },
                )
            finally:
                with self._lock:
                    self._active.discard(run_id)

        threading.Thread(
            target=execute,
            name=f"rich-run-{run_id}",
            daemon=True,
        ).start()
        return {"run_id": run_id, "status": "accepted", "active": True}

    def active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._active


class Application:
    """Framework-neutral request router with durable idempotency."""

    def __init__(
        self,
        store: RichStore,
        *,
        preview_orchestrator: PreviewOrchestrator | None = None,
        workspace_root: str | Path | None = None,
        run_executor: RunExecutor | None = None,
        architect: ArchitectProposer | None = None,
        interviewer: InterviewerProposer | None = None,
        route: str = CLAUDE_CODE_ROUTE,
    ):
        self.store = store
        self.workspace_root = Path(
            workspace_root or (store.root.parent / "workspaces")
        ).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        trusted_run_executor = run_executor or DefaultRunExecutor(
            store, route=route
        )
        self.control_plane = ControlPlane(
            store,
            preview_orchestrator=(
                preview_orchestrator or default_preview_orchestrator()
            ),
            run_executor=trusted_run_executor,
            architect=architect,
            interviewer=interviewer,
            # The boundary belongs to the control plane, not to this dispatch
            # table: an HTTP caller names a directory over the network, and
            # every path into the engine has to refuse the same escapes.
            workspace_root=self.workspace_root,
        )
        self.execution_manager = _ExecutionManager(
            store, self.control_plane
        )

    def handle(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, list[str]] | None = None,
    ) -> ApiResponse:
        method = method.upper()
        request_body = dict(body or {})
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        request_error = validate_local_request(method, normalized_headers)
        if request_error is not None:
            return request_error
        query = query or {}
        idempotency_key = None
        idempotency_owner = None
        operation = f"{method} {path}"
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            idempotency_key = normalized_headers.get("idempotency-key")
            if not idempotency_key:
                return ApiResponse(
                    428,
                    {
                        "error": "IdempotencyKeyRequired",
                        "message": "mutating requests require an Idempotency-Key header",
                    },
                )
            try:
                claim = self.store.claim_idempotency(
                    idempotency_key,
                    operation=operation,
                    request=request_body,
                )
            except Exception as exc:
                return _error_response(exc)
            if isinstance(claim, IdempotencyReplay):
                return ApiResponse(claim.status_code, claim.response)
            idempotency_owner = claim.owner_token

        try:
            response = self._dispatch(method, path, request_body, query)
        except Exception as exc:
            response = _error_response(exc)

        if idempotency_key is not None:
            try:
                if response.status < 500:
                    self.store.complete_idempotency(
                        idempotency_key,
                        owner_token=idempotency_owner,
                        status_code=response.status,
                        response=response.body,
                    )
                else:
                    self.store.abandon_idempotency(
                        idempotency_key,
                        owner_token=idempotency_owner,
                    )
            except Exception as exc:
                return _error_response(exc)
        return response

    def _dispatch(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
        query: Mapping[str, list[str]],
    ) -> ApiResponse:
        parts = [part for part in path.split("/") if part]
        if parts == ["v1", "health"] and method == "GET":
            return ApiResponse(
                200,
                {
                    "status": "ok",
                    "api_version": "v1",
                    "store_schema_version": STORE_SCHEMA_VERSION,
                },
            )
        if parts == ["v1", "vocabulary", "acceptance"] and method == "GET":
            # What an oracle step may say, from the one place that decides it.
            # A surface that offers a dropdown built from this cannot offer a
            # value the step constructor will refuse.
            return ApiResponse(200, acceptance_vocabulary())
        if parts == ["v1", "projects"] and method == "POST":
            project = self.control_plane.create_project(
                project_id=_required(body, "project_id"),
                name=_required(body, "name"),
            )
            return ApiResponse(201, {"project": project})
        if len(parts) == 3 and parts[:2] == ["v1", "projects"] and method == "GET":
            return ApiResponse(200, {"project": self.store.get_project(parts[2])})
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "projects"]
            and parts[3] == "revisions"
            and method == "GET"
        ):
            kind = _query_string(query, "kind")
            return ApiResponse(
                200,
                {
                    "revisions": [
                        asdict(revision)
                        for revision in self.store.list_revisions(
                            parts[2], kind=kind
                        )
                    ]
                },
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "projects"]
            and parts[3] == "runs"
            and method == "GET"
        ):
            return ApiResponse(
                200, {"runs": self.store.list_runs(parts[2])}
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "projects"]
            and parts[3] == "state"
            and method == "GET"
        ):
            return ApiResponse(200, self.control_plane.project_state(parts[2]))
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "projects"]
            and parts[3] == "interview"
            and method == "GET"
        ):
            return ApiResponse(
                200, {"draft": self.control_plane.get_interview_draft(parts[2])}
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "projects"]
            and parts[3] == "interview-turns"
            and method == "POST"
        ):
            # 200, not 201: a turn revises the draft; nothing approvable is
            # created until spec-submissions, and only from answers a person
            # has read.
            return ApiResponse(
                200,
                self.control_plane.interview_turn(
                    parts[2],
                    message=_required(body, "message"),
                    expected_draft_revision=_required_int(
                        body, "expected_draft_revision"
                    ),
                ),
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "projects"]
            and parts[3] == "interview"
            and method == "PUT"
        ):
            return ApiResponse(
                200,
                {
                    "draft": self.control_plane.save_interview_draft(
                        parts[2],
                        document=_required_mapping(body, "document"),
                        expected_draft_revision=_required_int(
                            body, "expected_draft_revision"
                        ),
                    )
                },
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "projects"]
            and parts[3] == "approvals"
            and method == "GET"
        ):
            return ApiResponse(
                200,
                {
                    "approvals": self.store.list_approvals(
                        parts[2],
                        run_id=_query_string(query, "run_id"),
                        status=_query_string(query, "status"),
                    )
                },
            )
        if len(parts) == 2 and parts == ["v1", "projects"] and method == "GET":
            return ApiResponse(
                200, {"projects": self.store.list_projects()}
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "projects"]
            and parts[3] == "interview-questions"
            and method == "POST"
        ):
            answers = body.get("answers")
            if not isinstance(answers, Mapping):
                raise ValueError("answers document is required")
            # 200: this reads what has been said and reports what is still
            # needed. Nothing is stored -- the spec is created by
            # spec-submissions, and only once a human is ready.
            return ApiResponse(
                200,
                self.control_plane.interview_questions(
                    project_id=parts[2],
                    project_name=_required(body, "project_name"),
                    answers=answers,
                    limit=_optional_int(body, "limit") or 3,
                ),
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "projects"]
            and parts[3] == "spec-submissions"
            and method == "POST"
        ):
            submission = self.control_plane.submit_interview(
                project_id=parts[2],
                project_name=_required(body, "project_name"),
                answers=_required_mapping(body, "answers"),
                expected_revision=_required_int(body, "expected_revision"),
            )
            return ApiResponse(
                201,
                {
                    "spec": submission.spec.to_dict(),
                    "revision": asdict(submission.revision),
                    "approval": submission.approval,
                },
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "runs"]
            and parts[3] == "executions"
            and method == "POST"
        ):
            execution = self.execution_manager.start(
                run_id=parts[2],
                workspace=self._workspace_destination(
                    _required(body, "workspace")
                ),
                architecture_approval_id=_optional_string(
                    body, "architecture_approval_id"
                ),
            )
            return ApiResponse(202, {"execution": execution})
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "runs"]
            and parts[3] == "execution"
            and method == "GET"
        ):
            run = self.store.get_run(parts[2])
            # "active" has to mean the durable truth, not this process's memory
            # of it.  The Canvas and `rich serve` are separate processes over
            # one state directory, so a run started by either -- or by the CLI --
            # is invisible to the other's in-memory set while its lease says
            # plainly that it is running.  Reporting idle there invites a second
            # operator to start the run the lease is about to refuse.
            in_process = self.execution_manager.active(parts[2])
            leased = self.store.is_run_execution_leased(parts[2])
            return ApiResponse(
                200,
                {
                    "execution": {
                        "run_id": parts[2],
                        "status": run["status"],
                        "active": in_process or leased,
                        "owned_here": in_process,
                    }
                },
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "projects"]
            and parts[3] == "architecture-submissions"
            and method == "POST"
        ):
            submission = self.control_plane.propose_architecture(
                project_id=parts[2],
                spec_revision_id=_required(body, "spec_revision_id"),
                spec_approval_id=_required(body, "spec_approval_id"),
                expected_revision=_required_int(body, "expected_revision"),
            )
            return _architecture_response(submission)
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "projects"]
            and parts[3] == "architecture-drafts"
            and method == "POST"
        ):
            draft = self.control_plane.draft_architecture(
                project_id=parts[2],
                spec_revision_id=_required(body, "spec_revision_id"),
                spec_approval_id=_required(body, "spec_approval_id"),
                repair=_optional_string(body, "repair"),
            )
            # 200, not 201: nothing was created. The draft is a suggestion
            # until a human applies it through architecture-revisions.
            return ApiResponse(
                200,
                {
                    "architecture": draft.architecture.to_dict(),
                    "decisions": list(draft.decisions),
                    "risks": list(draft.risks),
                    "source": draft.source,
                    "rationale": draft.rationale,
                },
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "projects"]
            and parts[3] in {"change-plans", "change-applications"}
            and method == "POST"
        ):
            action = (
                self.control_plane.plan_change
                if parts[3] == "change-plans"
                else self.control_plane.apply_change
            )
            # 200 for both: a plan creates nothing, and applying withdraws
            # permission to replay rather than creating a resource.
            return ApiResponse(
                200,
                action(
                    project_id=parts[2],
                    from_spec_revision_id=_required(body, "from_spec_revision_id"),
                    to_spec_revision_id=_required(body, "to_spec_revision_id"),
                    from_architecture_revision_id=_required(
                        body, "from_architecture_revision_id"
                    ),
                    to_architecture_revision_id=_required(
                        body, "to_architecture_revision_id"
                    ),
                ),
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "projects"]
            and parts[3] == "node-rebuilds"
            and method == "POST"
        ):
            # 200, not 201: this creates nothing. It withdraws permission to
            # replay one node's last answer, so the next run recomputes it.
            return ApiResponse(
                200,
                {
                    "rebuild": self.control_plane.rebuild_node(
                        project_id=parts[2],
                        node_id=_required(body, "node_id"),
                        architecture_revision_id=_optional_string(
                            body, "architecture_revision_id"
                        ),
                    )
                },
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "projects"]
            and parts[3] == "architecture-revisions"
            and method == "POST"
        ):
            architecture = body.get("architecture")
            if not isinstance(architecture, Mapping):
                raise ValueError("architecture document is required")
            submission = self.control_plane.revise_architecture(
                project_id=parts[2],
                spec_revision_id=_required(body, "spec_revision_id"),
                spec_approval_id=_required(body, "spec_approval_id"),
                expected_revision=_required_int(body, "expected_revision"),
                document=architecture,
                decisions=_optional_strings(body, "decisions"),
                risks=_optional_strings(body, "risks"),
            )
            return _architecture_response(submission)
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "approvals"]
            and parts[3] == "decisions"
            and method == "POST"
        ):
            approval = self.control_plane.decide_approval(
                parts[2],
                approved=_required_bool(body, "approved"),
                actor=_required(body, "actor"),
                reason=str(body.get("reason", "")),
            )
            return ApiResponse(200, {"approval": approval})
        if len(parts) == 3 and parts[:2] == ["v1", "approvals"] and method == "GET":
            return ApiResponse(200, {"approval": self.store.get_approval(parts[2])})
        if len(parts) == 3 and parts[:2] == ["v1", "revisions"] and method == "GET":
            return ApiResponse(200, {"revision": asdict(self.store.get_revision(parts[2]))})
        if parts == ["v1", "runs"] and method == "POST":
            prepared = self.control_plane.prepare_run(
                architecture_approval_id=_required(body, "architecture_approval_id"),
                budget=_required_mapping(body, "budget"),
            )
            return ApiResponse(
                201,
                {
                    "run": prepared.run,
                    "compiled": prepared.compiled.to_dict(),
                    "tasks": list(prepared.tasks),
                    "plan_artifact_digest": prepared.plan_artifact.digest,
                },
            )
        if len(parts) == 3 and parts[:2] == ["v1", "runs"] and method == "GET":
            return ApiResponse(200, {"run": self.store.get_run(parts[2])})
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "runs"]
            and parts[3] == "tasks"
            and method == "GET"
        ):
            return ApiResponse(
                200, {"tasks": self.store.list_tasks(parts[2])}
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "runs"]
            and parts[3] == "release"
            and method == "GET"
        ):
            return self._release_snapshot(parts[2])
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "runs"]
            and parts[3] == "usage"
            and method == "GET"
        ):
            return ApiResponse(200, self.control_plane.run_usage(parts[2]))
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "runs"]
            and parts[3] == "timeline"
            and method == "GET"
        ):
            after = _query_string(query, "after")
            return ApiResponse(
                200,
                self.control_plane.run_timeline(
                    parts[2], after_sequence=int(after) if after else 0
                ),
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "runs"]
            and parts[3] == "artifacts"
            and method == "GET"
        ):
            return ApiResponse(
                200,
                {"artifacts": self.store.list_run_artifacts(parts[2])},
            )
        if (
            len(parts) == 5
            and parts[:2] == ["v1", "runs"]
            and parts[3] == "artifacts"
            and method == "GET"
        ):
            return self._artifact_content(run_id=parts[2], digest=parts[4])
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "runs"]
            and parts[3] == "source-transactions"
            and method == "GET"
        ):
            return ApiResponse(
                200,
                {
                    "source_transactions": self.store.list_source_transactions(
                        parts[2], status=_query_string(query, "status")
                    )
                },
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "runs"]
            and parts[3] == "scaffold"
            and method == "POST"
        ):
            result = self.control_plane.scaffold_run(
                run_id=parts[2],
                destination=self._workspace_destination(
                    _required(body, "destination")
                ),
                package_scope=body.get("package_scope"),
            )
            return ApiResponse(
                201,
                {
                    "destination": str(result.destination),
                    "manifest": result.manifest.as_dict(),
                    "manifest_artifact_digest": result.manifest_artifact.digest,
                },
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "runs"]
            and parts[3] == "cancellation"
            and method == "POST"
        ):
            return ApiResponse(
                202,
                {
                    "execution": self.control_plane.cancel_run(
                        run_id=parts[2],
                        reason=_optional_string(body, "reason")
                        or "canceled by operator",
                    )
                },
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "runs"]
            and parts[3] == "preview-requests"
            and method == "POST"
        ):
            if "neon_token_handle" in body or "vercel_token_handle" in body:
                raise ValueError(
                    "preview secret handles are configured by the trusted server"
                )
            submission = self.control_plane.request_preview(
                run_id=parts[2],
                source_dir=_required(body, "source_dir"),
                neon_project_id=_required(body, "neon_project_id"),
                expires_at=_required_datetime(body, "expires_at"),
                neon_branch_name=_optional_string(body, "neon_branch_name"),
                vercel_project_id=_optional_string(body, "vercel_project_id"),
                vercel_team_id=_optional_string(body, "vercel_team_id"),
                neon_parent_branch_id=_optional_string(
                    body, "neon_parent_branch_id"
                ),
            )
            return ApiResponse(
                201,
                {
                    "preview": submission.preview,
                    "approval": submission.approval,
                },
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "runs"]
            and parts[3] == "previews"
            and method == "GET"
        ):
            return ApiResponse(
                200, {"previews": self.store.list_previews(parts[2])}
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "runs"]
            and parts[3] == "events"
            and method == "GET"
        ):
            after = _query_int(query, "after", default=0)
            return ApiResponse(
                200,
                {"events": self.store.list_events(parts[2], after_sequence=after)},
            )
        if len(parts) == 3 and parts[:2] == ["v1", "previews"] and method == "GET":
            return ApiResponse(
                200, {"preview": self.store.get_preview(parts[2])}
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "previews"]
            and parts[3] == "deployments"
            and method == "POST"
        ):
            deployed = self.control_plane.deploy_preview(
                preview_id=parts[2],
                approval_id=_required(body, "approval_id"),
            )
            return ApiResponse(
                201,
                {
                    "preview": deployed.preview,
                    "result": asdict(deployed.result),
                },
            )
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "previews"]
            and parts[3] == "destroy"
            and method == "POST"
        ):
            return ApiResponse(
                200,
                {
                    "preview": self.control_plane.destroy_preview(
                        preview_id=parts[2]
                    )
                },
            )
        return ApiResponse(404, {"error": "NotFound", "message": "route not found"})

    def _artifact_content(self, *, run_id: str, digest: str) -> ApiResponse:
        """Serve one artifact's bytes, scoped to a run that produced it.

        The digest alone would be a sufficient capability, but routing through
        the run keeps this from becoming an open read oracle over the whole
        content-addressed store: a caller can read what a run they named
        produced, and nothing else.
        """

        attachments = [
            attachment
            for attachment in self.store.list_run_artifacts(run_id)
            if attachment["digest"] == digest
        ]
        if not attachments and not self._run_journals(run_id, digest):
            raise NotFoundError(
                f"artifact {digest!r} is not attached to run {run_id!r}"
            )
        stored = self.store.get_artifact(digest)
        if stored.size > MAX_ARTIFACT_BYTES:
            raise ValueError(
                f"artifact {digest!r} is larger than the maximum served size"
            )
        raw = stored.path.read_bytes()
        body: dict[str, Any] = {
            "artifact": {
                "digest": stored.digest,
                "size": stored.size,
                "media_type": stored.media_type,
                "metadata": dict(stored.metadata),
                "attachments": [
                    {"role": item["role"], "task_id": item["task_id"]}
                    for item in attachments
                ],
            }
        }
        # Most artifacts RICH stores are JSON documents -- generated source
        # bundles, evidence, manifests -- so hand them back parsed rather than
        # making every reader re-decode. Anything else is base64, because a
        # verification log is bytes and may not be valid UTF-8.
        try:
            body["content"] = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body["content_base64"] = base64.b64encode(raw).decode("ascii")
        return ApiResponse(200, body)

    def _release_snapshot(self, run_id: str) -> ApiResponse:
        """The verified release ZIP, as bytes.

        It already existed as the content-addressed artifact the acceptance
        evidence is bound to; the only way to read it was base64 inside a JSON
        document, under a 4 MiB cap. The digest travels in a header so a
        download can be checked against the run's evidence.
        """

        attachments = [
            attachment
            for attachment in self.store.list_run_artifacts(run_id)
            if attachment["role"] == "source:release-snapshot"
        ]
        if not attachments:
            raise NotFoundError(
                f"run {run_id!r} has no verified release snapshot"
            )
        stored = self.store.get_artifact(attachments[-1]["digest"])
        filename = f"rich-release-{stored.digest[:12]}.zip"
        return ApiResponse(
            200,
            {},
            raw=stored.path.read_bytes(),
            content_type="application/zip",
            headers=(
                ("Content-Disposition", f'attachment; filename="{filename}"'),
                ("X-RICH-Release-Digest", stored.digest),
            ),
        )

    def _run_journals(self, run_id: str, digest: str) -> bool:
        """Whether a run's source transactions reference this digest.

        A write-ahead journal holds each file's bytes as they were *before* the
        write, which is the only thing that turns "here is the new file" into a
        real diff. It is recorded on the transaction rather than attached to
        the run, so run-scoping alone would hide it. Authorizing what a run
        journaled keeps the scope honest without hiding the interesting half.
        """

        return any(
            transaction.get(field) == digest
            for transaction in self.store.list_source_transactions(run_id)
            for field in ("journal_digest", "generated_digest")
        )

    def _workspace_destination(self, value: str) -> Path:
        supplied = Path(value)
        candidate = (
            supplied if supplied.is_absolute() else self.workspace_root / supplied
        ).resolve()
        try:
            relative = candidate.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError(
                "scaffold destination must remain inside the configured workspace root"
            ) from exc
        if not relative.parts:
            raise ValueError("scaffold destination cannot be the workspace root")
        return candidate


def _architecture_response(submission: Any) -> ApiResponse:
    return ApiResponse(
        201,
        {
            "architecture": submission.proposal.architecture.to_dict(),
            "decisions": list(submission.proposal.decisions),
            "risks": list(submission.proposal.risks),
            "revision": asdict(submission.revision),
            "approval": submission.approval,
        },
    )


def _optional_strings(body: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = body.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(f"{key} must be a list of strings")
    return tuple(value)


def _error_response(exc: Exception) -> ApiResponse:
    if isinstance(exc, BudgetExceeded):
        # A refusal, not a fault: the request was well-formed and the ceiling
        # said no. 500 would suggest RICH broke.
        return ApiResponse(
            429, {"error": "BudgetExceeded", "message": str(exc)}
        )
    if isinstance(exc, NotFoundError):
        status = 404
    elif isinstance(exc, ApprovalRequired):
        status = 403
    elif isinstance(exc, ConcurrentExecutionError):
        status = 409
    elif isinstance(exc, RunExecutionUnavailable):
        status = 503
    elif isinstance(exc, (RevisionConflict, DestinationNotEmptyError)):
        status = 409
    elif isinstance(exc, PreviewError):
        status = 502
    elif isinstance(exc, StoreError) and "in progress" in str(exc):
        status = 409
    elif isinstance(exc, (ValueError, TypeError, KeyError)):
        status = 400
    else:
        status = 500
    if status == 500:
        return ApiResponse(
            500,
            {
                "error": "InternalServerError",
                "message": "an unexpected internal error occurred",
            },
        )
    return ApiResponse(status, {"error": type(exc).__name__, "message": str(exc)})


def validate_local_request(
    method: str,
    headers: Mapping[str, str],
    *,
    require_host: bool = False,
    require_origin: bool = False,
    require_json: bool = False,
) -> ApiResponse | None:
    """Reject DNS-rebinding and cross-origin browser requests at the local API."""

    allowed = {"localhost", "127.0.0.1", "::1"}
    host = headers.get("host")
    if require_host and not host:
        return ApiResponse(
            403,
            {
                "error": "UntrustedHost",
                "message": "the local API requires a loopback Host header",
            },
        )
    if host:
        try:
            hostname = urlparse(f"http://{host}").hostname
        except ValueError:
            hostname = None
        if hostname not in allowed:
            return ApiResponse(
                403,
                {
                    "error": "UntrustedHost",
                    "message": "the local API accepts only loopback Host headers",
                },
            )
    origin = headers.get("origin")
    mutation = method in {"POST", "PUT", "PATCH", "DELETE"}
    if mutation and require_origin and not origin:
        return ApiResponse(
            403,
            {
                "error": "UntrustedOrigin",
                "message": "browser mutations require a loopback Origin",
            },
        )
    if mutation and origin:
        try:
            parsed = urlparse(origin)
            trusted = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname in allowed
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            trusted = False
        if not trusted:
            return ApiResponse(
                403,
                {
                    "error": "UntrustedOrigin",
                    "message": "browser mutations require a loopback Origin",
                },
            )
    if mutation and require_json:
        content_type = headers.get("content-type", "")
        media_type = content_type.partition(";")[0].strip().lower()
        if media_type != "application/json":
            return ApiResponse(
                415,
                {
                    "error": "UnsupportedMediaType",
                    "message": "mutating requests require application/json",
                },
            )
    return None


def _required(body: Mapping[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _required_mapping(body: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = body.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    return dict(value)


def _required_int(body: Mapping[str, Any], key: str) -> int:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _optional_int(body: Mapping[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _required_bool(body: Mapping[str, Any], key: str) -> bool:
    value = body.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _required_datetime(body: Mapping[str, Any], key: str) -> datetime:
    value = _required(body, key)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{key} must be an ISO 8601 timestamp") from exc


def _optional_string(body: Mapping[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string when supplied")
    return value.strip()


def _query_int(
    query: Mapping[str, list[str]], key: str, *, default: int
) -> int:
    values = query.get(key)
    if not values:
        return default
    try:
        value = int(values[0])
    except ValueError as exc:
        raise ValueError(f"query parameter {key} must be an integer") from exc
    if value < 0:
        raise ValueError(f"query parameter {key} must be non-negative")
    return value


def _query_string(
    query: Mapping[str, list[str]], key: str
) -> str | None:
    values = query.get(key)
    if not values:
        return None
    value = values[0].strip()
    if not value:
        raise ValueError(f"query parameter {key} cannot be empty")
    return value


# The built single-page app. One server answers the API and serves the UI,
# because two ports for one product is how a proxy config becomes a thing
# somebody has to know about.
STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".map": "application/json",
}


def acceptance_vocabulary() -> dict[str, Any]:
    """The closed vocabulary of an acceptance oracle, as the models define it."""

    from .models import AcceptanceAction, BrowserLocatorKind
    from .models._common import _ARIA_ROLES
    from .models.spec import (
        _LOCATOR_ONLY_ACTIONS,
        _LOCATOR_VALUE_ACTIONS,
        _VALUE_ONLY_ACTIONS,
    )

    def takes(action: AcceptanceAction) -> list[str]:
        fields: list[str] = []
        if action in _LOCATOR_ONLY_ACTIONS or action in _LOCATOR_VALUE_ACTIONS:
            fields.append("locator")
        if action in _LOCATOR_VALUE_ACTIONS or action in _VALUE_ONLY_ACTIONS:
            fields.append("value")
        return fields

    return {
        "actions": [
            {"action": action.value, "takes": takes(action)}
            for action in AcceptanceAction
        ],
        "locator_kinds": [kind.value for kind in BrowserLocatorKind],
        "roles": sorted(_ARIA_ROLES),
        "path_actions": [
            AcceptanceAction.NAVIGATE.value,
            AcceptanceAction.ASSERT_URL.value,
        ],
    }


def default_web_root() -> Path:
    """Where `npm --prefix web run build` leaves the app."""

    return Path(__file__).resolve().parents[2] / "web" / "dist"


def handler_for(
    application: Application, web_root: Path | None = None
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if web_root is not None and not parsed.path.startswith("/v1"):
                self._serve_static(parsed.path)
                return
            self._handle()

        def _serve_static(self, path: str) -> None:
            index = web_root / "index.html"
            if not index.is_file():
                self._send_bytes(
                    503,
                    b"<h1>RICH canvas is not built</h1><p>Run "
                    b"<code>npm --prefix web ci &amp;&amp; npm --prefix web run "
                    b"build</code>, then reload.</p>",
                    "text/html; charset=utf-8",
                )
                return
            relative = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            try:
                target = (web_root / relative).resolve()
                target.relative_to(web_root.resolve())
            except (ValueError, OSError):
                self._send_bytes(403, b"forbidden", "text/plain; charset=utf-8")
                return
            if not target.is_file():
                # Client-side routing: unknown paths are the app's to resolve.
                target = index
            self._send_bytes(
                200,
                target.read_bytes(),
                STATIC_CONTENT_TYPES.get(
                    target.suffix, "application/octet-stream"
                ),
            )

        def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            self._handle()

        def do_PUT(self) -> None:
            self._handle()

        def _handle(self) -> None:
            parsed = urlparse(self.path)
            try:
                body = self._read_json() if self.command != "GET" else {}
            except Exception as exc:
                self._send(_error_response(exc))
                return
            response = application.handle(
                self.command,
                parsed.path,
                body=body,
                headers={key: value for key, value in self.headers.items()},
                query=parse_qs(parsed.query),
            )
            self._send(response)

        def _read_json(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ValueError("Content-Length must be an integer") from exc
            if length < 0 or length > MAX_BODY_BYTES:
                raise ValueError(f"request body exceeds {MAX_BODY_BYTES} bytes")
            if length == 0:
                return {}
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("JSON request body must be an object")
            return value

        def _send(self, response: ApiResponse) -> None:
            if response.raw is not None:
                payload = response.raw
            else:
                payload = (
                    json.dumps(response.body, sort_keys=True, default=str) + "\n"
                ).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            for name, value in response.headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def serve(
    state_dir: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8767,
    web_root: str | Path | None = None,
    architect: Any | None = None,
    interviewer: Any | None = None,
    route: str = CLAUDE_CODE_ROUTE,
) -> None:
    """Serve the API and the canvas on one port.

    The architect and the interviewer are built here rather than left to the
    caller, because a server that silently plans deterministically or silently
    asks the fixed questions is a server whose headline features are
    unreachable. Both default builders return None when no model route is
    available, so a host with no login still starts, still plans, still asks
    -- and every draft says which one answered.
    """

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the local API binds to loopback only")
    root = Path(web_root) if web_root is not None else default_web_root()
    # "none" is a real mode, not a missing credential: a host with no model
    # route still plans deterministically and asks the fixed questions, and
    # says so. Execution is built on the api route and fails closed at run
    # time, where the missing credential is the honest error.
    no_model = route == NO_MODEL_ROUTE
    if architect is None and not no_model:
        architect = default_architect(route=route)
    if interviewer is None and not no_model:
        interviewer = default_interviewer(route=route)
    application = Application(
        RichStore(state_dir),
        architect=architect,
        interviewer=interviewer,
        route=API_ROUTE if no_model else route,
    )
    server = ThreadingHTTPServer((host, port), handler_for(application, root))
    # flush: stdout is block-buffered when piped, and a server that runs
    # forever would then print its own address only once someone kills it.
    print(f"RICH → http://{host}:{port}", flush=True)
    print(f"  route  {route}", flush=True)
    print(
        "  architect  model-backed"
        if architect is not None
        else "  architect  deterministic planner (that route is unavailable)",
        flush=True,
    )
    print(
        "  interviewer  model-backed"
        if interviewer is not None
        else "  interviewer  fixed questions (that route is unavailable)",
        flush=True,
    )
    print(f"  api    http://{host}:{port}/v1/health", flush=True)
    print(
        "  canvas ready"
        if (root / "index.html").is_file()
        else "  canvas NOT BUILT — run: npm --prefix web ci && npm --prefix web run build",
        flush=True,
    )
    print(f"  state  {Path(state_dir).resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
