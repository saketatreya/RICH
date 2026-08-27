"""Nodes, edges, ports, and the architecture graph."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ._common import (
    ArtifactStatus,
    EdgeKind,
    EvidenceStatus,
    ModelValidationError,
    NodeKind,
    PortDirection,
    RunStatus,
    SCHEMA_VERSION,
    TaskStatus,
    _check_schema_version,
    _enum,
    _json_mapping,
    _models,
    _positive_revision,
    _relative_owned_path,
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
from .contracts import (
    Contract,
)



@dataclass(frozen=True, slots=True)
class PortSpec:
    id: str
    name: str
    direction: PortDirection
    schema: dict[str, Any]
    operation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "port.id"))
        object.__setattr__(self, "name", _text(self.name, "port.name"))
        object.__setattr__(
            self, "direction", _enum(self.direction, PortDirection, "port.direction")
        )
        object.__setattr__(self, "schema", _json_mapping(self.schema, "port.schema"))
        if self.operation_id is not None:
            object.__setattr__(
                self,
                "operation_id",
                _stable_id(self.operation_id, "port.operation_id"),
            )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PortSpec":
        doc = _strict_fields(
            data,
            label="PortSpec",
            required={"id", "name", "direction", "schema"},
            optional={"operation_id"},
        )
        return cls(
            id=doc["id"],
            name=doc["name"],
            direction=doc["direction"],
            schema=doc["schema"],
            operation_id=doc.get("operation_id"),
        )


@dataclass(frozen=True, slots=True)
class ArchitectureNode:
    id: str
    name: str
    kind: NodeKind
    contract_id: str | None
    ports: tuple[PortSpec, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    owned_paths: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "node.id"))
        object.__setattr__(self, "name", _text(self.name, "node.name"))
        object.__setattr__(self, "kind", _enum(self.kind, NodeKind, "node.kind"))
        if self.contract_id is not None:
            object.__setattr__(
                self,
                "contract_id",
                _stable_id(self.contract_id, "node.contract_id"),
            )
        object.__setattr__(
            self, "ports", _models(self.ports, PortSpec, "node.ports")
        )
        _unique_by_id(self.ports, "node.ports")
        object.__setattr__(
            self,
            "requirement_ids",
            _strings(
                self.requirement_ids,
                "node.requirement_ids",
                stable_ids=True,
            ),
        )
        if isinstance(self.owned_paths, (str, bytes)) or not isinstance(
            self.owned_paths, Iterable
        ):
            raise ModelValidationError("node.owned_paths must be a sequence")
        owned_paths = tuple(
            _relative_owned_path(path, f"node.owned_paths[{index}]")
            for index, path in enumerate(self.owned_paths)
        )
        if len(set(owned_paths)) != len(owned_paths):
            raise ModelValidationError("node.owned_paths cannot contain duplicates")
        object.__setattr__(self, "owned_paths", owned_paths)
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "node.metadata")
        )

    @property
    def port_index(self) -> dict[str, PortSpec]:
        return {port.id: port for port in self.ports}

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArchitectureNode":
        doc = _strict_fields(
            data,
            label="ArchitectureNode",
            required={"id", "name", "kind", "contract_id"},
            optional={"ports", "requirement_ids", "owned_paths", "metadata"},
        )
        return cls(
            id=doc["id"],
            name=doc["name"],
            kind=doc["kind"],
            contract_id=doc["contract_id"],
            ports=doc.get("ports", ()),
            requirement_ids=doc.get("requirement_ids", ()),
            owned_paths=doc.get("owned_paths", ()),
            metadata=doc.get("metadata", {}),
        )


_PORT_EDGE_KINDS = frozenset(
    {
        EdgeKind.CALL,
        EdgeKind.DATA,
        EdgeKind.CAPABILITY,
        EdgeKind.EVENT,
        EdgeKind.SCHEMA,
    }
)


@dataclass(frozen=True, slots=True)
class ArchitectureEdge:
    id: str
    kind: EdgeKind
    source_node_id: str
    target_node_id: str
    source_port_id: str | None = None
    target_port_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "edge.id"))
        object.__setattr__(self, "kind", _enum(self.kind, EdgeKind, "edge.kind"))
        object.__setattr__(
            self,
            "source_node_id",
            _stable_id(self.source_node_id, "edge.source_node_id"),
        )
        object.__setattr__(
            self,
            "target_node_id",
            _stable_id(self.target_node_id, "edge.target_node_id"),
        )
        if self.source_node_id == self.target_node_id:
            raise ModelValidationError(f"edge {self.id!r} cannot target its source")
        if self.source_port_id is not None:
            object.__setattr__(
                self,
                "source_port_id",
                _stable_id(self.source_port_id, "edge.source_port_id"),
            )
        if self.target_port_id is not None:
            object.__setattr__(
                self,
                "target_port_id",
                _stable_id(self.target_port_id, "edge.target_port_id"),
            )
        if self.kind in _PORT_EDGE_KINDS and (
            self.source_port_id is None or self.target_port_id is None
        ):
            raise ModelValidationError(
                f"{self.kind.value} edge {self.id!r} requires source and target ports"
            )
        if self.kind is EdgeKind.CONTAINS and (
            self.source_port_id is not None or self.target_port_id is not None
        ):
            raise ModelValidationError(
                f"contains edge {self.id!r} cannot carry ports"
            )
        if (self.source_port_id is None) != (self.target_port_id is None):
            raise ModelValidationError(
                f"edge {self.id!r} must define both ports or neither"
            )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "edge.metadata")
        )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArchitectureEdge":
        doc = _strict_fields(
            data,
            label="ArchitectureEdge",
            required={"id", "kind", "source_node_id", "target_node_id"},
            optional={"source_port_id", "target_port_id", "metadata"},
        )
        return cls(
            id=doc["id"],
            kind=doc["kind"],
            source_node_id=doc["source_node_id"],
            target_node_id=doc["target_node_id"],
            source_port_id=doc.get("source_port_id"),
            target_port_id=doc.get("target_port_id"),
            metadata=doc.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class ArchitectureSpec:
    id: str
    project_id: str
    root_node_id: str
    target_pack: str
    nodes: tuple[ArchitectureNode, ...]
    edges: tuple[ArchitectureEdge, ...]
    contracts: tuple[Contract, ...]
    project_spec_revision: int = 1
    revision: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "architecture.id"))
        object.__setattr__(
            self, "project_id", _stable_id(self.project_id, "architecture.project_id")
        )
        object.__setattr__(
            self,
            "root_node_id",
            _stable_id(self.root_node_id, "architecture.root_node_id"),
        )
        object.__setattr__(
            self, "target_pack", _stable_id(self.target_pack, "architecture.target_pack")
        )
        object.__setattr__(
            self,
            "nodes",
            _models(self.nodes, ArchitectureNode, "architecture.nodes"),
        )
        object.__setattr__(
            self,
            "edges",
            _models(self.edges, ArchitectureEdge, "architecture.edges"),
        )
        object.__setattr__(
            self,
            "contracts",
            _models(self.contracts, Contract, "architecture.contracts"),
        )
        object.__setattr__(
            self,
            "project_spec_revision",
            _positive_revision(
                self.project_spec_revision, "architecture.project_spec_revision"
            ),
        )
        object.__setattr__(
            self,
            "revision",
            _positive_revision(self.revision, "architecture.revision"),
        )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "architecture.metadata")
        )
        self.validate()

    @property
    def node_index(self) -> dict[str, ArchitectureNode]:
        return {node.id: node for node in self.nodes}

    @property
    def edge_index(self) -> dict[str, ArchitectureEdge]:
        return {edge.id: edge for edge in self.edges}

    @property
    def contract_index(self) -> dict[str, Contract]:
        return {contract.id: contract for contract in self.contracts}

    def validate(self) -> None:
        nodes = _unique_by_id(self.nodes, "architecture.nodes")
        edges = _unique_by_id(self.edges, "architecture.edges")
        contracts = _unique_by_id(self.contracts, "architecture.contracts")
        if not nodes:
            raise ModelValidationError("architecture.nodes cannot be empty")
        if self.root_node_id not in nodes:
            raise ModelValidationError(
                f"architecture root node {self.root_node_id!r} does not exist"
            )

        contract_owners: dict[str, str] = {}
        owned_paths: dict[str, str] = {}
        for node in nodes.values():
            for path in node.owned_paths:
                previous_owner = owned_paths.get(path)
                if previous_owner is not None:
                    raise ModelValidationError(
                        f"owned path {path!r} is assigned to both "
                        f"{previous_owner!r} and {node.id!r}"
                    )
                owned_paths[path] = node.id

            if node.contract_id is None:
                if node.kind is not NodeKind.RESOURCE:
                    raise ModelValidationError(
                        f"non-resource node {node.id!r} requires a contract"
                    )
                if node.requirement_ids:
                    raise ModelValidationError(
                        f"resource node {node.id!r} cannot own requirements "
                        "without a contract"
                    )
                continue

            contract = contracts.get(node.contract_id)
            if contract is None:
                raise ModelValidationError(
                    f"node {node.id!r} references unknown contract "
                    f"{node.contract_id!r}"
                )
            if contract.node_id != node.id:
                raise ModelValidationError(
                    f"contract {contract.id!r} belongs to {contract.node_id!r}, "
                    f"not node {node.id!r}"
                )
            if node.contract_id in contract_owners:
                raise ModelValidationError(
                    f"contract {node.contract_id!r} is assigned to multiple nodes"
                )
            contract_owners[node.contract_id] = node.id
            if set(node.requirement_ids) != set(contract.traced_requirement_ids):
                raise ModelValidationError(
                    f"node {node.id!r} requirement allocation must exactly match "
                    f"its contract traceability"
                )
            operation_ids = contract.operation_index.keys()
            for port in node.ports:
                if port.operation_id is not None and port.operation_id not in operation_ids:
                    raise ModelValidationError(
                        f"port {port.id!r} on node {node.id!r} references unknown "
                        f"operation {port.operation_id!r}"
                    )

        unassigned_contracts = contracts.keys() - contract_owners.keys()
        if unassigned_contracts:
            raise ModelValidationError(
                f"architecture has unassigned contracts: {sorted(unassigned_contracts)}"
            )

        incoming_contains: dict[str, int] = {node_id: 0 for node_id in nodes}
        containment_children: dict[str, list[str]] = {
            node_id: [] for node_id in nodes
        }
        for edge in edges.values():
            source = nodes.get(edge.source_node_id)
            target = nodes.get(edge.target_node_id)
            if source is None:
                raise ModelValidationError(
                    f"edge {edge.id!r} has unknown source {edge.source_node_id!r}"
                )
            if target is None:
                raise ModelValidationError(
                    f"edge {edge.id!r} has unknown target {edge.target_node_id!r}"
                )
            if edge.kind is EdgeKind.CONTAINS:
                incoming_contains[target.id] += 1
                containment_children[source.id].append(target.id)

            if edge.source_port_id is not None:
                source_port = source.port_index.get(edge.source_port_id)
                target_port = target.port_index.get(edge.target_port_id or "")
                if source_port is None:
                    raise ModelValidationError(
                        f"edge {edge.id!r} references unknown source port "
                        f"{edge.source_port_id!r}"
                    )
                if target_port is None:
                    raise ModelValidationError(
                        f"edge {edge.id!r} references unknown target port "
                        f"{edge.target_port_id!r}"
                    )
                if source_port.direction is not PortDirection.OUTPUT:
                    raise ModelValidationError(
                        f"edge {edge.id!r} source port must be an output"
                    )
                if target_port.direction is not PortDirection.INPUT:
                    raise ModelValidationError(
                        f"edge {edge.id!r} target port must be an input"
                    )

        if incoming_contains[self.root_node_id] != 0:
            raise ModelValidationError("architecture root cannot have a contains parent")
        invalid_parent_counts = {
            node_id: count
            for node_id, count in incoming_contains.items()
            if node_id != self.root_node_id and count != 1
        }
        if invalid_parent_counts:
            raise ModelValidationError(
                "every non-root node needs exactly one contains parent; "
                f"invalid counts: {invalid_parent_counts}"
            )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ModelValidationError(
                    f"contains edges form a cycle through node {node_id!r}"
                )
            if node_id in visited:
                return
            visiting.add(node_id)
            for child_id in containment_children[node_id]:
                visit(child_id)
            visiting.remove(node_id)
            visited.add(node_id)

        visit(self.root_node_id)
        unreachable = nodes.keys() - visited
        if unreachable:
            raise ModelValidationError(
                f"nodes are unreachable from architecture root: {sorted(unreachable)}"
            )

    def validate_against_project(self, project: ProjectSpec) -> None:
        if self.project_id != project.id:
            raise ModelValidationError(
                f"architecture project {self.project_id!r} does not match "
                f"project spec {project.id!r}"
            )
        if self.project_spec_revision != project.revision:
            raise ModelValidationError(
                "architecture was compiled for project revision "
                f"{self.project_spec_revision}, not {project.revision}"
            )
        known = set(project.requirement_index)
        allocated: set[str] = set()
        for node in self.nodes:
            allocated.update(node.requirement_ids)
        unknown = allocated - known
        if unknown:
            raise ModelValidationError(
                f"architecture allocates unknown requirements: {sorted(unknown)}"
            )
        missing = known - allocated
        if missing:
            raise ModelValidationError(
                f"architecture leaves requirements unallocated: {sorted(missing)}"
            )
        for contract in self.contracts:
            contract.validate_requirement_ids(known)

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArchitectureSpec":
        doc = _strict_fields(
            data,
            label="ArchitectureSpec",
            required={
                "schema_version",
                "id",
                "project_id",
                "root_node_id",
                "target_pack",
                "nodes",
                "edges",
                "contracts",
            },
            optional={"project_spec_revision", "revision", "metadata"},
        )
        _check_schema_version(doc, "ArchitectureSpec")
        return cls(
            id=doc["id"],
            project_id=doc["project_id"],
            root_node_id=doc["root_node_id"],
            target_pack=doc["target_pack"],
            nodes=doc["nodes"],
            edges=doc["edges"],
            contracts=doc["contracts"],
            project_spec_revision=doc.get("project_spec_revision", 1),
            revision=doc.get("revision", 1),
            metadata=doc.get("metadata", {}),
        )


_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.DRAFT: frozenset(
        {RunStatus.AWAITING_APPROVAL, RunStatus.CANCELED}
    ),
    RunStatus.AWAITING_APPROVAL: frozenset(
        {RunStatus.READY, RunStatus.BLOCKED, RunStatus.CANCELED}
    ),
    RunStatus.READY: frozenset({RunStatus.RUNNING, RunStatus.CANCELED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.VERIFYING,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
            RunStatus.CANCELED,
        }
    ),
    RunStatus.VERIFYING: frozenset(
        {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.RUNNING,
            RunStatus.BLOCKED,
            RunStatus.CANCELED,
        }
    ),
    RunStatus.BLOCKED: frozenset(
        {
            RunStatus.AWAITING_APPROVAL,
            RunStatus.READY,
            RunStatus.RUNNING,
            RunStatus.FAILED,
            RunStatus.CANCELED,
        }
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELED: frozenset(),
}

_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELED}
    ),
    TaskStatus.READY: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.CACHED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.VERIFYING,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELED,
        }
    ),
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.RUNNING,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELED,
        }
    ),
    TaskStatus.FAILED: frozenset({TaskStatus.READY, TaskStatus.CANCELED}),
    TaskStatus.BLOCKED: frozenset({TaskStatus.READY, TaskStatus.CANCELED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.CACHED: frozenset(),
    TaskStatus.CANCELED: frozenset(),
}

_EVIDENCE_TRANSITIONS: dict[EvidenceStatus, frozenset[EvidenceStatus]] = {
    EvidenceStatus.PENDING: frozenset(
        {EvidenceStatus.RUNNING, EvidenceStatus.SKIPPED, EvidenceStatus.ERROR}
    ),
    EvidenceStatus.RUNNING: frozenset(
        {
            EvidenceStatus.PASSED,
            EvidenceStatus.FAILED,
            EvidenceStatus.ERROR,
        }
    ),
    EvidenceStatus.PASSED: frozenset(),
    EvidenceStatus.FAILED: frozenset(),
    EvidenceStatus.SKIPPED: frozenset(),
    EvidenceStatus.ERROR: frozenset(),
}

_ARTIFACT_TRANSITIONS: dict[ArtifactStatus, frozenset[ArtifactStatus]] = {
    ArtifactStatus.PENDING: frozenset(
        {ArtifactStatus.AVAILABLE, ArtifactStatus.QUARANTINED, ArtifactStatus.DELETED}
    ),
    ArtifactStatus.AVAILABLE: frozenset(
        {
            ArtifactStatus.QUARANTINED,
            ArtifactStatus.EXPIRED,
            ArtifactStatus.DELETED,
        }
    ),
    ArtifactStatus.QUARANTINED: frozenset(
        {ArtifactStatus.AVAILABLE, ArtifactStatus.EXPIRED, ArtifactStatus.DELETED}
    ),
    ArtifactStatus.EXPIRED: frozenset({ArtifactStatus.DELETED}),
    ArtifactStatus.DELETED: frozenset(),
}
