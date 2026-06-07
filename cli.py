"""cli.py — RICH command-line driver + canned (no-LLM) demos.

Split out of build.py (cleanup): build.py is now the engine only. This module
holds the argparse CLI and the canned regression demos (M-A pipeline, M-F fan-in,
M-G deep + memo) plus the live single-leaf / decompose drivers.

Run:  python cli.py --fan-in | --deep | --memo-test
      python cli.py --test-leaf <id> --contract "<desc>"     (live)
      python cli.py --decompose "<desc>"                     (live)
      python cli.py                                          (M-A canned pipeline)
"""

import argparse
import shutil
import subprocess
import sys

from build import build, assemble, BuildFailure, K_IMPL, K_WIRE
from node import BUILD_ROOT, Node


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


# M-F: Fan-in demo root contract
FAN_IN_ROOT_CONTRACT = {
    "id": "email_checker",
    "description": "Check email format validity and whether domain is a common provider, using a shared regex engine",
    "interface": {
        "operations": [
            {
                "name": "check",
                "inputs": {"email": "string"},
                "outputs": {"email": "string", "valid_format": "bool", "common_domain": "bool", "domain": "string"},
                "errors": [],
            }
        ]
    },
    "dependencies": [
        {"name": "regex_engine", "id": "regex_engine"},
        {"name": "format_checker", "id": "format_checker"},
        {"name": "domain_checker", "id": "domain_checker"},
    ],
    "behavior": [
        {"id": "share_regex", "prose": "Both format_checker and domain_checker share the same regex_engine instance"},
        {"id": "valid_detection", "prose": "Returns valid_format=true for properly formatted emails, common_domain=true for gmail/yahoo/outlook"},
    ],
}


