"""What a change actually costs.

RICH could build software once. Software is not built once, and a compiler that
can only do greenfield is a demonstration rather than a way of working. This is
the piece that makes the modular claim mean something: given two approved
revisions, compute the smallest set of components that must be regenerated, and
let everything else replay from memo.

The interesting part is not that some nodes are stale. It is *why the blast
radius stops where it does*, and the answer is the information firewall. A
worker is shown its dependencies' contracts and never their source, so:

- a node whose **implementation** changed cannot affect its consumers, because
  no consumer was ever shown that implementation; and
- a node whose **contract** changed invalidates every consumer, because the
  contract is exactly what they were shown.

That is a compositional guarantee falling out of a discipline the system
already enforces, rather than a heuristic about what probably broke. It is the
difference between "rebuild everything, it is cheaper than thinking" and a
change whose cost is proportional to the change.

Verification is a separate question and gets a separate answer: the gates are
whole-application and always re-run. Reusing an *answer* is not reusing a
*verdict*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .models import ArchitectureSpec, Contract, ProjectSpec


@dataclass(frozen=True, slots=True)
class RequirementDelta:
    """What changed in the approved intent."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()

    @property
    def touched(self) -> frozenset[str]:
        return frozenset(self.added) | frozenset(self.removed) | frozenset(self.modified)

    @property
    def is_empty(self) -> bool:
        return not self.touched

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "modified": list(self.modified),
        }


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """The compiled cost of moving from one approved revision to the next."""

    requirements: RequirementDelta
    directly_stale: tuple[str, ...] = ()
    contract_changed: tuple[str, ...] = ()
    consumers_stale: tuple[str, ...] = ()
    added_nodes: tuple[str, ...] = ()
    removed_nodes: tuple[str, ...] = ()
    reusable: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def stale(self) -> tuple[str, ...]:
        """Every node that must be regenerated, in a stable order."""

        return tuple(
            sorted(
                set(self.directly_stale)
                | set(self.contract_changed)
                | set(self.consumers_stale)
                | set(self.added_nodes)
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirements": self.requirements.to_dict(),
            "stale": list(self.stale),
            "directly_stale": list(self.directly_stale),
            "contract_changed": list(self.contract_changed),
            "consumers_stale": list(self.consumers_stale),
            "added_nodes": list(self.added_nodes),
            "removed_nodes": list(self.removed_nodes),
            "reusable": list(self.reusable),
            "notes": list(self.notes),
        }


def _requirement_delta(before: ProjectSpec, after: ProjectSpec) -> RequirementDelta:
    old = {item.id: item for item in before.requirements}
    new = {item.id: item for item in after.requirements}
    # Acceptance scenarios are part of what a requirement means: changing the
    # oracle changes the claim without touching the sentence.
    old_scenarios = _scenarios_by_requirement(before)
    new_scenarios = _scenarios_by_requirement(after)
    modified = tuple(
        sorted(
            identifier
            for identifier in set(old) & set(new)
            if old[identifier].to_dict() != new[identifier].to_dict()
            or old_scenarios.get(identifier) != new_scenarios.get(identifier)
        )
    )
    return RequirementDelta(
        added=tuple(sorted(set(new) - set(old))),
        removed=tuple(sorted(set(old) - set(new))),
        modified=modified,
    )


def _scenarios_by_requirement(spec: ProjectSpec) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for scenario in spec.acceptance_scenarios:
        for requirement_id in scenario.requirement_ids:
            index.setdefault(requirement_id, []).append(scenario.to_dict())
    for entries in index.values():
        entries.sort(key=lambda item: str(item.get("id", "")))
    return index


def _contracts_by_node(architecture: ArchitectureSpec) -> dict[str, Contract]:
    return {contract.node_id: contract for contract in architecture.contracts}


def _shape(node: Any) -> dict[str, Any]:
    """What a node is responsible for, apart from the promise it makes.

    Ownership and ports are not part of a contract, so a change to them is
    invisible to consumers -- but the node itself is being asked to produce
    something different and cannot replay its last answer.
    """

    document = node.to_dict()
    return {
        "kind": document.get("kind"),
        "owned_paths": document.get("owned_paths"),
        "ports": document.get("ports"),
        "contract_id": document.get("contract_id"),
    }


def _behaviour(contract: Contract) -> dict[str, Any]:
    """What a consumer was actually shown of this contract.

    Deliberately not ``to_dict()``. A contract also carries planner-defined
    metadata -- the built-in planner copies the whole project spec into it --
    and comparing that would make every contract differ whenever any
    requirement anywhere changed, which is the opposite of change locality and
    would be a bug wearing the costume of caution.

    ``projection`` is metadata-free for the same reason: metadata is not part
    of the promise. Operations, obligations and invariants are.
    """

    document = contract.to_dict()
    return {
        "id": document.get("id"),
        "node_id": document.get("node_id"),
        "operations": document.get("operations"),
        "obligations": document.get("obligations"),
        "invariants": document.get("invariants"),
    }


def _consumers(architecture: ArchitectureSpec) -> dict[str, set[str]]:
    """Who depends on whom, by node id.

    An edge runs from the consumer to the thing it consumes, so the consumers
    of a node are the sources of the edges pointing at it.
    """

    index: dict[str, set[str]] = {node.id: set() for node in architecture.nodes}
    for edge in architecture.edges:
        index.setdefault(edge.target_node_id, set()).add(edge.source_node_id)
    return index


def _transitive_consumers(
    seeds: Iterable[str], consumers: Mapping[str, set[str]]
) -> set[str]:
    """Everything downstream of a contract change.

    Transitive because a consumer whose own contract is expressed in terms of
    the changed one has itself changed, and its consumers were shown that.
    """

    found: set[str] = set()
    pending = list(seeds)
    while pending:
        current = pending.pop()
        for consumer in consumers.get(current, ()):  # noqa: B007
            if consumer not in found:
                found.add(consumer)
                pending.append(consumer)
    return found


def compile_change(
    *,
    before_spec: ProjectSpec,
    after_spec: ProjectSpec,
    before_architecture: ArchitectureSpec,
    after_architecture: ArchitectureSpec,
) -> ChangeSet:
    """Compute the smallest set of nodes a change forces to be regenerated."""

    delta = _requirement_delta(before_spec, after_spec)
    before_nodes = {node.id: node for node in before_architecture.nodes}
    after_nodes = {node.id: node for node in after_architecture.nodes}

    added_nodes = tuple(sorted(set(after_nodes) - set(before_nodes)))
    removed_nodes = tuple(sorted(set(before_nodes) - set(after_nodes)))

    touched = delta.touched
    directly_stale = tuple(
        sorted(
            node_id
            for node_id, node in after_nodes.items()
            if touched & set(node.requirement_ids)
            # A node whose allocation changed is stale even if every
            # requirement it now owns is textually identical: it is being asked
            # to be responsible for a different set of things. Same for its
            # shape -- different owned paths means a different job.
            or (
                node_id in before_nodes
                and (
                    set(node.requirement_ids)
                    != set(before_nodes[node_id].requirement_ids)
                    or _shape(node) != _shape(before_nodes[node_id])
                )
            )
        )
    )

    before_contracts = _contracts_by_node(before_architecture)
    after_contracts = _contracts_by_node(after_architecture)
    contract_changed = tuple(
        sorted(
            node_id
            for node_id in set(before_contracts) & set(after_contracts)
            if _behaviour(before_contracts[node_id])
            != _behaviour(after_contracts[node_id])
        )
    )

    consumers = _consumers(after_architecture)
    # Only a contract change propagates. An implementation change cannot reach
    # a consumer that was never shown it -- which is the firewall, cashed in.
    downstream = _transitive_consumers(contract_changed, consumers)
    consumers_stale = tuple(sorted(downstream - set(contract_changed)))

    stale = (
        set(directly_stale)
        | set(contract_changed)
        | set(consumers_stale)
        | set(added_nodes)
    )
    reusable = tuple(sorted(set(after_nodes) - stale))

    notes: list[str] = []
    if not stale:
        notes.append("Nothing is stale; every component replays from memo.")
    if directly_stale and not contract_changed:
        notes.append(
            "Contracts are unchanged, so no consumer is affected: a consumer "
            "was never shown an implementation, only a promise."
        )
    if removed_nodes:
        notes.append(
            "Removed components keep their evidence in the store; their source "
            "simply stops being owned by the approved architecture."
        )
    notes.append(
        "Every gate re-runs regardless. Reusing an answer is not reusing a verdict."
    )

    return ChangeSet(
        requirements=delta,
        directly_stale=directly_stale,
        contract_changed=contract_changed,
        consumers_stale=consumers_stale,
        added_nodes=added_nodes,
        removed_nodes=removed_nodes,
        reusable=reusable,
        notes=tuple(notes),
    )
