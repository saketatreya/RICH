"""Deterministic architecture compiler and immutable workflow IR for RICH v2.

The typed model layer rejects malformed documents at construction time.  This module
revalidates the invariants needed by execution and reports *all* discovered problems as
stable diagnostics.  Compilation never emits a partial task graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import heapq
import math
from pathlib import PurePosixPath
from typing import Iterable, TypeAlias

from rich_v2.models import (
    ArchitectureEdge,
    ArchitectureNode,
    ArchitectureSpecV2,
    EdgeKind,
    NodeKind,
    PortDirection,
    ProjectSpecV2,
)


class DiagnosticCode(str, Enum):
    DUPLICATE_NODE_ID = "duplicate_node_id"
    DUPLICATE_EDGE_ID = "duplicate_edge_id"
    DUPLICATE_CONTRACT_ID = "duplicate_contract_id"
    UNKNOWN_ROOT_NODE = "unknown_root_node"
    MISSING_CONTRACT = "missing_contract"
    UNKNOWN_CONTRACT_REFERENCE = "unknown_contract_reference"
    CONTRACT_OWNER_MISMATCH = "contract_owner_mismatch"
    UNASSIGNED_CONTRACT = "unassigned_contract"
    UNKNOWN_EDGE_SOURCE = "unknown_edge_source"
    UNKNOWN_EDGE_TARGET = "unknown_edge_target"
    UNKNOWN_PORT_REFERENCE = "unknown_port_reference"
    INVALID_PORT_DIRECTION = "invalid_port_direction"
    INCOMPATIBLE_PORT_SCHEMA = "incompatible_port_schema"
    DUPLICATE_OWNED_PATH = "duplicate_owned_path"
    OVERLAPPING_OWNED_PATH = "overlapping_owned_path"
    INVALID_CONTAINMENT = "invalid_containment"
    PROJECT_MISMATCH = "project_mismatch"
    PROJECT_REVISION_MISMATCH = "project_revision_mismatch"
    UNKNOWN_REQUIREMENT = "unknown_requirement"
    UNALLOCATED_REQUIREMENT = "unallocated_requirement"
    REQUIREMENT_TRACE_MISMATCH = "requirement_trace_mismatch"
    SYNCHRONOUS_CYCLE = "synchronous_cycle"
    WORKFLOW_INVALID_NODE = "workflow_invalid_node"
    WORKFLOW_EMPTY = "workflow_empty"
    WORKFLOW_PARALLEL_ARITY = "workflow_parallel_arity"
    WORKFLOW_DUPLICATE_BRANCH = "workflow_duplicate_branch"
    WORKFLOW_INVALID_RETRY = "workflow_invalid_retry"
    WORKFLOW_INVALID_TIMEOUT = "workflow_invalid_timeout"
    WORKFLOW_UNKNOWN_TASK = "workflow_unknown_task"
    WORKFLOW_CYCLE = "workflow_cycle"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """Machine-readable reason compilation or workflow validation was rejected."""

    code: DiagnosticCode
    message: str
    node_ids: tuple[str, ...] = ()
    edge_ids: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    owned_paths: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "message": self.message,
            "node_ids": list(self.node_ids),
            "edge_ids": list(self.edge_ids),
            "requirement_ids": list(self.requirement_ids),
            "owned_paths": list(self.owned_paths),
            "locations": list(self.locations),
        }


def _diagnostic_sort_key(diagnostic: Diagnostic) -> tuple[object, ...]:
    return (
        diagnostic.code.value,
        diagnostic.node_ids,
        diagnostic.edge_ids,
        diagnostic.requirement_ids,
        diagnostic.owned_paths,
        diagnostic.locations,
        diagnostic.message,
    )


def _sorted_diagnostics(diagnostics: Iterable[Diagnostic]) -> tuple[Diagnostic, ...]:
    return tuple(sorted(diagnostics, key=_diagnostic_sort_key))


class CompilationError(ValueError):
    """Architecture compilation failed without producing a partial build plan."""

    def __init__(self, diagnostics: Iterable[Diagnostic]):
        self.diagnostics = _sorted_diagnostics(diagnostics)
        if not self.diagnostics:
            raise ValueError("CompilationError requires at least one diagnostic")
        summary = "; ".join(
            f"{item.code.value}: {item.message}" for item in self.diagnostics
        )
        super().__init__(summary)


class WorkflowValidationError(ValueError):
    """A workflow definition is invalid and must not be scheduled."""

    def __init__(self, diagnostics: Iterable[Diagnostic]):
        self.diagnostics = _sorted_diagnostics(diagnostics)
        if not self.diagnostics:
            raise ValueError("WorkflowValidationError requires at least one diagnostic")
        summary = "; ".join(
            f"{item.code.value}: {item.message}" for item in self.diagnostics
        )
        super().__init__(summary)


def _duplicates(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return tuple(sorted(duplicates))


def _index_first(items: Iterable[object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in items:
        item_id = getattr(item, "id")
        result.setdefault(item_id, item)
    return result


def _dependency_consumer(edge: ArchitectureEdge) -> tuple[str, str] | None:
    """Return ``(dependency, consumer)`` for a synchronous edge.

    CONTAINS, CALL, CAPABILITY, and RESOURCE point from a consumer/owner to its
    dependency.  DATA and SCHEMA point from a producer to its consumer.  EVENT is
    asynchronous and intentionally does not constrain build ordering.
    """

    if edge.kind is EdgeKind.EVENT:
        return None
    if edge.kind in {
        EdgeKind.CONTAINS,
        EdgeKind.CALL,
        EdgeKind.CAPABILITY,
        EdgeKind.RESOURCE,
    }:
        return edge.target_node_id, edge.source_node_id
    return edge.source_node_id, edge.target_node_id


def _dependency_graph(
    architecture: ArchitectureSpecV2,
    known_node_ids: set[str],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    dependencies = {node_id: set() for node_id in known_node_ids}
    consumers = {node_id: set() for node_id in known_node_ids}
    for edge in architecture.edges:
        if (
            edge.source_node_id not in known_node_ids
            or edge.target_node_id not in known_node_ids
        ):
            continue
        pair = _dependency_consumer(edge)
        if pair is None:
            continue
        dependency, consumer = pair
        dependencies[consumer].add(dependency)
        consumers[dependency].add(consumer)
    return dependencies, consumers


def _cyclic_components(consumers: dict[str, set[str]]) -> tuple[tuple[str, ...], ...]:
    """Return deterministic cyclic SCCs without relying on Python recursion depth."""

    visited: set[str] = set()
    finish_order: list[str] = []
    ordered_neighbors = {
        node_id: tuple(sorted(node_consumers))
        for node_id, node_consumers in consumers.items()
    }
    for start in sorted(consumers):
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            node_id, next_neighbor = stack[-1]
            neighbors = ordered_neighbors[node_id]
            if next_neighbor < len(neighbors):
                consumer_id = neighbors[next_neighbor]
                stack[-1] = (node_id, next_neighbor + 1)
                if consumer_id not in visited:
                    visited.add(consumer_id)
                    stack.append((consumer_id, 0))
                continue
            stack.pop()
            finish_order.append(node_id)

    dependencies = {node_id: set() for node_id in consumers}
    for dependency_id, node_consumers in consumers.items():
        for consumer_id in node_consumers:
            dependencies[consumer_id].add(dependency_id)
    ordered_dependencies = {
        node_id: tuple(sorted(node_dependencies))
        for node_id, node_dependencies in dependencies.items()
    }

    assigned: set[str] = set()
    components: list[tuple[str, ...]] = []
    for start in reversed(finish_order):
        if start in assigned:
            continue
        assigned.add(start)
        component: list[str] = []
        stack = [(start, 0)]
        while stack:
            node_id, next_neighbor = stack[-1]
            if next_neighbor == 0:
                component.append(node_id)
            neighbors = ordered_dependencies[node_id]
            if next_neighbor < len(neighbors):
                dependency_id = neighbors[next_neighbor]
                stack[-1] = (node_id, next_neighbor + 1)
                if dependency_id not in assigned:
                    assigned.add(dependency_id)
                    stack.append((dependency_id, 0))
                continue
            stack.pop()
        ordered = tuple(sorted(component))
        if len(ordered) > 1 or start in consumers[start]:
            components.append(ordered)

    return tuple(sorted(components))


def validate_architecture(
    architecture: ArchitectureSpecV2,
    project: ProjectSpecV2,
) -> tuple[Diagnostic, ...]:
    """Return every compiler-level error in stable order.

    This deliberately repeats execution-critical model validation.  Persisted
    documents may have crossed migrations or untrusted process boundaries; the
    compiler must fail closed even if it receives a corrupted model instance.
    """

    diagnostics: list[Diagnostic] = []
    duplicate_nodes = _duplicates(node.id for node in architecture.nodes)
    duplicate_edges = _duplicates(edge.id for edge in architecture.edges)
    duplicate_contracts = _duplicates(contract.id for contract in architecture.contracts)
    if duplicate_nodes:
        diagnostics.append(
            Diagnostic(
                DiagnosticCode.DUPLICATE_NODE_ID,
                f"node ids are duplicated: {list(duplicate_nodes)}",
                node_ids=duplicate_nodes,
                locations=("architecture.nodes",),
            )
        )
    if duplicate_edges:
        diagnostics.append(
            Diagnostic(
                DiagnosticCode.DUPLICATE_EDGE_ID,
                f"edge ids are duplicated: {list(duplicate_edges)}",
                edge_ids=duplicate_edges,
                locations=("architecture.edges",),
            )
        )
    if duplicate_contracts:
        diagnostics.append(
            Diagnostic(
                DiagnosticCode.DUPLICATE_CONTRACT_ID,
                f"contract ids are duplicated: {list(duplicate_contracts)}",
                locations=("architecture.contracts",),
            )
        )

    nodes = _index_first(architecture.nodes)
    contracts = _index_first(architecture.contracts)
    node_ids = set(nodes)
    if architecture.root_node_id not in node_ids:
        diagnostics.append(
            Diagnostic(
                DiagnosticCode.UNKNOWN_ROOT_NODE,
                f"root node {architecture.root_node_id!r} does not exist",
                node_ids=(architecture.root_node_id,),
                locations=("architecture.root_node_id",),
            )
        )

    path_owners: dict[str, list[str]] = {}
    contract_owners: dict[str, list[str]] = {}
    for raw_node in architecture.nodes:
        node = raw_node
        for path in node.owned_paths:
            path_owners.setdefault(path, []).append(node.id)
        if node.contract_id is None:
            if node.kind is not NodeKind.RESOURCE:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticCode.MISSING_CONTRACT,
                        f"non-resource node {node.id!r} has no contract",
                        node_ids=(node.id,),
                        locations=(f"nodes.{node.id}.contract_id",),
                    )
                )
            continue
        contract_owners.setdefault(node.contract_id, []).append(node.id)
        raw_contract = contracts.get(node.contract_id)
        if raw_contract is None:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.UNKNOWN_CONTRACT_REFERENCE,
                    f"node {node.id!r} references unknown contract {node.contract_id!r}",
                    node_ids=(node.id,),
                    locations=(f"nodes.{node.id}.contract_id",),
                )
            )
            continue
        contract = raw_contract
        if contract.node_id != node.id:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.CONTRACT_OWNER_MISMATCH,
                    f"contract {contract.id!r} belongs to {contract.node_id!r}, "
                    f"not {node.id!r}",
                    node_ids=tuple(sorted({node.id, contract.node_id})),
                    locations=(
                        f"nodes.{node.id}.contract_id",
                        f"contracts.{contract.id}.node_id",
                    ),
                )
            )
        node_requirements = set(node.requirement_ids)
        contract_requirements = set(contract.traced_requirement_ids)
        if node_requirements != contract_requirements:
            mismatch = tuple(sorted(node_requirements ^ contract_requirements))
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.REQUIREMENT_TRACE_MISMATCH,
                    f"node {node.id!r} allocation does not exactly match "
                    f"contract {contract.id!r} traceability",
                    node_ids=(node.id,),
                    requirement_ids=mismatch,
                    locations=(
                        f"nodes.{node.id}.requirement_ids",
                        f"contracts.{contract.id}",
                    ),
                )
            )

    for path, owners in sorted(path_owners.items()):
        unique_owners = tuple(sorted(set(owners)))
        if len(unique_owners) > 1:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.DUPLICATE_OWNED_PATH,
                    f"owned path {path!r} has multiple owners: {list(unique_owners)}",
                    node_ids=unique_owners,
                    owned_paths=(path,),
                    locations=tuple(
                        f"nodes.{node_id}.owned_paths" for node_id in unique_owners
                    ),
                )
            )
    ownership_entries = sorted(
        (PurePosixPath(path), path, owner)
        for path, owners in path_owners.items()
        for owner in set(owners)
    )
    for index, (left_parts, left_path, left_owner) in enumerate(ownership_entries):
        for right_parts, right_path, right_owner in ownership_entries[index + 1 :]:
            if left_owner == right_owner or left_path == right_path:
                continue
            left_prefix = left_parts.parts == right_parts.parts[: len(left_parts.parts)]
            right_prefix = right_parts.parts == left_parts.parts[: len(right_parts.parts)]
            if not left_prefix and not right_prefix:
                continue
            ownership_node_ids = tuple(sorted((left_owner, right_owner)))
            paths = tuple(sorted((left_path, right_path)))
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.OVERLAPPING_OWNED_PATH,
                    f"owned paths overlap across nodes {list(ownership_node_ids)}: "
                    f"{list(paths)}",
                    node_ids=ownership_node_ids,
                    owned_paths=paths,
                    locations=tuple(
                        f"nodes.{node_id}.owned_paths"
                        for node_id in ownership_node_ids
                    ),
                )
            )

    assigned_contract_ids = set(contract_owners)
    for contract in architecture.contracts:
        if contract.id not in assigned_contract_ids:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.UNASSIGNED_CONTRACT,
                    f"contract {contract.id!r} is not assigned to a node",
                    node_ids=(contract.node_id,),
                    locations=(f"contracts.{contract.id}",),
                )
            )
        elif len(set(contract_owners[contract.id])) > 1:
            owners = tuple(sorted(set(contract_owners[contract.id])))
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.CONTRACT_OWNER_MISMATCH,
                    f"contract {contract.id!r} is assigned to multiple nodes",
                    node_ids=owners,
                    locations=tuple(f"nodes.{node_id}.contract_id" for node_id in owners),
                )
            )

    incoming_contains = {node_id: 0 for node_id in node_ids}
    for edge in architecture.edges:
        raw_source = nodes.get(edge.source_node_id)
        raw_target = nodes.get(edge.target_node_id)
        if raw_source is None:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.UNKNOWN_EDGE_SOURCE,
                    f"edge {edge.id!r} references unknown source "
                    f"{edge.source_node_id!r}",
                    node_ids=(edge.source_node_id,),
                    edge_ids=(edge.id,),
                    locations=(f"edges.{edge.id}.source_node_id",),
                )
            )
        if raw_target is None:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.UNKNOWN_EDGE_TARGET,
                    f"edge {edge.id!r} references unknown target "
                    f"{edge.target_node_id!r}",
                    node_ids=(edge.target_node_id,),
                    edge_ids=(edge.id,),
                    locations=(f"edges.{edge.id}.target_node_id",),
                )
            )
        if raw_source is None or raw_target is None:
            continue
        source = raw_source
        target = raw_target
        if edge.kind is EdgeKind.CONTAINS:
            incoming_contains[target.id] += 1
        if edge.source_port_id is None:
            continue
        source_port = source.port_index.get(edge.source_port_id)
        target_port = target.port_index.get(edge.target_port_id or "")
        if source_port is None:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.UNKNOWN_PORT_REFERENCE,
                    f"edge {edge.id!r} references unknown source port "
                    f"{edge.source_port_id!r}",
                    node_ids=(source.id,),
                    edge_ids=(edge.id,),
                    locations=(f"edges.{edge.id}.source_port_id",),
                )
            )
        elif source_port.direction is not PortDirection.OUTPUT:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.INVALID_PORT_DIRECTION,
                    f"edge {edge.id!r} source port must be an output",
                    node_ids=(source.id,),
                    edge_ids=(edge.id,),
                    locations=(f"nodes.{source.id}.ports.{source_port.id}",),
                )
            )
        if target_port is None:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.UNKNOWN_PORT_REFERENCE,
                    f"edge {edge.id!r} references unknown target port "
                    f"{edge.target_port_id!r}",
                    node_ids=(target.id,),
                    edge_ids=(edge.id,),
                    locations=(f"edges.{edge.id}.target_port_id",),
                )
            )
        elif target_port.direction is not PortDirection.INPUT:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.INVALID_PORT_DIRECTION,
                    f"edge {edge.id!r} target port must be an input",
                    node_ids=(target.id,),
                    edge_ids=(edge.id,),
                    locations=(f"nodes.{target.id}.ports.{target_port.id}",),
                )
            )
        if (
            source_port is not None
            and target_port is not None
            and source_port.schema != target_port.schema
        ):
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.INCOMPATIBLE_PORT_SCHEMA,
                    f"edge {edge.id!r} connects incompatible port schemas",
                    node_ids=(source.id, target.id),
                    edge_ids=(edge.id,),
                    locations=(
                        f"nodes.{source.id}.ports.{source_port.id}.schema",
                        f"nodes.{target.id}.ports.{target_port.id}.schema",
                    ),
                )
            )

    for node_id, parent_count in sorted(incoming_contains.items()):
        expected = 0 if node_id == architecture.root_node_id else 1
        if parent_count != expected:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.INVALID_CONTAINMENT,
                    f"node {node_id!r} has {parent_count} contains parents; "
                    f"expected {expected}",
                    node_ids=(node_id,),
                    locations=("architecture.edges",),
                )
            )

    if architecture.project_id != project.id:
        diagnostics.append(
            Diagnostic(
                DiagnosticCode.PROJECT_MISMATCH,
                f"architecture project {architecture.project_id!r} does not match "
                f"project {project.id!r}",
                locations=("architecture.project_id", "project.id"),
            )
        )
    if architecture.project_spec_revision != project.revision:
        diagnostics.append(
            Diagnostic(
                DiagnosticCode.PROJECT_REVISION_MISMATCH,
                f"architecture targets project revision "
                f"{architecture.project_spec_revision}, not {project.revision}",
                locations=(
                    "architecture.project_spec_revision",
                    "project.revision",
                ),
            )
        )

    known_requirements = set(project.requirement_index)
    allocated_requirements = {
        requirement_id
        for node in architecture.nodes
        for requirement_id in node.requirement_ids
    }
    traced_requirements = {
        requirement_id
        for contract in architecture.contracts
        for requirement_id in contract.traced_requirement_ids
    }
    unknown_requirements = (allocated_requirements | traced_requirements) - known_requirements
    if unknown_requirements:
        ordered = tuple(sorted(unknown_requirements))
        diagnostics.append(
            Diagnostic(
                DiagnosticCode.UNKNOWN_REQUIREMENT,
                f"architecture references unknown requirements: {list(ordered)}",
                requirement_ids=ordered,
                locations=("architecture.nodes", "architecture.contracts"),
            )
        )
    unallocated_requirements = known_requirements - allocated_requirements
    if unallocated_requirements:
        ordered = tuple(sorted(unallocated_requirements))
        diagnostics.append(
            Diagnostic(
                DiagnosticCode.UNALLOCATED_REQUIREMENT,
                f"project requirements are unallocated: {list(ordered)}",
                requirement_ids=ordered,
                locations=("project.requirements", "architecture.nodes"),
            )
        )

    _, consumers = _dependency_graph(architecture, node_ids)
    for component in _cyclic_components(consumers):
        edge_ids = tuple(
            sorted(
                edge.id
                for edge in architecture.edges
                if edge.kind is not EdgeKind.EVENT
                and edge.source_node_id in component
                and edge.target_node_id in component
            )
        )
        diagnostics.append(
            Diagnostic(
                DiagnosticCode.SYNCHRONOUS_CYCLE,
                f"synchronous dependencies contain a cycle among "
                f"{list(component)}",
                node_ids=component,
                edge_ids=edge_ids,
                locations=("architecture.edges",),
            )
        )
    return _sorted_diagnostics(diagnostics)


@dataclass(frozen=True, slots=True)
class CompiledTask:
    task_id: str
    node_id: str
    order: int
    contract_id: str | None
    dependency_ids: tuple[str, ...]
    consumer_ids: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    owned_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "node_id": self.node_id,
            "order": self.order,
            "contract_id": self.contract_id,
            "dependency_ids": list(self.dependency_ids),
            "consumer_ids": list(self.consumer_ids),
            "requirement_ids": list(self.requirement_ids),
            "owned_paths": list(self.owned_paths),
        }


@dataclass(frozen=True, slots=True)
class CompiledArchitecture:
    architecture_id: str
    architecture_revision: int
    project_id: str
    project_revision: int
    root_node_id: str
    target_pack: str
    tasks: tuple[CompiledTask, ...]

    @property
    def task_index(self) -> dict[str, CompiledTask]:
        return {task.node_id: task for task in self.tasks}

    def to_dict(self) -> dict[str, object]:
        return {
            "architecture_id": self.architecture_id,
            "architecture_revision": self.architecture_revision,
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "root_node_id": self.root_node_id,
            "target_pack": self.target_pack,
            "tasks": [task.to_dict() for task in self.tasks],
        }


def _topological_order(
    dependencies: dict[str, set[str]],
    consumers: dict[str, set[str]],
) -> tuple[str, ...]:
    remaining = {node_id: len(items) for node_id, items in dependencies.items()}
    ready = [node_id for node_id, count in remaining.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        node_id = heapq.heappop(ready)
        ordered.append(node_id)
        for consumer_id in sorted(consumers[node_id]):
            remaining[consumer_id] -= 1
            if remaining[consumer_id] == 0:
                heapq.heappush(ready, consumer_id)
    if len(ordered) != len(dependencies):
        raise RuntimeError("validated dependency graph unexpectedly contains a cycle")
    return tuple(ordered)


def compile_architecture(
    architecture: ArchitectureSpecV2,
    project: ProjectSpecV2,
) -> CompiledArchitecture:
    """Validate and deterministically compile one task per architecture node."""

    diagnostics = validate_architecture(architecture, project)
    if diagnostics:
        raise CompilationError(diagnostics)
    nodes: dict[str, ArchitectureNode] = architecture.node_index
    # Resources are declarations consumed by implementation nodes. They are
    # provisioned by an explicit provider gate, not handed to a coding worker as
    # source-owning implementation tasks.
    node_ids = {
        node_id
        for node_id, node in nodes.items()
        if node.kind is not NodeKind.RESOURCE
    }
    dependencies, consumers = _dependency_graph(architecture, node_ids)
    order = _topological_order(dependencies, consumers)
    tasks = tuple(
        CompiledTask(
            task_id=f"implement:{node_id}",
            node_id=node_id,
            order=index,
            contract_id=nodes[node_id].contract_id,
            dependency_ids=tuple(sorted(dependencies[node_id])),
            consumer_ids=tuple(sorted(consumers[node_id])),
            requirement_ids=tuple(sorted(nodes[node_id].requirement_ids)),
            owned_paths=tuple(sorted(nodes[node_id].owned_paths)),
        )
        for index, node_id in enumerate(order)
    )
    return CompiledArchitecture(
        architecture_id=architecture.id,
        architecture_revision=architecture.revision,
        project_id=project.id,
        project_revision=project.revision,
        root_node_id=architecture.root_node_id,
        target_pack=architecture.target_pack,
        tasks=tasks,
    )


def _workflow_failure(
    code: DiagnosticCode,
    message: str,
    *,
    location: str = "workflow",
) -> WorkflowValidationError:
    return WorkflowValidationError(
        (Diagnostic(code, message, locations=(location,)),)
    )


def _workflow_id(value: str, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _workflow_failure(
            DiagnosticCode.WORKFLOW_INVALID_NODE,
            f"{location} must be a non-empty task id",
            location=location,
        )
    return value.strip()


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True, slots=True)
class WorkflowTask:
    task_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _workflow_id(self.task_id, "workflow.task_id"))


@dataclass(frozen=True, slots=True)
class WorkflowSequence:
    steps: tuple["WorkflowNode", ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "steps",
            _workflow_steps(self.steps, "workflow.sequence.steps", minimum=1),
        )


@dataclass(frozen=True, slots=True)
class WorkflowParallel:
    branches: tuple["WorkflowNode", ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "branches",
            _workflow_steps(
                self.branches, "workflow.parallel.branches", minimum=2
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkflowBranchCase:
    condition: str
    workflow: "WorkflowNode"

    def __post_init__(self) -> None:
        if not isinstance(self.condition, str) or not self.condition.strip():
            raise _workflow_failure(
                DiagnosticCode.WORKFLOW_INVALID_NODE,
                "workflow branch condition cannot be empty",
                location="workflow.branch.condition",
            )
        object.__setattr__(self, "condition", self.condition.strip())
        _ensure_workflow_node(self.workflow, "workflow.branch.workflow")


@dataclass(frozen=True, slots=True)
class WorkflowBranch:
    cases: tuple[WorkflowBranchCase, ...]
    otherwise: "WorkflowNode"

    def __post_init__(self) -> None:
        if isinstance(self.cases, (str, bytes)) or not isinstance(self.cases, Iterable):
            raise _workflow_failure(
                DiagnosticCode.WORKFLOW_EMPTY,
                "workflow branch cases must be a sequence",
                location="workflow.branch.cases",
            )
        cases = tuple(self.cases)
        if not cases:
            raise _workflow_failure(
                DiagnosticCode.WORKFLOW_EMPTY,
                "workflow branch needs at least one case",
                location="workflow.branch.cases",
            )
        if any(not isinstance(case, WorkflowBranchCase) for case in cases):
            raise _workflow_failure(
                DiagnosticCode.WORKFLOW_INVALID_NODE,
                "workflow branch cases must be WorkflowBranchCase values",
                location="workflow.branch.cases",
            )
        duplicates = _duplicates(case.condition for case in cases)
        if duplicates:
            raise _workflow_failure(
                DiagnosticCode.WORKFLOW_DUPLICATE_BRANCH,
                f"workflow branch conditions are duplicated: {list(duplicates)}",
                location="workflow.branch.cases",
            )
        _ensure_workflow_node(self.otherwise, "workflow.branch.otherwise")
        object.__setattr__(self, "cases", cases)


@dataclass(frozen=True, slots=True)
class WorkflowRetry:
    workflow: "WorkflowNode"
    max_attempts: int
    backoff_seconds: float = 0

    def __post_init__(self) -> None:
        _ensure_workflow_node(self.workflow, "workflow.retry.workflow")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 2
        ):
            raise _workflow_failure(
                DiagnosticCode.WORKFLOW_INVALID_RETRY,
                "workflow retry max_attempts must be an integer of at least 2",
                location="workflow.retry.max_attempts",
            )
        if not _finite_number(self.backoff_seconds) or self.backoff_seconds < 0:
            raise _workflow_failure(
                DiagnosticCode.WORKFLOW_INVALID_RETRY,
                "workflow retry backoff_seconds must be finite and non-negative",
                location="workflow.retry.backoff_seconds",
            )


@dataclass(frozen=True, slots=True)
class WorkflowTimeout:
    workflow: "WorkflowNode"
    timeout_seconds: float

    def __post_init__(self) -> None:
        _ensure_workflow_node(self.workflow, "workflow.timeout.workflow")
        if not _finite_number(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise _workflow_failure(
                DiagnosticCode.WORKFLOW_INVALID_TIMEOUT,
                "workflow timeout_seconds must be finite and positive",
                location="workflow.timeout.timeout_seconds",
            )


WorkflowNode: TypeAlias = (
    WorkflowTask
    | WorkflowSequence
    | WorkflowParallel
    | WorkflowBranch
    | WorkflowRetry
    | WorkflowTimeout
)
_WORKFLOW_TYPES = (
    WorkflowTask,
    WorkflowSequence,
    WorkflowParallel,
    WorkflowBranch,
    WorkflowRetry,
    WorkflowTimeout,
)


def _ensure_workflow_node(value: object, location: str) -> WorkflowNode:
    if not isinstance(value, _WORKFLOW_TYPES):
        raise _workflow_failure(
            DiagnosticCode.WORKFLOW_INVALID_NODE,
            f"{location} must be a workflow node",
            location=location,
        )
    return value


def _workflow_steps(
    value: Iterable[WorkflowNode],
    location: str,
    *,
    minimum: int,
) -> tuple[WorkflowNode, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise _workflow_failure(
            DiagnosticCode.WORKFLOW_EMPTY,
            f"{location} must be a sequence",
            location=location,
        )
    steps = tuple(value)
    if len(steps) < minimum:
        code = (
            DiagnosticCode.WORKFLOW_PARALLEL_ARITY
            if minimum > 1
            else DiagnosticCode.WORKFLOW_EMPTY
        )
        raise _workflow_failure(
            code,
            f"{location} needs at least {minimum} workflow "
            f"{'branches' if minimum > 1 else 'step'}",
            location=location,
        )
    for index, step in enumerate(steps):
        _ensure_workflow_node(step, f"{location}[{index}]")
    return steps


def validate_workflow(
    workflow: WorkflowNode,
    *,
    known_task_ids: Iterable[str],
) -> tuple[Diagnostic, ...]:
    """Validate task references and reject recursively corrupted workflow objects."""

    diagnostics: list[Diagnostic] = []
    known = set(known_task_ids)
    active: set[int] = set()

    def walk(node: object, location: str) -> None:
        if not isinstance(node, _WORKFLOW_TYPES):
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.WORKFLOW_INVALID_NODE,
                    f"{location} is not a workflow node",
                    locations=(location,),
                )
            )
            return
        identity = id(node)
        if identity in active:
            diagnostics.append(
                Diagnostic(
                    DiagnosticCode.WORKFLOW_CYCLE,
                    f"{location} recursively contains itself",
                    locations=(location,),
                )
            )
            return
        active.add(identity)
        try:
            if isinstance(node, WorkflowTask):
                if node.task_id not in known:
                    diagnostics.append(
                        Diagnostic(
                            DiagnosticCode.WORKFLOW_UNKNOWN_TASK,
                            f"workflow references unknown task {node.task_id!r}",
                            locations=(location,),
                        )
                    )
            elif isinstance(node, WorkflowSequence):
                for index, step in enumerate(node.steps):
                    walk(step, f"{location}.steps[{index}]")
            elif isinstance(node, WorkflowParallel):
                for index, branch in enumerate(node.branches):
                    walk(branch, f"{location}.branches[{index}]")
            elif isinstance(node, WorkflowBranch):
                for index, case in enumerate(node.cases):
                    walk(case.workflow, f"{location}.cases[{index}].workflow")
                walk(node.otherwise, f"{location}.otherwise")
            elif isinstance(node, WorkflowRetry):
                walk(node.workflow, f"{location}.retry")
            elif isinstance(node, WorkflowTimeout):
                walk(node.workflow, f"{location}.timeout")
        finally:
            active.remove(identity)

    walk(workflow, "workflow")
    return _sorted_diagnostics(diagnostics)


def require_valid_workflow(
    workflow: WorkflowNode,
    *,
    known_task_ids: Iterable[str],
) -> None:
    diagnostics = validate_workflow(workflow, known_task_ids=known_task_ids)
    if diagnostics:
        raise WorkflowValidationError(diagnostics)
