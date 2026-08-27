"""Durable execution state: tasks, evidence, approvals, runs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

from ._common import (
    ApprovalGate,
    ApprovalStatus,
    ArtifactKind,
    ArtifactStatus,
    EnumT,
    EvidenceKind,
    EvidenceStatus,
    ModelValidationError,
    RunStatus,
    SCHEMA_VERSION,
    TaskKind,
    TaskStatus,
    _SHA256_RE,
    _check_schema_version,
    _enum,
    _json_mapping,
    _non_negative_int,
    _positive_revision,
    _serialized,
    _stable_id,
    _strict_fields,
    _strings,
    _text,
    _unique_by_id,
)
from .spec import (
    ProjectSpec,
)
from .architecture import (
    ArchitectureSpec,
    _ARTIFACT_TRANSITIONS,
    _EVIDENCE_TRANSITIONS,
    _RUN_TRANSITIONS,
    _TASK_TRANSITIONS,
)



def _transition(
    current: EnumT,
    target: Any,
    enum_type: type[EnumT],
    transitions: Mapping[EnumT, frozenset[EnumT]],
    label: str,
) -> EnumT:
    target_status = _enum(target, enum_type, label)
    if target_status == current:
        return target_status
    if target_status not in transitions[current]:
        raise ModelValidationError(
            f"invalid {label} transition: {current.value} -> {target_status.value}"
        )
    return target_status


@dataclass(frozen=True, slots=True)
class BuildTask:
    id: str
    run_id: str
    node_id: str
    kind: TaskKind
    status: TaskStatus = TaskStatus.PENDING
    dependency_task_ids: tuple[str, ...] = ()
    attempt: int = 0
    cache_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "task.id"))
        object.__setattr__(self, "run_id", _stable_id(self.run_id, "task.run_id"))
        object.__setattr__(self, "node_id", _stable_id(self.node_id, "task.node_id"))
        object.__setattr__(self, "kind", _enum(self.kind, TaskKind, "task.kind"))
        object.__setattr__(
            self, "status", _enum(self.status, TaskStatus, "task.status")
        )
        object.__setattr__(
            self,
            "dependency_task_ids",
            _strings(
                self.dependency_task_ids,
                "task.dependency_task_ids",
                stable_ids=True,
            ),
        )
        if self.id in self.dependency_task_ids:
            raise ModelValidationError(f"task {self.id!r} cannot depend on itself")
        object.__setattr__(
            self, "attempt", _non_negative_int(self.attempt, "task.attempt")
        )
        if self.cache_key is not None:
            if not isinstance(self.cache_key, str) or not _SHA256_RE.fullmatch(
                self.cache_key
            ):
                raise ModelValidationError(
                    "task.cache_key must be a lowercase SHA-256 digest"
                )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "task.metadata")
        )

    def transitioned(self, status: TaskStatus | str) -> "BuildTask":
        target = _transition(
            self.status, status, TaskStatus, _TASK_TRANSITIONS, "task status"
        )
        attempt = self.attempt
        if target is TaskStatus.RUNNING and self.status is not TaskStatus.RUNNING:
            attempt += 1
        return replace(self, status=target, attempt=attempt)

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BuildTask":
        doc = _strict_fields(
            data,
            label="BuildTask",
            required={
                "schema_version",
                "id",
                "run_id",
                "node_id",
                "kind",
                "status",
            },
            optional={
                "dependency_task_ids",
                "attempt",
                "cache_key",
                "metadata",
            },
        )
        _check_schema_version(doc, "BuildTask")
        return cls(
            id=doc["id"],
            run_id=doc["run_id"],
            node_id=doc["node_id"],
            kind=doc["kind"],
            status=doc["status"],
            dependency_task_ids=doc.get("dependency_task_ids", ()),
            attempt=doc.get("attempt", 0),
            cache_key=doc.get("cache_key"),
            metadata=doc.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    run_id: str
    kind: EvidenceKind
    status: EvidenceStatus = EvidenceStatus.PENDING
    blocking: bool = True
    task_id: str | None = None
    node_id: str | None = None
    requirement_ids: tuple[str, ...] = ()
    acceptance_scenario_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "evidence.id"))
        object.__setattr__(
            self, "run_id", _stable_id(self.run_id, "evidence.run_id")
        )
        object.__setattr__(
            self, "kind", _enum(self.kind, EvidenceKind, "evidence.kind")
        )
        object.__setattr__(
            self,
            "status",
            _enum(self.status, EvidenceStatus, "evidence.status"),
        )
        if not isinstance(self.blocking, bool):
            raise ModelValidationError("evidence.blocking must be a boolean")
        for field_name in ("task_id", "node_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _stable_id(value, f"evidence.{field_name}"),
                )
        object.__setattr__(
            self,
            "requirement_ids",
            _strings(
                self.requirement_ids,
                "evidence.requirement_ids",
                stable_ids=True,
            ),
        )
        object.__setattr__(
            self,
            "acceptance_scenario_ids",
            _strings(
                self.acceptance_scenario_ids,
                "evidence.acceptance_scenario_ids",
                stable_ids=True,
            ),
        )
        object.__setattr__(
            self,
            "artifact_ids",
            _strings(
                self.artifact_ids, "evidence.artifact_ids", stable_ids=True
            ),
        )
        if (
            self.kind is EvidenceKind.ACCEPTANCE
            and not self.requirement_ids
        ):
            raise ModelValidationError(
                "acceptance evidence must trace requirements"
            )
        if (
            self.kind is EvidenceKind.ACCEPTANCE
            and self.status is EvidenceStatus.PASSED
            and not self.acceptance_scenario_ids
        ):
            raise ModelValidationError(
                "passed acceptance evidence must trace acceptance scenarios"
            )
        if self.status is EvidenceStatus.PASSED and not self.artifact_ids:
            raise ModelValidationError(
                "passed evidence requires at least one immutable result artifact"
            )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "evidence.metadata")
        )

    @property
    def satisfies_gate(self) -> bool:
        return self.status is EvidenceStatus.PASSED

    def transitioned(self, status: EvidenceStatus | str) -> "Evidence":
        target = _transition(
            self.status,
            status,
            EvidenceStatus,
            _EVIDENCE_TRANSITIONS,
            "evidence status",
        )
        return replace(self, status=target)

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Evidence":
        doc = _strict_fields(
            data,
            label="Evidence",
            required={
                "schema_version",
                "id",
                "run_id",
                "kind",
                "status",
            },
            optional={
                "blocking",
                "task_id",
                "node_id",
                "requirement_ids",
                "acceptance_scenario_ids",
                "artifact_ids",
                "metadata",
            },
        )
        _check_schema_version(doc, "Evidence")
        return cls(
            id=doc["id"],
            run_id=doc["run_id"],
            kind=doc["kind"],
            status=doc["status"],
            blocking=doc.get("blocking", True),
            task_id=doc.get("task_id"),
            node_id=doc.get("node_id"),
            requirement_ids=doc.get("requirement_ids", ()),
            acceptance_scenario_ids=doc.get("acceptance_scenario_ids", ()),
            artifact_ids=doc.get("artifact_ids", ()),
            metadata=doc.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class Approval:
    id: str
    project_id: str
    gate: ApprovalGate
    revision_id: str
    status: ApprovalStatus = ApprovalStatus.REQUESTED
    run_id: str | None = None
    requested_capabilities: tuple[str, ...] = ()
    decided_by: str | None = None
    decision_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "approval.id"))
        object.__setattr__(
            self,
            "project_id",
            _stable_id(self.project_id, "approval.project_id"),
        )
        object.__setattr__(
            self, "gate", _enum(self.gate, ApprovalGate, "approval.gate")
        )
        object.__setattr__(
            self,
            "revision_id",
            _stable_id(self.revision_id, "approval.revision_id"),
        )
        object.__setattr__(
            self,
            "status",
            _enum(self.status, ApprovalStatus, "approval.status"),
        )
        if self.run_id is not None:
            object.__setattr__(
                self, "run_id", _stable_id(self.run_id, "approval.run_id")
            )
        object.__setattr__(
            self,
            "requested_capabilities",
            _strings(
                self.requested_capabilities,
                "approval.requested_capabilities",
                stable_ids=True,
            ),
        )
        if self.decided_by is not None:
            object.__setattr__(
                self,
                "decided_by",
                _text(self.decided_by, "approval.decided_by"),
            )
        if self.decision_reason is not None:
            object.__setattr__(
                self,
                "decision_reason",
                _text(self.decision_reason, "approval.decision_reason"),
            )
        if self.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            if self.decided_by is None:
                raise ModelValidationError(
                    "approved or rejected approvals require decided_by"
                )
        elif self.status is ApprovalStatus.REQUESTED and (
            self.decided_by is not None or self.decision_reason is not None
        ):
            raise ModelValidationError(
                "a requested approval cannot contain a decision"
            )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "approval.metadata")
        )

    def decided(
        self,
        status: ApprovalStatus | str,
        *,
        decided_by: str,
        reason: str | None = None,
    ) -> "Approval":
        target = _enum(status, ApprovalStatus, "approval status")
        if self.status is not ApprovalStatus.REQUESTED:
            raise ModelValidationError(
                f"approval {self.id!r} has already been decided"
            )
        if target not in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.CANCELED,
        }:
            raise ModelValidationError(
                "approval decision must be approved, rejected, or canceled"
            )
        return replace(
            self,
            status=target,
            decided_by=decided_by,
            decision_reason=reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Approval":
        doc = _strict_fields(
            data,
            label="Approval",
            required={
                "schema_version",
                "id",
                "project_id",
                "gate",
                "revision_id",
                "status",
            },
            optional={
                "run_id",
                "requested_capabilities",
                "decided_by",
                "decision_reason",
                "metadata",
            },
        )
        _check_schema_version(doc, "Approval")
        return cls(
            id=doc["id"],
            project_id=doc["project_id"],
            gate=doc["gate"],
            revision_id=doc["revision_id"],
            status=doc["status"],
            run_id=doc.get("run_id"),
            requested_capabilities=doc.get("requested_capabilities", ()),
            decided_by=doc.get("decided_by"),
            decision_reason=doc.get("decision_reason"),
            metadata=doc.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    run_id: str
    kind: ArtifactKind
    status: ArtifactStatus = ArtifactStatus.PENDING
    digest: str | None = None
    uri: str | None = None
    media_type: str = "application/octet-stream"
    produced_by_task_id: str | None = None
    requirement_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "artifact.id"))
        object.__setattr__(
            self, "run_id", _stable_id(self.run_id, "artifact.run_id")
        )
        object.__setattr__(
            self, "kind", _enum(self.kind, ArtifactKind, "artifact.kind")
        )
        object.__setattr__(
            self,
            "status",
            _enum(self.status, ArtifactStatus, "artifact.status"),
        )
        if self.digest is not None:
            if not isinstance(self.digest, str) or not _SHA256_RE.fullmatch(
                self.digest
            ):
                raise ModelValidationError(
                    "artifact.digest must be a lowercase SHA-256 digest"
                )
        if self.uri is not None:
            object.__setattr__(self, "uri", _text(self.uri, "artifact.uri"))
        object.__setattr__(
            self, "media_type", _text(self.media_type, "artifact.media_type")
        )
        if self.produced_by_task_id is not None:
            object.__setattr__(
                self,
                "produced_by_task_id",
                _stable_id(
                    self.produced_by_task_id, "artifact.produced_by_task_id"
                ),
            )
        object.__setattr__(
            self,
            "requirement_ids",
            _strings(
                self.requirement_ids,
                "artifact.requirement_ids",
                stable_ids=True,
            ),
        )
        if self.status in {
            ArtifactStatus.AVAILABLE,
            ArtifactStatus.QUARANTINED,
        } and (self.digest is None or self.uri is None):
            raise ModelValidationError(
                f"{self.status.value} artifacts require digest and uri"
            )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "artifact.metadata")
        )

    def transitioned(self, status: ArtifactStatus | str) -> "Artifact":
        target = _transition(
            self.status,
            status,
            ArtifactStatus,
            _ARTIFACT_TRANSITIONS,
            "artifact status",
        )
        return replace(self, status=target)

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Artifact":
        doc = _strict_fields(
            data,
            label="Artifact",
            required={
                "schema_version",
                "id",
                "run_id",
                "kind",
                "status",
            },
            optional={
                "digest",
                "uri",
                "media_type",
                "produced_by_task_id",
                "requirement_ids",
                "metadata",
            },
        )
        _check_schema_version(doc, "Artifact")
        return cls(
            id=doc["id"],
            run_id=doc["run_id"],
            kind=doc["kind"],
            status=doc["status"],
            digest=doc.get("digest"),
            uri=doc.get("uri"),
            media_type=doc.get("media_type", "application/octet-stream"),
            produced_by_task_id=doc.get("produced_by_task_id"),
            requirement_ids=doc.get("requirement_ids", ()),
            metadata=doc.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class BuildRun:
    id: str
    project_id: str
    spec_revision_id: str
    architecture_revision_id: str
    status: RunStatus = RunStatus.DRAFT
    task_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    revision: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "run.id"))
        object.__setattr__(
            self, "project_id", _stable_id(self.project_id, "run.project_id")
        )
        object.__setattr__(
            self,
            "spec_revision_id",
            _stable_id(self.spec_revision_id, "run.spec_revision_id"),
        )
        object.__setattr__(
            self,
            "architecture_revision_id",
            _stable_id(
                self.architecture_revision_id, "run.architecture_revision_id"
            ),
        )
        object.__setattr__(self, "status", _enum(self.status, RunStatus, "run.status"))
        for field_name in ("task_ids", "evidence_ids", "artifact_ids"):
            object.__setattr__(
                self,
                field_name,
                _strings(
                    getattr(self, field_name),
                    f"run.{field_name}",
                    stable_ids=True,
                ),
            )
        object.__setattr__(
            self, "revision", _positive_revision(self.revision, "run.revision")
        )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "run.metadata")
        )

    def transitioned(self, status: RunStatus | str) -> "BuildRun":
        target = _transition(
            self.status, status, RunStatus, _RUN_TRANSITIONS, "run status"
        )
        return replace(self, status=target, revision=self.revision + 1)

    def validate_records(
        self,
        *,
        tasks: Iterable[BuildTask],
        evidence: Iterable[Evidence],
        artifacts: Iterable[Artifact],
        required_requirement_ids: Iterable[str] = (),
        required_acceptance_scenario_ids: Iterable[str] = (),
    ) -> None:
        task_index = _unique_by_id(tasks, "run tasks")
        evidence_index = _unique_by_id(evidence, "run evidence")
        artifact_index = _unique_by_id(artifacts, "run artifacts")
        declared = (
            ("task", set(self.task_ids), set(task_index)),
            ("evidence", set(self.evidence_ids), set(evidence_index)),
            ("artifact", set(self.artifact_ids), set(artifact_index)),
        )
        for label, declared_ids, actual_ids in declared:
            if declared_ids != actual_ids:
                raise ModelValidationError(
                    f"run {label} ids do not match records; "
                    f"missing={sorted(declared_ids - actual_ids)}, "
                    f"undeclared={sorted(actual_ids - declared_ids)}"
                )

        for task in task_index.values():
            if task.run_id != self.id:
                raise ModelValidationError(
                    f"task {task.id!r} belongs to run {task.run_id!r}, not {self.id!r}"
                )
            unknown_dependencies = set(task.dependency_task_ids) - task_index.keys()
            if unknown_dependencies:
                raise ModelValidationError(
                    f"task {task.id!r} has unknown dependencies: "
                    f"{sorted(unknown_dependencies)}"
                )

        visited: set[str] = set()
        visiting: set[str] = set()

        def visit_task(task_id: str) -> None:
            if task_id in visiting:
                raise ModelValidationError(
                    f"build task dependencies form a cycle through {task_id!r}"
                )
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency_id in task_index[task_id].dependency_task_ids:
                visit_task(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_index:
            visit_task(task_id)

        for artifact in artifact_index.values():
            if artifact.run_id != self.id:
                raise ModelValidationError(
                    f"artifact {artifact.id!r} belongs to another run"
                )
            if (
                artifact.produced_by_task_id is not None
                and artifact.produced_by_task_id not in task_index
            ):
                raise ModelValidationError(
                    f"artifact {artifact.id!r} references unknown producing task "
                    f"{artifact.produced_by_task_id!r}"
                )

        for record in evidence_index.values():
            if record.run_id != self.id:
                raise ModelValidationError(
                    f"evidence {record.id!r} belongs to another run"
                )
            if record.task_id is not None and record.task_id not in task_index:
                raise ModelValidationError(
                    f"evidence {record.id!r} references unknown task "
                    f"{record.task_id!r}"
                )
            unknown_artifacts = set(record.artifact_ids) - artifact_index.keys()
            if unknown_artifacts:
                raise ModelValidationError(
                    f"evidence {record.id!r} references unknown artifacts: "
                    f"{sorted(unknown_artifacts)}"
                )

        required_requirements = set(
            _strings(
                required_requirement_ids,
                "required_requirement_ids",
                stable_ids=True,
            )
        )
        required_scenarios = set(
            _strings(
                required_acceptance_scenario_ids,
                "required_acceptance_scenario_ids",
                stable_ids=True,
            )
        )
        passed_acceptance = [
            record
            for record in evidence_index.values()
            if record.kind is EvidenceKind.ACCEPTANCE and record.satisfies_gate
        ]
        covered_requirements = {
            requirement_id
            for record in passed_acceptance
            for requirement_id in record.requirement_ids
        }
        covered_scenarios = {
            scenario_id
            for record in passed_acceptance
            for scenario_id in record.acceptance_scenario_ids
        }
        if required_requirements - covered_requirements:
            raise ModelValidationError(
                "passed acceptance evidence does not cover requirements: "
                f"{sorted(required_requirements - covered_requirements)}"
            )
        if required_scenarios - covered_scenarios:
            raise ModelValidationError(
                "passed acceptance evidence does not cover scenarios: "
                f"{sorted(required_scenarios - covered_scenarios)}"
            )

        if self.status is RunStatus.SUCCEEDED:
            unfinished = {
                task.id: task.status.value
                for task in task_index.values()
                if task.status not in {TaskStatus.SUCCEEDED, TaskStatus.CACHED}
            }
            if unfinished:
                raise ModelValidationError(
                    f"succeeded run has unfinished tasks: {unfinished}"
                )
            unsatisfied = {
                record.id: record.status.value
                for record in evidence_index.values()
                if record.blocking and not record.satisfies_gate
            }
            if unsatisfied:
                raise ModelValidationError(
                    f"succeeded run has unsatisfied blocking evidence: {unsatisfied}"
                )
            if not any(record.blocking for record in evidence_index.values()):
                raise ModelValidationError(
                    "succeeded run requires at least one blocking evidence record"
                )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BuildRun":
        doc = _strict_fields(
            data,
            label="BuildRun",
            required={
                "schema_version",
                "id",
                "project_id",
                "spec_revision_id",
                "architecture_revision_id",
                "status",
            },
            optional={
                "task_ids",
                "evidence_ids",
                "artifact_ids",
                "revision",
                "metadata",
            },
        )
        _check_schema_version(doc, "BuildRun")
        return cls(
            id=doc["id"],
            project_id=doc["project_id"],
            spec_revision_id=doc["spec_revision_id"],
            architecture_revision_id=doc["architecture_revision_id"],
            status=doc["status"],
            task_ids=doc.get("task_ids", ()),
            evidence_ids=doc.get("evidence_ids", ()),
            artifact_ids=doc.get("artifact_ids", ()),
            revision=doc.get("revision", 1),
            metadata=doc.get("metadata", {}),
        )


def validate_release_traceability(
    *,
    project: ProjectSpec,
    architecture: ArchitectureSpec,
    run: BuildRun,
    tasks: Iterable[BuildTask],
    evidence: Iterable[Evidence],
    artifacts: Iterable[Artifact],
) -> None:
    """Validate the complete requirement-to-release proof chain."""

    if run.status is not RunStatus.SUCCEEDED:
        raise ModelValidationError("only a succeeded run can be release-ready")
    if run.project_id != project.id:
        raise ModelValidationError(
            f"run project {run.project_id!r} does not match {project.id!r}"
        )
    architecture.validate_against_project(project)

    evidence_records = tuple(evidence)
    artifact_records = tuple(artifacts)
    known_requirements = set(project.requirement_index)
    known_scenarios = set(project.scenario_index)
    for record in evidence_records:
        unknown_requirements = set(record.requirement_ids) - known_requirements
        unknown_scenarios = set(record.acceptance_scenario_ids) - known_scenarios
        if unknown_requirements:
            raise ModelValidationError(
                f"evidence {record.id!r} traces unknown requirements: "
                f"{sorted(unknown_requirements)}"
            )
        if unknown_scenarios:
            raise ModelValidationError(
                f"evidence {record.id!r} traces unknown scenarios: "
                f"{sorted(unknown_scenarios)}"
            )
        for scenario_id in record.acceptance_scenario_ids:
            scenario_requirements = set(
                project.scenario_index[scenario_id].requirement_ids
            )
            if not scenario_requirements <= set(record.requirement_ids):
                raise ModelValidationError(
                    f"evidence {record.id!r} does not trace every requirement "
                    f"of scenario {scenario_id!r}"
                )

    run.validate_records(
        tasks=tasks,
        evidence=evidence_records,
        artifacts=artifact_records,
        required_requirement_ids=known_requirements,
        required_acceptance_scenario_ids=known_scenarios,
    )
    release_artifacts = [
        artifact
        for artifact in artifact_records
        if artifact.status is ArtifactStatus.AVAILABLE
        and artifact.kind in {ArtifactKind.SOURCE, ArtifactKind.BUNDLE}
    ]
    if not release_artifacts:
        raise ModelValidationError(
            "release-ready run needs an available source or bundle artifact"
        )
