"""build.py — The core recursive procedure (§4) and M-A driver.

build(contract) -> Node | FAILURE:
    decision = PLAN(contract)
    persist decision.json, status=planned
    if decision.is_leaf:
        tests = DERIVE_TESTS(contract)
        persist tests/
        for attempt in 1..K_IMPL:
            src = IMPLEMENT(contract, dep_contracts={})
            result = run_tests(src, tests)
            if result.passed:
                persist src/, status=verified
                return Leaf(contract, src, tests)
        return FAILURE(...)
    else:  # internal node
        for child_contract in topological_order(decision.children, decision.edges):
            node = build(child_contract)  # RECURSE
            if node is FAILURE: ...
            children[child_contract.id] = node
        tests = DERIVE_TESTS(contract)
        for attempt in 1..K_WIRE:
            src = IMPLEMENT(contract, dep_contracts={child_contracts}, pipeline=True)
            result = run_tests(src, tests)
            if result.passed: return Internal(...)
        return FAILURE(...)
"""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from node import (
    BUILD_ROOT,
    Node,
    save_contract,
    save_decision,
    save_deps,
    save_status,
    topological_order,
)
from skills import plan, implement, derive_tests


K_IMPL = 3
K_WIRE = 3


class BuildFailure(Exception):
    """A node could not be built."""
    def __init__(self, contract_id: str, reason: str):
        self.contract_id = contract_id
        self.reason = reason
        super().__init__(f"FAILURE [{contract_id}]: {reason}")


def run_tests(src_dir: Path, tests_dir: Path) -> dict:
    """§6.1 — Run consumer-derived tests against the implementation.

    M-A: Stubbed — always returns {passed: True, failures: []}.
    Real subprocess execution arrives in M-B.
    """
    return {"passed": True, "failures": []}


def assemble(node: Node) -> object:
    """§6.2 — Deterministic topological fold with injection.

    M-A: Stubbed. Real assembly arrives in M-B.
    """
    return None


def build(contract: dict) -> Node:
    """The core recursive procedure (§4).

    Takes a contract, returns a verified Node or raises BuildFailure.
    """
    # 1. PLAN
    decision = plan(contract)
    node_id = contract["id"]

    # Create node
    node = Node(
        id=node_id,
        contract=contract,
        is_leaf=decision["is_leaf"],
    )
    save_contract(node)
    save_decision(node)
    save_status(node, "planned")

    if node.is_leaf:
        # 2a. DERIVE_TESTS
        tests_src = derive_tests(contract)
        node.tests_path().mkdir(parents=True, exist_ok=True)
        test_file = node.tests_path() / f"test_{node_id}.py"
        test_file.write_text(tests_src)

        # 3a. IMPLEMENT + verify loop
        for attempt in range(1, K_IMPL + 1):
            src = implement(contract, dep_contracts=None, pipeline=False)
            node.src_path().mkdir(parents=True, exist_ok=True)
            src_file = node.src_path() / f"{node_id}.py"
            src_file.write_text(src)

            result = run_tests(node.src_path(), node.tests_path())
            if result["passed"]:
                save_status(node, "verified")
                save_deps(node)
                return node

            # Include failures in next attempt's prompt (wired in M-C)
            print(f"  [{node_id}] attempt {attempt}/{K_IMPL} FAILED: {result.get('failures', 'unknown')}")

        save_status(node, "failed", reason=f"leaf unsatisfiable after {K_IMPL} attempts")
        raise BuildFailure(node_id, f"leaf unsatisfiable after {K_IMPL} attempts")

    else:
        # 2b. Internal node — recurse on children
        children_contracts = decision["children"]
        edges = decision.get("edges", [])

        # Build child nodes
        children_nodes = {}
        for child_contract in children_contracts:
            child_node = build(child_contract)
            children_nodes[child_contract["id"]] = child_node

        node.children = list(children_nodes.values())
        node.edges = edges

        # Resolve dependencies: map edge names to child ids
        node.dependencies = []
        for edge in edges:
            node.dependencies.append({
                "name": edge["name"],
                "id": edge["to"],
            })

        # 3b. DERIVE_TESTS for the internal node
        tests_src = derive_tests(contract)
        node.tests_path().mkdir(parents=True, exist_ok=True)
        test_file = node.tests_path() / f"test_{node_id}.py"
        test_file.write_text(tests_src)

        # 4b. IMPLEMENT wiring + verify loop
        # Build dep_contracts dict: {name: contract, ...}
        dep_contracts = {}
        for dep in node.dependencies:
            child_id = dep["id"]
            dep_contracts[dep["name"]] = children_nodes[child_id].contract

        for attempt in range(1, K_WIRE + 1):
            src = implement(contract, dep_contracts=dep_contracts, pipeline=True)
            node.src_path().mkdir(parents=True, exist_ok=True)
            src_file = node.src_path() / f"{node_id}.py"
            src_file.write_text(src)

            # Also copy child source files into the parent's src dir for test_exec
            for child_name, child_node in children_nodes.items():
                child_src = child_node.src_path()
                if child_src.exists():
                    for f in child_src.iterdir():
                        dest = node.src_path() / f.name
                        if not dest.exists():
                            shutil.copy2(f, dest)

            result = run_tests(node.src_path(), node.tests_path())
            if result["passed"]:
                save_status(node, "verified")
                save_deps(node)
                # Update decision with children contracts
                save_decision(node)
                return node

            print(f"  [{node_id}] wiring attempt {attempt}/{K_WIRE} FAILED: {result.get('failures', 'unknown')}")

        save_status(node, "failed", reason=f"wiring failed after {K_WIRE} attempts")
        raise BuildFailure(node_id, f"wiring failed after {K_WIRE} attempts")


# ═════════════════════════════════════════════════════════════════════
# M-A: Canned pipeline demo — "normalize then validate a string"
# ═════════════════════════════════════════════════════════════════════

ROOT_CONTRACT = {
    "id": "pipeline_demo",
    "description": "Normalize a string (strip whitespace, lowercase) then validate it (non-empty, no special chars)",
    "interface": {
        "operations": [
            {
                "name": "run",
                "inputs": {"text": "string"},
                "outputs": {"original": "string", "normalized": "string", "valid": "bool", "reason": "string"},
                "errors": [],
            }
        ]
    },
    "dependencies": [
        {"name": "normalizer", "id": "normalizer"},
        {"name": "validator", "id": "validator"},
    ],
    "behavior": [
        {
            "id": "pipeline_order",
            "prose": "Normalization happens before validation",
        },
        {
            "id": "valid_output",
            "prose": "If valid is true, reason must be 'OK'",
        },
    ],
}


def main():
    """M-A driver: build the canned pipeline demo. Zero LLM calls."""
    print("=" * 60)
    print("M-A: Canned pipeline demo")
    print("=" * 60)

    # Clean build dir
    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    BUILD_ROOT.mkdir()

    try:
        root = build(ROOT_CONTRACT)
        print(f"\n✓ Build succeeded!")
        print(f"  Root: {root.id} (is_leaf={root.is_leaf})")
        print(f"  Children: {[c.id for c in root.children]}")
        print(f"  Status: verified")
        print(f"\n  Tree on disk:")
        for p in sorted(BUILD_ROOT.rglob("*")):
            if p.is_file():
                print(f"    {p}")
    except BuildFailure as e:
        print(f"\n✗ Build FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()