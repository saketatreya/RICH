"""Fail-honest import of RICH v1 canvas documents into the v2 durable store."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .store import Artifact, Revision, RichStore


class MigrationError(ValueError):
    """A legacy document cannot be imported without losing structural truth."""


@dataclass(frozen=True, slots=True)
class MigrationIssue:
    code: str
    message: str
    node_id: str | None = None
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class V1MigrationDraft:
    project_id: str
    project_name: str
    goal: str
    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]
    source_digest: str
    issues: tuple[MigrationIssue, ...]

    @property
    def ready_for_spec_approval(self) -> bool:
        return not any(issue.blocking for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_schema_version": "1",
            "project_id": self.project_id,
            "project_name": self.project_name,
            "goal": self.goal,
            "nodes": list(self.nodes),
            "edges": list(self.edges),
            "source_digest": self.source_digest,
            "ready_for_spec_approval": self.ready_for_spec_approval,
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "node_id": issue.node_id,
                    "blocking": issue.blocking,
                }
                for issue in self.issues
            ],
        }


@dataclass(frozen=True, slots=True)
class PersistedMigration:
    project: dict[str, Any]
    revision: Revision
    source_artifact: Artifact
    draft: V1MigrationDraft


def inspect_v1_canvas(document: Mapping[str, Any]) -> V1MigrationDraft:
    if not isinstance(document, Mapping):
        raise MigrationError("v1 canvas document must be an object")
    tree = document.get("tree")
    if not isinstance(tree, Mapping):
        raise MigrationError("v1 canvas document has no root tree")
    root_id = _legacy_id(tree.get("id"), "root")
    goal = _required_text(tree.get("description"), "root description")
    canonical_source = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    issues: list[MigrationIssue] = [
        MigrationIssue(
            "audience_missing",
            "v1 did not record product audiences; complete the adaptive interview.",
        ),
        MigrationIssue(
            "acceptance_missing",
            "v1 tests are implementation-derived and cannot be promoted to approved "
            "root acceptance scenarios.",
        ),
    ]
    seen: set[str] = set()

    def visit(raw: Mapping[str, Any], parent_id: str | None) -> None:
        node_id = _legacy_id(raw.get("id"), "node")
        if node_id in seen:
            raise MigrationError(f"duplicate v1 node id {node_id!r}")
        seen.add(node_id)
        operations = raw.get("operations") or []
        if not isinstance(operations, list):
            raise MigrationError(f"node {node_id!r} operations must be a list")
        children = raw.get("children") or []
        if not isinstance(children, list) or any(
            not isinstance(child, Mapping) for child in children
        ):
            raise MigrationError(f"node {node_id!r} children must be objects")
        nodes.append(
            {
                "id": node_id,
                "description": str(raw.get("description") or "").strip(),
                "kind": raw.get("kind") or "pure",
                "is_leaf": raw.get("is_leaf"),
                "stateful": bool(raw.get("stateful", False)),
                "operations": operations,
                "dependencies": raw.get("dependencies") or [],
                "external": raw.get("external") or None,
                # A v1 status is provenance only. It is never v2 evidence.
                "legacy_status": raw.get("status") or "unknown",
                "parent_id": parent_id,
            }
        )
        if raw.get("status") == "verified":
            issues.append(
                MigrationIssue(
                    "legacy_verification_not_trusted",
                    "The legacy verified label lacks the v2 evidence chain and was "
                    "imported as provenance only.",
                    node_id=node_id,
                )
            )
        if not operations:
            issues.append(
                MigrationIssue(
                    "operation_missing",
                    "Define a typed operation or invariant before compiling this node.",
                    node_id=node_id,
                )
            )
        if raw.get("kind") == "adapter" and not (raw.get("external") or {}).get(
            "provider"
        ):
            issues.append(
                MigrationIssue(
                    "adapter_provider_missing",
                    "Name the external provider and its outage policy.",
                    node_id=node_id,
                )
            )

        if parent_id is not None:
            edges.append(
                {
                    "id": f"contains:{parent_id}:{node_id}",
                    "kind": "contains",
                    "source_node_id": parent_id,
                    "target_node_id": node_id,
                }
            )
        child_ids = {_legacy_id(child.get("id"), "child") for child in children}
        for edge_index, edge in enumerate(raw.get("edges") or []):
            if not isinstance(edge, Mapping):
                raise MigrationError(f"node {node_id!r} contains a non-object edge")
            source = _legacy_id(edge.get("from"), "edge source")
            target = _legacy_id(edge.get("to"), "edge target")
            if source not in child_ids or target not in child_ids:
                issues.append(
                    MigrationIssue(
                        "edge_reference_unresolved",
                        f"Legacy edge {source!r} → {target!r} is not between direct children.",
                        node_id=node_id,
                    )
                )
            edges.append(
                {
                    "id": f"legacy:{node_id}:{edge_index}:{source}:{target}",
                    "kind": "legacy_untyped",
                    "source_node_id": source,
                    "target_node_id": target,
                    "legacy_name": edge.get("name"),
                }
            )
        for child in children:
            visit(child, node_id)

    visit(tree, None)
    project_id = f"project.{root_id}"
    return V1MigrationDraft(
        project_id=project_id,
        project_name=goal[:80],
        goal=goal,
        nodes=tuple(nodes),
        edges=tuple(edges),
        source_digest=hashlib.sha256(canonical_source).hexdigest(),
        issues=tuple(issues),
    )


def import_v1_canvas(
    store: RichStore,
    source: str | Path | Mapping[str, Any],
    *,
    project_name: str | None = None,
) -> PersistedMigration:
    if isinstance(source, Mapping):
        document = dict(source)
        source_bytes = json.dumps(
            document, sort_keys=True, indent=2, ensure_ascii=False
        ).encode("utf-8")
    else:
        path = Path(source)
        try:
            source_bytes = path.read_bytes()
            document = json.loads(source_bytes)
        except (OSError, json.JSONDecodeError) as exc:
            raise MigrationError(f"could not read v1 canvas document: {exc}") from exc

    draft = inspect_v1_canvas(document)
    project_id = draft.project_id
    suffix = 2
    while True:
        try:
            store.get_project(project_id)
        except Exception as exc:
            # Only a missing project permits reuse of the candidate id.
            from .store import NotFoundError

            if isinstance(exc, NotFoundError):
                break
            raise
        project_id = f"{draft.project_id}.{suffix}"
        suffix += 1
    if project_id != draft.project_id:
        draft = V1MigrationDraft(
            project_id=project_id,
            project_name=draft.project_name,
            goal=draft.goal,
            nodes=draft.nodes,
            edges=draft.edges,
            source_digest=draft.source_digest,
            issues=draft.issues,
        )

    project = store.create_project(
        project_name or draft.project_name, project_id=draft.project_id
    )
    source_artifact = store.put_artifact(
        source_bytes,
        media_type="application/vnd.rich.canvas-v1+json",
        metadata={"role": "migration_source", "source_digest": draft.source_digest},
    )
    revision = store.save_revision(
        project["id"],
        kind="v1_import_draft",
        schema_version="1-to-2-draft",
        document={
            **draft.to_dict(),
            "source_artifact_digest": source_artifact.digest,
        },
        expected_revision=0,
    )
    return PersistedMigration(
        store.get_project(project["id"]), revision, source_artifact, draft
    )


def _legacy_id(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise MigrationError(f"{label} id must be a string")
    normalized = re.sub(r"[^a-zA-Z0-9._:/-]+", "_", value.strip()).strip("_")
    if not normalized:
        raise MigrationError(f"{label} id cannot be empty")
    return normalized


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MigrationError(f"{label} cannot be empty")
    return value.strip()
