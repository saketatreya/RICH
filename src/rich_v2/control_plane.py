"""Approval-gated local control plane for the first RICH v2 vertical slice."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Any, Iterable, Mapping, Protocol
from uuid import uuid4

from .budget import RunBudget
from .compiler import CompiledArchitecture, compile_architecture
from .interview import AdaptiveInterview, InterviewState
from .models import ApprovalGate, ArchitectureSpecV2, ProjectSpecV2
from .planner import ArchitectureProposal, plan_nextjs_architecture
from .preview import (
    PreviewOrchestrator,
    PreviewRequest,
    PreviewResult,
    PreviewTeardown,
    create_deployment_snapshot,
    extract_deployment_snapshot,
)
from .store import Artifact, Revision, RichStore, StoreError
from .target_packs.nextjs import (
    NextJsTargetPack,
    NextJsTargetPackConfig,
    ScaffoldManifest,
)


class ApprovalRequired(PermissionError):
    """A control-plane mutation lacks the matching approved gate."""


class UnsupportedTargetPack(ValueError):
    """The compiled architecture requests a target pack not installed locally."""


class RunExecutionUnavailable(RuntimeError):
    """This control plane has no trusted run executor configured."""


class RunExecutor(Protocol):
    def execute(
        self,
        *,
        run_id: str,
        workspace: str | Path,
        architecture_approval_id: str | None = None,
    ) -> Any:
        """Execute or resume one approval-bound durable run."""


class ArchitectProposer(Protocol):
    def propose(
        self,
        spec: ProjectSpecV2,
        *,
        target_pack: str,
        repair: str | None = None,
    ) -> ArchitectureProposal:
        """Propose one decomposition. May raise; the caller decides what next."""


@dataclass(frozen=True, slots=True)
class ArchitectureDraft:
    """A proposal that has not been recorded, and may never be.

    Drafting and revising are deliberately separate operations. A proposal the
    control plane stored on the model's say-so would be a decision; a proposal
    a human can read, diff and discard is a suggestion. v1's canvas settled
    this the same way: compute the change, show the diff, commit only on Apply.
    """

    architecture: ArchitectureSpecV2
    decisions: tuple[str, ...]
    risks: tuple[str, ...]
    source: str
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class SpecSubmission:
    spec: ProjectSpecV2
    revision: Revision
    approval: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ArchitectureSubmission:
    proposal: ArchitectureProposal
    revision: Revision
    approval: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PreparedRun:
    run: dict[str, Any]
    compiled: CompiledArchitecture
    plan_artifact: Artifact
    tasks: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ScaffoldResult:
    manifest: ScaffoldManifest
    manifest_artifact: Artifact
    destination: Path


@dataclass(frozen=True, slots=True)
class PreviewSubmission:
    preview: dict[str, Any]
    approval: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PreviewDeploymentResult:
    preview: dict[str, Any]
    result: PreviewResult


class ControlPlane:
    """Orchestrates durable state without hiding approval or evidence boundaries."""

    def __init__(
        self,
        store: RichStore,
        *,
        preview_orchestrator: PreviewOrchestrator | None = None,
        run_executor: RunExecutor | None = None,
        architect: ArchitectProposer | None = None,
        workspace_root: str | Path | None = None,
    ):
        self.store = store
        self.preview_orchestrator = preview_orchestrator
        self.run_executor = run_executor
        self.architect = architect
        # Confinement belongs to whoever built this control plane, because
        # whether it is needed depends on who supplies the path. A network
        # caller must never name a directory outside the root the operator
        # chose; an operator at their own shell can already write anywhere,
        # and pretending otherwise is friction rather than safety. Set it and
        # every entry point through this instance inherits the boundary.
        self.workspace_root = (
            Path(workspace_root).resolve() if workspace_root is not None else None
        )

    def _confined(self, value: str | Path, *, label: str) -> Path:
        candidate = Path(value)
        if self.workspace_root is None:
            return candidate
        resolved = (
            candidate
            if candidate.is_absolute()
            else self.workspace_root / candidate
        ).resolve()
        try:
            relative = resolved.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError(
                f"{label} must remain inside the configured workspace root"
            ) from exc
        if not relative.parts:
            raise ValueError(f"{label} cannot be the workspace root itself")
        return resolved

    def create_project(self, *, project_id: str, name: str) -> dict[str, Any]:
        return self.store.create_project(name, project_id=project_id)

    def rebuild_node(
        self,
        *,
        project_id: str,
        node_id: str,
        architecture_revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Mark one architecture node stale so the next run regenerates it.

        The Canvas is built around this operation and v2 had no equivalent: its
        only granularity was the whole run, so correcting one component meant
        re-paying for every component beside it.

        Forgetting a memo is deliberately all this does. It cannot invalidate
        evidence, because evidence is not reused in the first place -- every
        run re-verifies from scratch. What it drops is permission to replay an
        answer, which is the only thing that was ever carried forward.
        """

        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("node_id cannot be empty")
        self.store.get_project(project_id)
        if architecture_revision_id is not None:
            revision = self.store.get_revision(architecture_revision_id)
            if revision.project_id != project_id or revision.kind != "architecture":
                raise ValueError(
                    "architecture revision does not belong to the requested project"
                )
            architecture = ArchitectureSpecV2.from_dict(revision.document)
            known = {node.id for node in architecture.nodes}
            if node_id not in known:
                raise ValueError(
                    f"node {node_id!r} is not in that architecture; "
                    f"expected one of {sorted(known)}"
                )
        forgotten = self.store.forget_generation_memos(
            project_id=project_id, node_id=node_id
        )
        return {
            "project_id": project_id,
            "node_id": node_id,
            "forgotten_generations": forgotten,
        }

    def interview_questions(
        self,
        *,
        project_id: str,
        project_name: str,
        answers: Mapping[str, Any],
        limit: int = 3,
    ) -> dict[str, Any]:
        """Ask what still needs answering, given what has been said so far.

        AdaptiveInterview has always been adaptive -- it asks about roles only
        once identity is mentioned, about a data policy only once persistence
        is -- and nothing ever called it before compile(). So the interview
        behaved like a fixed form that rejected incomplete submissions, and a
        human had to guess which questions their project actually needed.

        Reads nothing and writes nothing; it exists so the next question can
        depend on the last answer.
        """

        interview = AdaptiveInterview(
            InterviewState(
                project_id=project_id,
                project_name=project_name,
                answers=dict(answers),
                revision=len(self.store.list_revisions(project_id, kind="product_spec"))
                + 1,
            )
        )
        outstanding = interview.next_questions(limit=limit)
        return {
            "project_id": project_id,
            "questions": [
                {
                    "id": question.id,
                    "prompt": question.prompt,
                    "answer_kind": question.answer_kind,
                    "rationale": question.rationale,
                }
                for question in outstanding
            ],
            "complete": not outstanding,
        }

    def submit_interview(
        self,
        *,
        project_id: str,
        project_name: str,
        answers: Mapping[str, Any],
        expected_revision: int,
    ) -> SpecSubmission:
        existing_specs = self.store.list_revisions(project_id, kind="product_spec")
        spec_revision = len(existing_specs) + 1
        spec = AdaptiveInterview(
            InterviewState(
                project_id=project_id,
                project_name=project_name,
                answers=dict(answers),
                revision=spec_revision,
            )
        ).compile()
        revision = self.store.save_revision(
            project_id,
            kind="product_spec",
            schema_version=spec.schema_version,
            document=spec.to_dict(),
            expected_revision=expected_revision,
        )
        approval = self.store.request_approval(
            project_id,
            gate=ApprovalGate.PRODUCT_SPEC.value,
            request={
                "revision_id": revision.id,
                "spec_revision": spec.revision,
                "requirement_ids": sorted(spec.requirement_index),
                "acceptance_scenario_ids": sorted(spec.scenario_index),
            },
        )
        return SpecSubmission(spec, revision, approval)

    def decide_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        actor: str,
        reason: str = "",
    ) -> dict[str, Any]:
        if not actor.strip():
            raise ValueError("approval actor cannot be empty")
        return self.store.decide_approval(
            approval_id,
            approved=approved,
            decision={"actor": actor.strip(), "reason": reason.strip()},
        )

    def propose_architecture(
        self,
        *,
        project_id: str,
        spec_revision_id: str,
        spec_approval_id: str,
        expected_revision: int,
    ) -> ArchitectureSubmission:
        spec = self._approved_spec(
            project_id=project_id,
            spec_revision_id=spec_revision_id,
            spec_approval_id=spec_approval_id,
        )
        proposal = plan_nextjs_architecture(spec)
        return self._save_architecture(
            project_id=project_id,
            spec_revision_id=spec_revision_id,
            expected_revision=expected_revision,
            proposal=proposal,
        )

    def draft_architecture(
        self,
        *,
        project_id: str,
        spec_revision_id: str,
        spec_approval_id: str,
        repair: str | None = None,
    ) -> ArchitectureDraft:
        """Propose an architecture without recording anything.

        Nothing durable changes here: no revision, no approval, no bump to the
        project's revision counter. That is the point. A human reads the draft
        against whatever is current, edits it or asks again, and only a call to
        ``revise_architecture`` makes it real.

        Falls back to the deterministic planner when no architect is
        configured, and says which one answered, because a template and a model
        are not interchangeable and a reviewer should not have to guess.
        """

        spec = self._approved_spec(
            project_id=project_id,
            spec_revision_id=spec_revision_id,
            spec_approval_id=spec_approval_id,
        )
        if self.architect is None:
            proposal = plan_nextjs_architecture(spec)
            return ArchitectureDraft(
                architecture=proposal.architecture,
                decisions=tuple(proposal.decisions),
                risks=tuple(proposal.risks),
                source="planner",
            )
        proposal = self.architect.propose(
            spec, target_pack=NextJsTargetPack.target_pack_id, repair=repair
        )
        architecture = proposal.architecture
        architecture.validate_against_project(spec)
        compile_architecture(architecture, spec)
        return ArchitectureDraft(
            architecture=architecture,
            decisions=tuple(proposal.decisions),
            risks=tuple(proposal.risks),
            # Reported, not assumed. An architect that could not assemble a
            # design hands back the baseline, and calling that "model" would
            # be the one lie this surface cannot afford.
            source=proposal.source,
            rationale=str(architecture.metadata.get("rationale", "")),
        )

    def _approved_spec(
        self, *, project_id: str, spec_revision_id: str, spec_approval_id: str
    ) -> ProjectSpecV2:
        self._require_approval(
            spec_approval_id,
            gate=ApprovalGate.PRODUCT_SPEC.value,
            project_id=project_id,
            revision_id=spec_revision_id,
        )
        spec_revision = self.store.get_revision(spec_revision_id)
        if (
            spec_revision.project_id != project_id
            or spec_revision.kind != "product_spec"
        ):
            raise ValueError("spec revision does not belong to the requested project")
        return ProjectSpecV2.from_dict(spec_revision.document)

    def revise_architecture(
        self,
        *,
        project_id: str,
        spec_revision_id: str,
        spec_approval_id: str,
        expected_revision: int,
        document: Mapping[str, Any],
        decisions: Iterable[str] = (),
        risks: Iterable[str] = (),
    ) -> ArchitectureSubmission:
        """Record an architecture a human authored or corrected.

        ``propose_architecture`` discards whatever it is given and re-derives
        the graph, which makes rejecting a proposal a dead end: the only way to
        a different architecture is to change the spec until the planner
        happens to emit one. That is a veto, not a review.

        This takes the document instead. It is not a weaker gate -- the
        document goes through exactly the same constructor, the same
        ``validate_against_project``, and the same compiler as anything the
        planner produces, and it lands as a new immutable revision needing its
        own approval. What changes is only who may author the proposal.
        """

        spec = self._approved_spec(
            project_id=project_id,
            spec_revision_id=spec_revision_id,
            spec_approval_id=spec_approval_id,
        )
        architecture = ArchitectureSpecV2.from_dict(document)
        if architecture.project_id != project_id:
            raise ValueError("architecture belongs to a different project")
        architecture.validate_against_project(spec)
        # Compile before storing. A graph that validates but cannot be compiled
        # into tasks is not an architecture anyone can act on, and finding that
        # out at run-preparation time would strand an approved revision.
        compile_architecture(architecture, spec)
        return self._save_architecture(
            project_id=project_id,
            spec_revision_id=spec_revision_id,
            expected_revision=expected_revision,
            proposal=ArchitectureProposal(
                architecture=architecture,
                decisions=tuple(decisions),
                risks=tuple(risks),
            ),
        )

    def _save_architecture(
        self,
        *,
        project_id: str,
        spec_revision_id: str,
        expected_revision: int,
        proposal: ArchitectureProposal,
    ) -> ArchitectureSubmission:
        architecture = proposal.architecture
        revision = self.store.save_revision(
            project_id,
            kind="architecture",
            schema_version=architecture.schema_version,
            document=architecture.to_dict(),
            expected_revision=expected_revision,
        )
        approval = self.store.request_approval(
            project_id,
            gate=ApprovalGate.ARCHITECTURE.value,
            request={
                "revision_id": revision.id,
                "spec_revision_id": spec_revision_id,
                "target_pack": architecture.target_pack,
                "node_ids": sorted(architecture.node_index),
                "decisions": list(proposal.decisions),
                "risks": list(proposal.risks),
            },
        )
        return ArchitectureSubmission(proposal, revision, approval)

    def prepare_run(
        self,
        *,
        architecture_approval_id: str,
        budget: Mapping[str, Any],
    ) -> PreparedRun:
        approved_budget = RunBudget.from_mapping(budget).to_mapping()
        approval = self._require_approval(
            architecture_approval_id, gate=ApprovalGate.ARCHITECTURE.value
        )
        request = approval["request"]
        architecture_revision = self.store.get_revision(request["revision_id"])
        spec_revision = self.store.get_revision(request["spec_revision_id"])
        project_id = architecture_revision.project_id
        if spec_revision.project_id != project_id:
            raise ValueError("architecture and product spec revisions belong to different projects")
        architecture = ArchitectureSpecV2.from_dict(architecture_revision.document)
        project = ProjectSpecV2.from_dict(spec_revision.document)
        compiled = compile_architecture(architecture, project)
        run = self.store.create_run(
            project_id,
            spec_revision_id=spec_revision.id,
            architecture_revision_id=architecture_revision.id,
            budget=approved_budget,
            status="ready",
        )
        tasks = tuple(
            self.store.create_task(
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
            for task in compiled.tasks
        )
        plan_bytes = (
            json.dumps(compiled.to_dict(), sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        artifact = self.store.put_artifact(
            plan_bytes,
            media_type="application/vnd.rich.compiled-plan+json",
            metadata={
                "architecture_revision_id": architecture_revision.id,
                "spec_revision_id": spec_revision.id,
            },
        )
        self.store.attach_artifact(run["id"], artifact.digest, role="compiled_plan")
        self.store.append_event(
            run["id"],
            "run.prepared",
            {
                "task_count": len(tasks),
                "compiled_plan_digest": artifact.digest,
                "architecture_approval_id": architecture_approval_id,
            },
        )
        for task, compiled_task in zip(tasks, compiled.tasks, strict=True):
            self.store.append_event(
                run["id"],
                "task.planned",
                {
                    "node_id": compiled_task.node_id,
                    "dependency_ids": list(compiled_task.dependency_ids),
                    "owned_paths": list(compiled_task.owned_paths),
                },
                task_id=task["id"],
            )
        return PreparedRun(run, compiled, artifact, tasks)

    def scaffold_run(
        self,
        *,
        run_id: str,
        destination: str | Path,
        package_scope: str | None = None,
    ) -> ScaffoldResult:
        destination = self._confined(destination, label="scaffold destination")
        run = self.store.get_run(run_id)
        if run["status"] != "ready":
            raise ValueError(f"run must be ready to scaffold, got {run['status']!r}")
        architecture_revision = self.store.get_revision(
            run["architecture_revision_id"]
        )
        spec_revision = self.store.get_revision(run["spec_revision_id"])
        if (
            architecture_revision.kind != "architecture"
            or spec_revision.kind != "product_spec"
            or architecture_revision.project_id != run["project_id"]
            or spec_revision.project_id != run["project_id"]
        ):
            raise ValueError(
                "run revisions must be its matching architecture and product spec"
            )
        architecture = ArchitectureSpecV2.from_dict(architecture_revision.document)
        spec = ProjectSpecV2.from_dict(spec_revision.document)
        architecture.validate_against_project(spec)
        if architecture.target_pack != NextJsTargetPack.target_pack_id:
            raise UnsupportedTargetPack(architecture.target_pack)
        project = self.store.get_project(run["project_id"])
        project_name = _dns_name(project["name"], fallback=project["id"])
        target_pack = NextJsTargetPack(
            NextJsTargetPackConfig(
                project_name=project_name,
                package_scope=package_scope,
                project_spec=spec,
                architecture=architecture,
            )
        )
        manifest = target_pack.scaffold(destination)
        manifest_bytes = manifest.to_json().encode("utf-8")
        artifact = self.store.put_artifact(
            manifest_bytes,
            media_type="application/vnd.rich.target-pack-manifest+json",
            metadata={
                "run_id": run_id,
                "target_pack": manifest.target_pack,
                "target_pack_version": manifest.target_pack_version,
                "content_digest": manifest.content_digest,
            },
        )
        self.store.attach_artifact(
            run_id, artifact.digest, role="scaffold_manifest"
        )
        self.store.append_event(
            run_id,
            "scaffold.completed",
            {
                "destination": str(Path(destination).absolute()),
                "manifest_digest": artifact.digest,
                "content_digest": manifest.content_digest,
            },
        )
        return ScaffoldResult(manifest, artifact, Path(destination).absolute())

    def execute_run(
        self,
        *,
        run_id: str,
        workspace: str | Path,
        architecture_approval_id: str | None = None,
    ) -> Any:
        if self.run_executor is None:
            raise RunExecutionUnavailable(
                "no trusted run executor is configured"
            )
        workspace = self._confined(workspace, label="run workspace")
        return self.run_executor.execute(
            run_id=run_id,
            workspace=workspace,
            architecture_approval_id=architecture_approval_id,
        )

    def cancel_run(
        self, *, run_id: str, reason: str = "canceled by operator"
    ) -> dict[str, Any]:
        """Ask a run to stop at its next checkpoint.

        Cooperative by design. The engine unwinds through the paths it already
        has -- releasing its lease, rolling back a prepared source transaction,
        settling its budget reservation -- rather than being killed partway
        through a write it has not finished recording.
        """

        run = self.store.request_run_cancellation(run_id, reason=reason)
        self.store.append_event(
            run_id, "run.cancellation_requested", {"reason": reason}
        )
        standing = self.store.run_cancellation(run_id)
        return {
            "run_id": run_id,
            "status": run["status"],
            "cancellation": standing,
        }

    def request_preview(
        self,
        *,
        run_id: str,
        source_dir: str | Path,
        neon_project_id: str,
        expires_at: datetime,
        neon_branch_name: str | None = None,
        vercel_project_id: str | None = None,
        vercel_team_id: str | None = None,
        neon_parent_branch_id: str | None = None,
        neon_token_handle: str = "neon.api_token",
        vercel_token_handle: str = "vercel.api_token",
    ) -> PreviewSubmission:
        run = self.store.get_run(run_id)
        if run["status"] != "succeeded":
            raise ValueError(
                "preview requires a succeeded run with release evidence; "
                f"run is {run['status']!r}"
            )
        source = Path(source_dir).resolve()
        scaffold_destinations = {
            Path(event["payload"]["destination"]).resolve()
            for event in self.store.list_events(run_id)
            if event["event_type"] == "scaffold.completed"
            and isinstance(event["payload"].get("destination"), str)
        }
        if source not in scaffold_destinations:
            raise ValueError(
                "preview source must be a scaffold destination owned by this run"
            )
        project = self.store.get_project(run["project_id"])
        project_name = _dns_name(project["name"], fallback=project["id"])
        branch_name = neon_branch_name or _preview_branch_name(run_id)
        request = PreviewRequest(
            run_id=run_id,
            source_dir=source,
            project_name=project_name,
            neon_project_id=neon_project_id,
            neon_branch_name=branch_name,
            expires_at=expires_at,
            neon_token_handle=neon_token_handle,
            vercel_token_handle=vercel_token_handle,
            neon_parent_branch_id=neon_parent_branch_id,
            vercel_project_id=vercel_project_id,
            vercel_team_id=vercel_team_id,
        )
        preview_id = f"preview_{uuid4().hex}"
        snapshot_content = create_deployment_snapshot(source)
        live_snapshot_digest = hashlib.sha256(snapshot_content).hexdigest()
        release_candidates = [
            attachment
            for attachment in self.store.list_run_artifacts(run_id)
            if attachment["role"] == "source:release-snapshot"
        ]
        if not release_candidates:
            raise ValueError(
                "preview requires the release source snapshot verified by the run"
            )
        final_attempts = {
            (task["node_id"], int(task["attempt"]))
            for task in self.store.list_tasks(run_id)
            if task["status"] in {"succeeded", "cached"}
        }
        final_candidates = [
            attachment
            for attachment in release_candidates
            if (
                attachment["metadata"].get("node_id"),
                attachment["metadata"].get("attempt"),
            )
            in final_attempts
        ]
        if final_candidates:
            release_candidates = final_candidates
        matching = [
            attachment
            for attachment in release_candidates
            if attachment["digest"] == live_snapshot_digest
        ]
        if len(matching) != 1:
            raise ValueError(
                "preview source does not exactly match the run's verified release "
                "snapshot"
            )
        release_artifact = self.store.get_artifact(matching[0]["digest"])
        snapshot_content = release_artifact.path.read_bytes()
        snapshot_artifact = self.store.put_artifact(
            snapshot_content,
            media_type="application/vnd.rich.preview-source+zip",
            metadata={
                "preview_id": preview_id,
                "run_id": run_id,
                "kind": "approved_preview_source",
            },
        )
        source_digest = f"sha256:{snapshot_artifact.digest}"
        durable_request = {
            "run_id": request.run_id,
            "source_dir": str(request.source_dir),
            "source_digest": source_digest,
            "source_snapshot_digest": snapshot_artifact.digest,
            "project_name": request.project_name,
            "neon_project_id": request.neon_project_id,
            "neon_branch_name": request.neon_branch_name,
            "expires_at": request.expires_at.isoformat(),
            "neon_token_handle": request.neon_token_handle,
            "vercel_token_handle": request.vercel_token_handle,
            "neon_parent_branch_id": request.neon_parent_branch_id,
            "vercel_project_id": request.vercel_project_id,
            "vercel_team_id": request.vercel_team_id,
        }
        approval_request = {
            "preview_id": preview_id,
            "run_id": run_id,
            "provider": "vercel",
            "database_provider": "neon",
            "source_dir": str(request.source_dir),
            "source_digest": source_digest,
            "source_snapshot_digest": snapshot_artifact.digest,
            "project_name": request.project_name,
            "neon_project_id": request.neon_project_id,
            "neon_branch_name": request.neon_branch_name,
            "expires_at": request.expires_at.isoformat(),
            "vercel_project_id": request.vercel_project_id,
            "vercel_team_id": request.vercel_team_id,
        }
        preview, approval = self.store.request_preview(
            run_id,
            request=durable_request,
            approval_request=approval_request,
            preview_id=preview_id,
        )
        self.store.attach_artifact(
            run_id,
            snapshot_artifact.digest,
            role="preview_source_snapshot",
        )
        self.store.append_event(
            run_id,
            "preview.approval_requested",
            {
                "preview_id": preview_id,
                "approval_id": approval["id"],
                "source_digest": source_digest,
                "source_snapshot_digest": snapshot_artifact.digest,
                "expires_at": request.expires_at.isoformat(),
            },
        )
        return PreviewSubmission(preview, approval)

    def deploy_preview(
        self,
        *,
        preview_id: str,
        approval_id: str,
    ) -> PreviewDeploymentResult:
        orchestrator = self._preview_orchestrator()
        preview = self.store.get_preview(preview_id)
        if preview["approval_id"] != approval_id:
            raise ApprovalRequired("approval belongs to a different preview")
        approval = self._require_approval(
            approval_id,
            gate=ApprovalGate.PREVIEW_DEPLOYMENT.value,
            project_id=self.store.get_run(preview["run_id"])["project_id"],
        )
        if approval["request"].get("preview_id") != preview_id:
            raise ApprovalRequired("approval belongs to a different preview")
        if preview["status"] != "awaiting_approval":
            raise ValueError(
                f"preview must be awaiting approval, got {preview['status']!r}"
            )
        approved_digest = preview["request"]["source_digest"]
        snapshot_digest = str(
            preview["request"].get("source_snapshot_digest", "")
        )
        if (
            not snapshot_digest
            or approval["request"].get("source_snapshot_digest")
            != snapshot_digest
            or approved_digest != f"sha256:{snapshot_digest}"
        ):
            raise ApprovalRequired(
                "approval is not bound to the durable preview source snapshot"
            )
        snapshot = self.store.get_artifact(snapshot_digest)
        snapshot_content = snapshot.path.read_bytes()
        if hashlib.sha256(snapshot_content).hexdigest() != snapshot.digest:
            raise StoreError("preview source snapshot failed content verification")
        self.store.set_preview_status(
            preview_id,
            "deploying",
            expected_status="awaiting_approval",
            error=None,
        )
        self.store.append_event(
            preview["run_id"],
            "preview.deploying",
            {
                "preview_id": preview_id,
                "source_digest": approved_digest,
                "source_snapshot_digest": snapshot_digest,
            },
        )
        result: PreviewResult | None = None
        try:
            with TemporaryDirectory(prefix="rich-preview-source-") as temporary:
                source = extract_deployment_snapshot(
                    snapshot_content,
                    Path(temporary) / "source",
                )
                request = _preview_request(
                    preview["request"],
                    source_dir=source,
                )
                result = orchestrator.create(request)
            persisted = self.store.set_preview_status(
                preview_id,
                "ready",
                expected_status="deploying",
                result=asdict(result),
                error=None,
            )
        except Exception as exc:
            if result is not None:
                try:
                    orchestrator.destroy(
                        _preview_teardown(preview["request"]), result
                    )
                except Exception:
                    pass
            try:
                self.store.set_preview_status(
                    preview_id,
                    "failed",
                    expected_status="deploying",
                    error={"type": type(exc).__name__},
                )
                self.store.append_event(
                    preview["run_id"],
                    "preview.failed",
                    {"preview_id": preview_id, "error_type": type(exc).__name__},
                )
            except Exception:
                pass
            raise
        self.store.append_event(
            preview["run_id"],
            "preview.ready",
            {
                "preview_id": preview_id,
                "deployment_id": result.deployment_id,
                "preview_url": result.preview_url,
                "database_branch_id": result.database_branch_id,
                "expires_at": result.expires_at,
            },
        )
        return PreviewDeploymentResult(persisted, result)

    def destroy_preview(self, *, preview_id: str) -> dict[str, Any]:
        orchestrator = self._preview_orchestrator()
        preview = self.store.get_preview(preview_id)
        if preview["status"] not in {"ready", "destroy_failed"}:
            raise ValueError(
                f"preview must be ready to destroy, got {preview['status']!r}"
            )
        if not isinstance(preview["result"], Mapping):
            raise ValueError("preview has no durable provider result")
        result = PreviewResult(**dict(preview["result"]))
        previous_status = preview["status"]
        self.store.set_preview_status(
            preview_id,
            "destroying",
            expected_status=previous_status,
            error=None,
        )
        try:
            orchestrator.destroy(
                _preview_teardown(preview["request"]),
                result,
            )
        except Exception as exc:
            self.store.set_preview_status(
                preview_id,
                "destroy_failed",
                expected_status="destroying",
                error={"type": type(exc).__name__},
            )
            self.store.append_event(
                preview["run_id"],
                "preview.destroy_failed",
                {"preview_id": preview_id, "error_type": type(exc).__name__},
            )
            raise
        destroyed = self.store.set_preview_status(
            preview_id,
            "destroyed",
            expected_status="destroying",
            error=None,
            destroyed=True,
        )
        self.store.append_event(
            preview["run_id"],
            "preview.destroyed",
            {
                "preview_id": preview_id,
                "deployment_id": result.deployment_id,
                "database_branch_id": result.database_branch_id,
            },
        )
        return destroyed

    def _preview_orchestrator(self) -> PreviewOrchestrator:
        if self.preview_orchestrator is None:
            raise RuntimeError("preview providers are not configured")
        return self.preview_orchestrator

    def _require_approval(
        self,
        approval_id: str,
        *,
        gate: str,
        project_id: str | None = None,
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        approval = self.store.get_approval(approval_id)
        failures = []
        if approval["status"] != "approved":
            failures.append(f"status is {approval['status']!r}")
        if approval["gate"] != gate:
            failures.append(f"gate is {approval['gate']!r}, not {gate!r}")
        if project_id is not None and approval["project_id"] != project_id:
            failures.append("approval belongs to a different project")
        if (
            revision_id is not None
            and approval["request"].get("revision_id") != revision_id
        ):
            failures.append("approval belongs to a different revision")
        if failures:
            raise ApprovalRequired(
                f"approval {approval_id!r} is not valid: {'; '.join(failures)}"
            )
        return approval


def _dns_name(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not normalized:
        normalized = re.sub(r"[^a-z0-9]+", "-", fallback.lower()).strip("-")
    normalized = normalized[:63].rstrip("-")
    if not normalized:
        raise ValueError("project name cannot be converted to a target-pack name")
    return normalized


def _preview_branch_name(run_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "-", run_id).strip("-")
    return f"preview/{normalized[-54:]}"


def _preview_request(
    document: Mapping[str, Any],
    *,
    source_dir: Path | None = None,
) -> PreviewRequest:
    return PreviewRequest(
        run_id=str(document["run_id"]),
        source_dir=source_dir or Path(str(document["source_dir"])),
        project_name=str(document["project_name"]),
        neon_project_id=str(document["neon_project_id"]),
        neon_branch_name=str(document["neon_branch_name"]),
        expires_at=datetime.fromisoformat(str(document["expires_at"])),
        neon_token_handle=str(document["neon_token_handle"]),
        vercel_token_handle=str(document["vercel_token_handle"]),
        neon_parent_branch_id=_optional_string(
            document.get("neon_parent_branch_id")
        ),
        vercel_project_id=_optional_string(document.get("vercel_project_id")),
        vercel_team_id=_optional_string(document.get("vercel_team_id")),
    )


def _preview_teardown(document: Mapping[str, Any]) -> PreviewTeardown:
    return PreviewTeardown(
        run_id=str(document["run_id"]),
        neon_token_handle=str(document["neon_token_handle"]),
        vercel_token_handle=str(document["vercel_token_handle"]),
        vercel_team_id=_optional_string(document.get("vercel_team_id")),
    )


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None
