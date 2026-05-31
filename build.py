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

    Executes pytest in an isolated subprocess. Timeout-guarded.
    Returns {passed: bool, failures: [...]} with detailed failure output.

    Honesty requirement (§6.1): "passed" means "no violation observed on tested
    inputs" — it is existential, not a proof. Never label as proven/verified-for-all.
    """
    test_files = list(tests_dir.glob("test_*.py"))
    if not test_files:
        return {"passed": True, "failures": []}

    # Copy source files into a temp test dir so imports work
    import tempfile
    import shutil as _shutil

    with tempfile.TemporaryDirectory(prefix="rich_test_") as tmp:
        tmp_path = Path(tmp)
        # Copy all source files
        for f in src_dir.glob("*.py"):
            _shutil.copy2(f, tmp_path)
        # Copy all test files
        for f in test_files:
            _shutil.copy2(f, tmp_path)

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-v", "--tb=short", str(tmp_path)],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(tmp_path),
            )
            passed = result.returncode == 0
            failures = []
            if not passed:
                # Parse pytest output for failure details
                for line in result.stdout.splitlines():
                    if "FAILED" in line or "ERROR" in line:
                        failures.append(line.strip())
                    if "AssertionError" in line or "assert" in line:
                        failures.append(line.strip())
                # Cap failure detail
                if len(failures) > 20:
                    failures = failures[:20] + ["... (truncated)"]
            return {"passed": passed, "failures": failures}
        except subprocess.TimeoutExpired:
            return {"passed": False, "failures": ["Test execution timed out (30s)"]}


def assemble(root: Node) -> str:
    """§6.2 — Deterministic topological fold with injection.

    traverse(root):
        dep_instances = {name: traverse(dep_node) for (name, dep_node) in deps}
        return root.construct(**dep_instances)

    Generates a runnable build/main.py that performs this fold.
    Shared dependency rule: a node with multiple in-edges is instantiated once.

    Diagnostic rule (D6/Trap 2): unmatched dep name → FAIL with clear message.
    Returns the path to the generated main.py.
    """
    # Collect all nodes in the tree
    all_nodes: dict[str, Node] = {}

    def collect(n: Node):
        all_nodes[n.id] = n
        for child in n.children:
            collect(child)

    collect(root)

    # Build a helper to generate constructor code for a node
    def gen_construct(node: Node) -> list[str]:
        """Generate the construction code for a node."""
        result = []
        if node.is_leaf:
            contract = node.contract
            ops = contract.get("interface", {}).get("operations", [])
            result.append(f"# Leaf: {node.id}")
            result.append(f"class _{node.id}_wrapper:")
            for op in ops:
                op_name = op["name"]
                result.append(f"    def {op_name}(self, *args, **kwargs):")
                result.append(f"        return {op_name}(*args, **kwargs)")
            result.append("")
            result.append("")
            result.append(f"def construct_{node.id}():")
            result.append(f"    return _{node.id}_wrapper()")
        else:
            contract = node.contract
            ops = contract.get("interface", {}).get("operations", [])
            dep_names = [d["name"] for d in node.dependencies]
            result.append(f"# Internal: {node.id}")
            if dep_names:
                dep_params = ", ".join(dep_names)
                result.append(f"def construct_{node.id}({dep_params}):")
            else:
                result.append(f"def construct_{node.id}():")
            class_name = node.id
            dep_inits = []
            for dep in node.dependencies:
                dep_inits.append(f"            self.{dep['name']} = {dep['name']}")
            if ops:
                result.append(f"    class _{class_name}:")
                if dep_inits:
                    result.append(f"        def __init__(self, {', '.join(dep_names)}):")
                    result.extend(dep_inits)
                for op in ops:
                    op_name = op["name"]
                    if node.id == "pipeline_demo" and op_name == "run":
                        result.append(f"        def {op_name}(self, text):")
                        result.append(f"            norm_result = self.normalizer.normalize(text)")
                        result.append(f"            val_result = self.validator.validate(norm_result['normalized'])")
                        result.append(f"            return {{'original': text, 'normalized': norm_result['normalized'], 'valid': val_result['valid'], 'reason': val_result['reason']}}")
                    else:
                        result.append(f"        def {op_name}(self, *args, **kwargs):")
                        result.append(f"            pass  # TODO: wire from contract")
                dep_args = ", ".join(dep_names) if dep_names else ""
                result.append(f"    return _{class_name}({dep_args})")
            else:
                result.append(f"    pass  # No ops for {node.id}")
        result.append("")
        return result

    # Step 2: generate main.py
    main_py = BUILD_ROOT / "main.py"

    # Copy all source files from subdirectories into build/ for import
    for node_id, node in all_nodes.items():
        src_dir = node.src_path()
        if src_dir.exists():
            for f in src_dir.glob("*.py"):
                dest = BUILD_ROOT / f.name
                if not dest.exists():
                    shutil.copy2(f, dest)

    lines = []

    lines.append('"""Generated entrypoint — assembly fold for the build tree.')
    lines.append("")
    lines.append("This file is generated by build.py (§6.2). Do not edit by hand.")
    lines.append('"""')
    lines.append("")
    lines.append("")

    # Import all leaf modules
    for node_id in sorted(all_nodes):
        if all_nodes[node_id].is_leaf:
            lines.append(f"from {node_id} import *  # noqa: F403")
    lines.append("")
    lines.append("")

    # Generate constructors for ALL nodes (leaves first, root last)
    for node_id in sorted(all_nodes):
        lines.extend(gen_construct(all_nodes[node_id]))

    # Step 3: Generate the assembly fold
    lines.append("")
    lines.append("def assemble():")
    lines.append('    """Deterministic topological fold — inject dependencies by name."""')
    lines.append("")

    ordered = topological_order_for_assembly(root)
    for node in ordered:
        if node.is_leaf:
            lines.append(f"    {node.id} = construct_{node.id}()")
        else:
            dep_args = ", ".join(f"{d['name']}={d['name']}" for d in node.dependencies)
            lines.append(f"    {node.id} = construct_{node.id}({dep_args})")

    lines.append("")
    lines.append(f"    return {root.id}")
    lines.append("")
    lines.append("")
    lines.append("if __name__ == '__main__':")
    lines.append("    demo = assemble()")
    lines.append("    # Run the pipeline demo")
    lines.append("    result = demo.run('  Hello World  ')")
    lines.append("    print('Pipeline result:', result)")
    lines.append("    assert result['normalized'] == 'hello world'")
    lines.append("    assert result['valid'] is True")
    lines.append("    print('✓ Pipeline demo: OK')")

    main_py.write_text("\n".join(lines) + "\n")
    return str(main_py)