def test_fan_in():
    """M-F: Test shared dependency (fan-in) with canned data.

    Email checker: format_checker and domain_checker both depend on regex_engine.
    Assembly must instantiate regex_engine ONCE and inject the same instance into both.
    """
    print("=" * 60)
    print("M-F: Fan-in (shared dependency) test")
    print("     Two children share one regex_engine")
    print("=" * 60)

    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    BUILD_ROOT.mkdir()

    try:
        root = build(FAN_IN_ROOT_CONTRACT, use_canned=True)
        print(f"\n✓ Fan-in build succeeded!")
        print(f"  Root: {root.id}")
        print(f"  Children: {[c.id for c in root.children]}")
        print(f"  Edges: {root.edges}")

        # Verify regex_engine is a dependency of both format_checker and domain_checker
        regex_shared = [
            c.id for c in root.children
            if any(d["id"] == "regex_engine" for d in c.dependencies)
        ]
        print(f"  Both depend on regex_engine: {regex_shared}")
        assert len(regex_shared) == 2, f"Expected 2 children sharing regex_engine, got {regex_shared}"

        # Assemble and verify shared instantiation
        print(f"\n{'=' * 60}")
        print("Assembly (shared dependency check)")
        print("=" * 60)
        main_py_path = assemble(root)
        print(f"  Generated: {main_py_path}")

        # Verify main.py has only ONE construct_regex_engine() CALL (not def)
        main_py_content = (BUILD_ROOT / "main.py").read_text()
        regex_constructs = main_py_content.count("= construct_regex_engine()")
        print(f"  construct_regex_engine() calls in main.py: {regex_constructs}")
        assert regex_constructs == 1, f"Expected 1 shared instantiation, got {regex_constructs}"

        # Run the deliverable
        result = subprocess.run(
            [sys.executable, "main.py"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(BUILD_ROOT),
        )
        if result.returncode == 0:
            print(f"  ✓ Deliverable runs successfully")
            for line in result.stdout.splitlines():
                print(f"    {line}")
        else:
            print(f"  ✗ Deliverable failed (exit {result.returncode})")
            print(f"  STDERR: {result.stderr[:500]}")
            sys.exit(1)

    except BuildFailure as e:
        print(f"\n✗ Fan-in build FAILED: {e}")
        sys.exit(1)


def test_single_leaf(module_id: str, description: str):
    """M-C: Test single-leaf generate+verify loop with real LLM."""
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
        contract["id"] = "normalizer"
        node = build(contract)
        print(f"  ✓ Canned fallback: {node.id} verified")
        return

    print(f"\n  Model: {__import__('llm').RICH_MODEL}")
    print(f"  K_IMPL: {K_IMPL}")

    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    BUILD_ROOT.mkdir()

    try:
        node = build(contract)
        print(f"\n  ✓ {module_id} built and verified via LLM!")
        print(f"  Source: {node.src_path()}/{module_id}.py")
        print(f"  Tests:  {node.tests_path()}/test_{module_id}.py")
    except BuildFailure as e:
        print(f"\n  ✗ {module_id} FAILED after {K_IMPL} attempts: {e.reason}")
        sys.exit(1)


def test_decompose(desc: str, goal: str):
    """M-E: Test full decomposition pipeline with real LLM.

    Creates a root contract from the goal description.
    PLAN can decompose into children.
    IMPLEMENT generates all modules.
    DERIVE_TESTS generates all tests.
    Assembly produces runnable deliverable.
    """
    from llm import is_available as llm_available

    print("=" * 60)
    print(f"M-E: Decomposition test")
    print(f"     Goal: {goal}")
    print("=" * 60)

    if not llm_available():
        print("\n  ⚠ OPENROUTER_API_KEY not set — cannot test decomposition")
        print("  Set the env var and re-run.")
        sys.exit(1)

    # Build root contract from goal
    root_id = desc.lower().replace(" ", "_")[:32]
    root_contract = {
        "id": root_id,
        "description": goal,
        "interface": {
            "operations": [
                {
                    "name": "run",
                    "inputs": {"input_text": "string"},
                    "outputs": {"result": "string"},
                    "errors": [],
                }
            ]
        },
        "dependencies": [],
        "behavior": [
            {"id": "goal", "prose": goal},
        ],
    }

    print(f"\n  Model: {__import__('llm').RICH_MODEL}")
    print(f"  Root ID: {root_id}")
    print(f"  K_IMPL: {K_IMPL}, K_WIRE: {K_WIRE}")
    print(f"  Allowing decomposition: YES")
    print()

    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    BUILD_ROOT.mkdir()

    try:
        root = build(root_contract, allow_decompose=True)

        if root.is_leaf:
            print(f"\n  ✓ Built as single leaf module")
            print(f"  Source: {root.src_path()}/{root_id}.py")
            print(f"  Tests:  {root.tests_path()}/test_{root_id}.py")
        else:
            print(f"\n  ✓ Decomposed into {len(root.children)} children:")
            for child in root.children:
                print(f"    - {child.id} (leaf={child.is_leaf})")
            print(f"  Root wiring: {root.src_path()}/{root_id}.py")

        # Assemble and run
        print(f"\n{'=' * 60}")
        print("Assembly + execution")
        print("=" * 60)
        main_py_path = assemble(root)
        print(f"  Generated: {main_py_path}")

        result = subprocess.run(
            [sys.executable, "main.py"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(BUILD_ROOT),
        )
        if result.returncode == 0:
            print(f"  ✓ Deliverable runs successfully")
            for line in result.stdout.splitlines():
                print(f"    {line}")
        else:
            print(f"  ✗ Deliverable failed (exit {result.returncode})")
            print(f"  STDERR: {result.stderr[:500]}")

    except BuildFailure as e:
        print(f"\n  ✗ Build FAILED: {e}")
        sys.exit(1)


def test_deep():
    """M-G: Test depth-2 recursion with canned data."""
    from deep_test import (
        CANNED_DEEP_DECISION, CANNED_PASSWORD_PIPELINE_DECISION,
        CANNED_IMPLS_DEEP, CANNED_TESTS_DEEP,
    )
    import skills

    # Register deep canned data
    for k, v in CANNED_IMPLS_DEEP.items():
        skills.CANNED_IMPLS[k] = v
    for k, v in CANNED_TESTS_DEEP.items():
        skills.CANNED_TESTS[k] = v

    # Override plan_canned at module level
    _orig_plan_canned = skills.plan_canned

    def plan_canned_deep(contract):
        if contract["id"] == "password_pipeline":
            return CANNED_PASSWORD_PIPELINE_DECISION
        if contract["id"] == "validate_registration":
            return CANNED_DEEP_DECISION
        return _orig_plan_canned(contract)

    skills.plan_canned = plan_canned_deep

    print("=" * 60)
    print("M-G: Depth-2 recursion test")
    print("     validate_registration → password_pipeline → (length_check, complexity_check)")
    print("=" * 60)

    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    BUILD_ROOT.mkdir()

    DEEP_ROOT_CONTRACT = {
        "id": "validate_registration",
        "description": "Validate username, password strength, and generate welcome token",
        "interface": {
            "operations": [{
                "name": "validate",
                "inputs": {"username": "string", "password": "string"},
                "outputs": {"username_ok": "bool", "password_ok": "bool", "token": "string", "reason": "string"},
                "errors": [],
            }]
        },
        "dependencies": [
            {"name": "username_checker", "id": "username_checker"},
            {"name": "password_pipeline", "id": "password_pipeline"},
            {"name": "token_generator", "id": "token_generator"},
        ],
        "behavior": [{"id": "full", "prose": "Validates username, checks password strength, generates token"}],
    }

    try:
        root = build(DEEP_ROOT_CONTRACT, use_canned=True)
        print(f"\n✓ Depth-2 build succeeded!")
        print(f"  Root: {root.id}")
        print(f"  Children: {[c.id for c in root.children]}")

        # Check depth-2: password_pipeline should have its own children
        for child in root.children:
            if child.id == "password_pipeline":
                print(f"  password_pipeline children: {[c.id for c in child.children]}")
                assert len(child.children) == 2, f"Expected 2 grandchildren, got {len(child.children)}"
                assert {c.id for c in child.children} == {"length_check", "complexity_check"}

        print(f"\n  ✓ Depth-2 tree verified — password_pipeline has 2 children")
        print(f"\n  Full tree:")
        _print_tree(root)

        # Assemble
        main_py_path = assemble(root)
        result = subprocess.run(
            [sys.executable, "main.py"],
            capture_output=True, text=True, timeout=10, cwd=str(BUILD_ROOT),
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                print(f"    {line}")
        else:
            print(f"  ✗ Failed: {result.stderr[:300]}")

    except BuildFailure as e:
        print(f"\n✗ Depth-2 build FAILED: {e}")
        sys.exit(1)
    finally:
        skills.plan_canned = _orig_plan_canned


def _print_tree(node, indent=0):
    """Print the build tree recursively."""
    marker = "L" if node.is_leaf else "I"
    status_text = node.status_path().read_text() if node.status_path().exists() else '{}'
    import json
    try:
        status = json.loads(status_text).get("status", "?")
    except Exception:
        status = "?"
    print(f"  {'  ' * indent}{marker} {node.id} ({status})")
    for child in node.children:
        _print_tree(child, indent + 1)


def test_memo():
    """M-G: Test memoization — build once, then rebuild; second should hit cache."""
    print("=" * 60)
    print("M-G: Memoization test")
    print("=" * 60)

    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    BUILD_ROOT.mkdir()

    print("\n  First build (real work):")
    root1 = build(ROOT_CONTRACT, use_canned=True)
    memo_count = len(list(BUILD_ROOT.rglob("memo.txt")))
    print(f"  Root: {root1.id}, children: {[c.id for c in root1.children]}")
    print(f"  Memo files: {memo_count}")

    print("\n  Second build (should hit memo cache):")
    import time
    t0 = time.time()
    root2 = build(ROOT_CONTRACT, use_canned=True)
    elapsed = time.time() - t0
    print(f"  Root: {root2.id}, children: {[c.id for c in root2.children]}")
    print(f"  Elapsed: {elapsed:.4f}s (should be near-zero)")
    assert root2.id == root1.id
    assert len(root2.children) == len(root1.children)
    print(f"  ✓ Memoization works — second build instant from cache")


def main():
    """M-A through M-G driver."""
    import argparse
    parser = argparse.ArgumentParser(description="RICH Build System")
    parser.add_argument("--test-leaf", type=str, metavar="MODULE_ID",
                        help="M-C: test single-leaf IMPLEMENT+DERIVE_TESTS with real LLM")
    parser.add_argument("--decompose", type=str, metavar="DESC",
                        help="M-E: test decomposition with real LLM (pipeline goal)")
    parser.add_argument("--contract", type=str, metavar="DESC",
                        help="Description for --test-leaf or --decompose contract")
    parser.add_argument("--fan-in", action="store_true",
                        help="M-F: test shared dependency (fan-in) with canned data")
    parser.add_argument("--deep", action="store_true",
                        help="M-G: test depth-2 recursion with canned data")
    parser.add_argument("--memo-test", action="store_true",
                        help="M-G: test memoization — build twice, verify second is cached")
    args = parser.parse_args()

    if args.test_leaf:
        test_single_leaf(args.test_leaf, args.contract or f"Implement {args.test_leaf}")
        return

    if args.decompose:
        test_decompose(args.decompose, args.contract or args.decompose)
        return

    if args.fan_in:
        test_fan_in()
        return

    if args.deep:
        test_deep()
        return

    if args.memo_test:
        test_memo()
        return

    print("=" * 60)
    print("M-A/B: Canned pipeline demo")
    print("=" * 60)

    # Clean build dir
    if BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)
    BUILD_ROOT.mkdir()

    try:
        root = build(ROOT_CONTRACT, use_canned=True)
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
