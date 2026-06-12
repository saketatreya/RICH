"""Node model — the on-disk representation of a module in the build tree.

§7: Each node is a directory under build/<id> containing:
  contract.yaml   — authored by parent (or root seed)
  decision.json   — {is_leaf:true} or {is_leaf:false, children:[...], edges:[...]}
  deps.yaml       — resolved dependencies (internal nodes)
  src/            — implementation
  tests/          — pytest from DERIVE_TESTS
  status.json     — {status: planned|implemented|verified|failed, reason?}
"""

import json
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


BUILD_ROOT = Path("build")


@dataclass
class Node:
    """A module node in the build tree."""
    id: str
    contract: dict
    is_leaf: bool
    children: list["Node"] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    dependencies: list[dict] = field(default_factory=list)

    def path(self) -> Path:
        return BUILD_ROOT / self.id

    def contract_path(self) -> Path:
        return self.path() / "contract.yaml"

    def decision_path(self) -> Path:
        return self.path() / "decision.json"

    def status_path(self) -> Path:
        return self.path() / "status.json"

    def src_path(self) -> Path:
        return self.path() / "src"

    def tests_path(self) -> Path:
        return self.path() / "tests"

    def deps_path(self) -> Path:
        return self.path() / "deps.yaml"


def save_contract(node: Node):
    """Persist contract.yaml to the node directory."""
    import yaml
    node.path().mkdir(parents=True, exist_ok=True)
    with open(node.contract_path(), "w") as f:
        yaml.dump(node.contract, f, default_flow_style=False, sort_keys=False)


def save_decision(node: Node):
    """Persist decision.json to the node directory."""
    node.path().mkdir(parents=True, exist_ok=True)
    decision: dict = {"is_leaf": node.is_leaf}
    if not node.is_leaf:
        decision["children"] = [c.contract for c in node.children]
        decision["edges"] = node.edges
    with open(node.decision_path(), "w") as f:
        json.dump(decision, f, indent=2)


def save_status(node: Node, status: str, reason: Optional[str] = None):
    """Persist status.json to the node directory."""
    node.path().mkdir(parents=True, exist_ok=True)
    data = {"status": status}
    if reason:
        data["reason"] = reason
    with open(node.status_path(), "w") as f:
        json.dump(data, f, indent=2)


def save_deps(node: Node):
    """Persist deps.yaml for internal nodes."""
    if node.dependencies:
        import yaml
        node.path().mkdir(parents=True, exist_ok=True)
        with open(node.deps_path(), "w") as f:
            yaml.dump(node.dependencies, f, default_flow_style=False, sort_keys=False)


def topological_order(children: list["Node"], edges: list[dict]) -> list["Node"]:
    """Return children in topological order based on dependency edges.

    edges: [{"from": "<child_id>", "to": "<child_id>", "name": "<param_name>"}, ...]
    """
    # Build adjacency: child -> set of children it depends on
    dep_of = {c.id: set() for c in children}
    for edge in edges:
        dep_of[edge["to"]].add(edge["from"])

    ordered = []
    visited = set()
    temp_marks = set()

    def visit(child_id):
        if child_id in temp_marks:
            raise ValueError(f"Cycle detected: {child_id}")
        if child_id in visited:
            return
        temp_marks.add(child_id)
        for dep_id in dep_of.get(child_id, set()):
            visit(dep_id)
        temp_marks.remove(child_id)
        visited.add(child_id)
        # Find the child Node by id
        for c in children:
            if c.id == child_id:
                ordered.append(c)
                break

    for c in children:
        visit(c.id)

    return ordered