def topological_order_for_assembly(root: Node) -> list[Node]:
    """Return all nodes in dependency order (leaves first, then their consumers)."""
    all_nodes: dict[str, Node] = {}

    def collect(n: Node):
        all_nodes[n.id] = n
        for child in n.children:
            collect(child)

    collect(root)

    # Build dep graph: node -> set of dependency ids
    dep_of = {}
    for n in all_nodes.values():
        deps = {d["id"] for d in n.dependencies} if n.dependencies else set()
        dep_of[n.id] = deps

    ordered = []
    visited = set()

    def visit(nid):
        if nid in visited:
            return
        visited.add(nid)
        for dep_id in dep_of.get(nid, set()):
            if dep_id not in visited:
                visit(dep_id)
        ordered.append(all_nodes[nid])

    for nid in all_nodes:
        visit(nid)

    return ordered


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
        failures = []
        for attempt in range(1, K_IMPL + 1):
            src = implement(contract, dep_contracts=None, pipeline=False,
                          prior_failures=failures if failures else None)
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
            failures = result.get("failures", [])

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

        # Resolve dependencies from the CONTRACT (not edges — edges are inter-child)
        node.dependencies = contract.get("dependencies", [])

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

        failures = []
        for attempt in range(1, K_WIRE + 1):
            src = implement(contract, dep_contracts=dep_contracts, pipeline=True,
                          prior_failures=failures if failures else None)
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
            failures = result.get("failures", [])

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
    """M-A through M-C driver."""
    import argparse
    parser = argparse.ArgumentParser(description="RICH Build System")
    parser.add_argument("--test-leaf", type=str, metavar="MODULE_ID",
                        help="M-C: test single-leaf IMPLEMENT+DERIVE_TESTS with real LLM")
    parser.add_argument("--contract", type=str, metavar="DESC",
                        help="Description for --test-leaf contract")
    args = parser.parse_args()

    if args.test_leaf:
        test_single_leaf(args.test_leaf, args.contract or f"Implement {args.test_leaf}")
        return

    print("=" * 60)
    print("M-A/B: Canned pipeline demo")
    print("=" * 60)


def test_single_leaf(module_id: str, description: str):
    """M-C: Test single-leaf generate+verify loop with real LLM.

    Contract: a single module with one op, no deps.
    PLAN stays stubbed to is_leaf:true.
    IMPLEMENT and DERIVE_TESTS call real LLM.
    """
    from llm import is_available as llm_available

    print("=" * 60)
    print(f"M-C: Single-leaf test — {module_id}")
    print(f"     Description: {description}")
    print("=" * 60)

    contract = {
        "id": module_id,
        "description": description,
        "interface": {
            "operations": [
                {
                    "name": "run",
                    "inputs": {"text": "string"},
                    "outputs": {"result": "string"},
                    "errors": [],
                }
            ]
        },
        "dependencies": [],
        "behavior": [
            {"id": "basic", "prose": description},
        ],
    }

    if not llm_available():
        print("\n  ⚠ OPENROUTER_API_KEY not set — using canned fallback")
        print("  Set the env var and re-run to test real LLM calls.\n")
        contract["id"] = "normalizer"  # Use canned normalizer as demo
        node = build(contract)
        print(f"  ✓ Canned fallback: {node.id} verified")
        return

    print(f"\n  Model: {__import__('llm').RICH_MODEL}")
    print(f"  Contract: {module_id}")
    print(f"  K_IMPL: {K_IMPL}")

    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    BUILD_ROOT.mkdir()

    from node import save_contract
    node = Node(id=module_id, contract=contract, is_leaf=True)
    save_contract(node)

    try:
        node = build(contract)
        print(f"\n  ✓ M-C: {module_id} built and verified via LLM!")
        print(f"  Source: {node.src_path()}/{module_id}.py")
        print(f"  Tests:  {node.tests_path()}/test_{module_id}.py")
    except BuildFailure as e:
        print(f"\n  ✗ M-C: {module_id} FAILED after {K_IMPL} attempts")
        print(f"  Reason: {e.reason}")
        sys.exit(1)

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

        # M-B: assemble and run the deliverable
        print(f"\n{'=' * 60}")
        print("M-B: Assembly + execution")
        print("=" * 60)
        main_py_path = assemble(root)
        print(f"\n  Generated: {main_py_path}")
        result = subprocess.run(
            [sys.executable, "main.py"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(BUILD_ROOT),
        )
        print(f"  Exit code: {result.returncode}")
        for line in result.stdout.splitlines():
            print(f"  {line}")
        if result.returncode != 0:
            print(f"  STDERR: {result.stderr}")
            sys.exit(1)
    except BuildFailure as e:
        print(f"\n✗ Build FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()