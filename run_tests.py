"""run_tests.py — Live end-to-end test runner for RICH.

Runs T0→T6 per live-test-spec.md. Uses test_harness.py for instrumentation.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-...12f7")
os.environ.setdefault("RICH_MODEL", "deepseek/deepseek-chat")

REPO_ROOT = Path(__file__).parent
sys.path.insert(0, str(REPO_ROOT))

# Key comes from environment (set by caller before running)
if not os.environ.get("OPENROUTER_API_KEY"):
    print("STOP: OPENROUTER_API_KEY not set in environment")
    sys.exit(1)

import test_harness as H
import yaml

# ── Helpers ─────────────────────────────────────────────────────────

def verify_d4_no_sibling_imports():
    """Scan all src/*.py in build/ for imports of sibling module ids."""
    build = REPO_ROOT / "build"
    violations = []
    module_ids = set()
    for d in build.iterdir():
        if d.is_dir() and (d / "contract.yaml").exists():
            module_ids.add(d.name)

    for pyfile in build.rglob("src/*.py"):
        content = pyfile.read_text(errors="replace")
        import re
        for mid in module_ids:
            if mid == pyfile.parent.parent.name:
                continue  # self-import is fine
            if re.search(rf"^(import\s+{mid}\b|from\s+{mid}\s+import)", content, re.MULTILINE):
                violations.append(f"{pyfile.relative_to(build)} imports sibling '{mid}'")
    return violations


def check_deliverable_runs(expected_substring=None):
    """Run build/main.py and return (ok, stdout)."""
    build = REPO_ROOT / "build"
    main_py = build / "main.py"
    if not main_py.exists():
        return False, "main.py not found"

    result = subprocess.run(
        [sys.executable, "main.py"],
        capture_output=True, text=True, timeout=15, cwd=str(build),
    )
    ok = result.returncode == 0
    if expected_substring and expected_substring not in result.stdout:
        ok = False
    return ok, result.stdout + result.stderr


# ═════════════════════════════════════════════════════════════════════
# T0 — Smoke
# ═════════════════════════════════════════════════════════════════════

def phase_t0():
    H.install()
    import skills, llm
    from llm import parse_json_response

    contract = {
        "id": "len_fn",
        "description": "Return the length of a string",
        "interface": {"operations": [{"name": "length", "inputs": {"text": "string"}, "outputs": {"len": "int"}, "errors": []}]},
        "dependencies": [],
        "behavior": [{"id": "basic", "prose": "Return string length"}],
    }

    decision = skills.plan(contract, allow_decompose=False)

    # Validate
    errors = []
    if not isinstance(decision, dict):
        errors.append("PLAN returned non-dict")
    if "is_leaf" not in decision:
        errors.append("is_leaf missing")

    if errors:
        return {"status": "FAIL", "errors": errors, "decision": decision}

    return {
        "status": "PASS",
        "decision": decision,
        "calls": H._call_count,
    }


# ═════════════════════════════════════════════════════════════════════
# T1 — Single leaf, full loop
# ═════════════════════════════════════════════════════════════════════

SLUGIFY_CONTRACT = {
    "id": "slugify",
    "description": "Convert string to URL-safe slug: lowercase, trim, replace runs of non-alphanumeric with single hyphens",
    "interface": {"operations": [{"name": "slugify", "inputs": {"text": "string"}, "outputs": {"slug": "string"}, "errors": []}]},
    "dependencies": [],
    "behavior": [
        {"id": "lowercase", "prose": "Output is lowercase"},
        {"id": "no_whitespace", "prose": "No leading/trailing whitespace"},
        {"id": "hyphenated", "prose": "Runs of non-alphanumeric chars replaced with single hyphens"},
    ],
}


def phase_t1():
    H.install()
    import build as B

    try:
        root = B.build(SLUGIFY_CONTRACT, allow_decompose=False, use_canned=False)
        status = json.loads(root.status_path().read_text())

        # Read the generated source and tests
        src = root.src_path() / "slugify.py"
        tests = root.tests_path() / "test_slugify.py"
        src_content = src.read_text() if src.exists() else ""
        tests_content = tests.read_text() if tests.exists() else ""

        # Check: right function name?
        has_correct_fn = "def slugify" in src_content

        # Check: tests use correct operation name?
        tests_use_right_name = "slugify" in tests_content

        # Check deliverable
        ok, stdout = check_deliverable_runs()
        H.flush_logs()

        return {
            "status": "PASS" if status.get("status") == "verified" else "FAIL",
            "final_status": status.get("status"),
            "has_correct_fn": has_correct_fn,
            "tests_use_right_name": tests_use_right_name,
            "calls": H._call_count,
        }
    except B.BuildFailure as e:
        H.flush_logs()
        return {"status": "FAIL", "reason": str(e), "calls": H._call_count}


# ═════════════════════════════════════════════════════════════════════
# T2 — Repeat ×5
# ═════════════════════════════════════════════════════════════════════

def phase_t2():
    H.install()
    import build as B

    results = []
    for i in range(5):
        print(f"\n  --- T2 run {i+1}/5 ---")
        H.reset_stats()
        H.archive_build()
        try:
            root = B.build(SLUGIFY_CONTRACT, allow_decompose=False, use_canned=False)
            status = json.loads(root.status_path().read_text())
            results.append({"run": i+1, "status": status.get("status"), "calls": H._call_count})
        except B.BuildFailure as e:
            results.append({"run": i+1, "status": "FAILED", "reason": str(e)[:200], "calls": H._call_count})
        H.flush_logs()

    successes = sum(1 for r in results if r["status"] == "verified")
    return {
        "status": "PASS" if successes >= 4 else "FAIL",
        "successes": successes,
        "total": 5,
        "details": results,
    }


# ═════════════════════════════════════════════════════════════════════
# T3 — Depth-1 decomposition
# ═════════════════════════════════════════════════════════════════════

PIPELINE_CONTRACT = {
    "id": "text_processor",
    "description": "Process raw text: (1) strip HTML tags, (2) collapse whitespace, (3) truncate to 200 chars",
    "interface": {"operations": [{"name": "process", "inputs": {"raw": "string"}, "outputs": {"result": "string"}, "errors": []}]},
    "dependencies": [],
    "behavior": [
        {"id": "strip_html", "prose": "Remove all HTML tags like <p>, <br>, </div>"},
        {"id": "collapse_ws", "prose": "Collapse multiple spaces/newlines into single space"},
        {"id": "truncate", "prose": "Truncate to 200 characters"},
    ],
}

def phase_t3():
    H.install()
    import build as B

    try:
        root = B.build(PIPELINE_CONTRACT, allow_decompose=True, use_canned=False)
        status = json.loads(root.status_path().read_text())

        # Check decomposition
        child_count = len(root.children)
        child_ids = [c.id for c in root.children]

        # Check deliverable
        ok, stdout = check_deliverable_runs()

        # Check D4
        d4_violations = verify_d4_no_sibling_imports()

        H.flush_logs()

        return {
            "status": "PASS" if status.get("status") == "verified" else "FAIL",
            "decomposed": not root.is_leaf,
            "child_count": child_count,
            "children": child_ids,
            "deliverable_runs": ok,
            "d4_violations": d4_violations,
            "calls": H._call_count,
        }
    except B.BuildFailure as e:
        H.flush_logs()
        return {"status": "FAIL", "reason": str(e), "calls": H._call_count}


# ═════════════════════════════════════════════════════════════════════
# T4 — Fan-in / shared dependency
# ═════════════════════════════════════════════════════════════════════

FANIN_CONTRACT = {
    "id": "report_generator",
    "description": "Produce a summary report: (a) formatted mean and (b) formatted standard deviation, using a shared number-rounding utility",
    "interface": {"operations": [{"name": "report", "inputs": {"numbers": "list<float>"}, "outputs": {"summary": "string"}, "errors": []}]},
    "dependencies": [],
    "behavior": [
        {"id": "mean", "prose": "Calculate and format the mean to 2 decimal places"},
        {"id": "stddev", "prose": "Calculate and format the standard deviation to 2 decimal places"},
        {"id": "shared_rounding", "prose": "Both formatters use the same rounding utility"},
    ],
}

def phase_t4():
    H.install()
    import build as B

    try:
        root = B.build(FANIN_CONTRACT, allow_decompose=True, use_canned=False)
        status = json.loads(root.status_path().read_text())

        # Check for shared deps: any child referenced by 2+ other children?
        dep_refs = {}
        for child in root.children:
            for dep in child.dependencies:
                dep_id = dep.get("id", dep.get("name", ""))
                dep_refs.setdefault(dep_id, []).append(child.id)

        shared = {k: v for k, v in dep_refs.items() if len(v) > 1}
        all_deps_have_single_ref = {k: v for k, v in dep_refs.items() if len(v) == 1}

        # Check assembly: how many times is each shared dep constructed?
        main_py = REPO_ROOT / "build" / "main.py"
        construct_counts = {}
        if main_py.exists():
            import re
            content = main_py.read_text()
            for dep_id in dep_refs:
                count = len(re.findall(rf"= construct_{dep_id}\(\)", content))
                construct_counts[dep_id] = count

        shared_once = all(construct_counts.get(k, 0) <= 1 for k in shared)

        ok, stdout = check_deliverable_runs()
        d4_violations = verify_d4_no_sibling_imports()
        H.flush_logs()

        return {
            "status": "PASS" if status.get("status") == "verified" else "FAIL",
            "shared_deps": shared,
            "all_single_ref_deps": all_deps_have_single_ref,
            "construct_counts": construct_counts,
            "shared_once": shared_once,
            "deliverable_runs": ok,
            "d4_violations": d4_violations,
            "calls": H._call_count,
        }
    except B.BuildFailure as e:
        H.flush_logs()
        return {"status": "FAIL", "reason": str(e), "calls": H._call_count}


# ═════════════════════════════════════════════════════════════════════
# T5 — Depth-2+ with backtracking
# ═════════════════════════════════════════════════════════════════════

SIGNUP_CONTRACT = {
    "id": "validate_signup",
    "description": "Validate email and password: check email format, check password strength (length + character-class), aggregate failures",
    "interface": {"operations": [{"name": "validate", "inputs": {"email": "string", "password": "string"}, "outputs": {"ok": "bool", "errors": "list<string>"}, "errors": []}]},
    "dependencies": [],
    "behavior": [
        {"id": "email_format", "prose": "Email must have valid format (contains @, has domain)"},
        {"id": "password_length", "prose": "Password must be at least 8 characters"},
        {"id": "password_complexity", "prose": "Password must contain at least one digit and one letter"},
        {"id": "aggregate", "prose": "All failures are collected into a single errors list"},
    ],
}

def _max_depth(node):
    if node.is_leaf:
        return 1
    return 1 + max((_max_depth(c) for c in node.children), default=0)


def phase_t5():
    H.install()
    import build as B

    try:
        root = B.build(SIGNUP_CONTRACT, allow_decompose=True, use_canned=False)
        status = json.loads(root.status_path().read_text())

        depth = _max_depth(root)
        child_count = len(root.children)
        child_ids = [c.id for c in root.children]

        ok, stdout = check_deliverable_runs()
        H.flush_logs()

        return {
            "status": "PASS" if status.get("status") == "verified" and depth >= 2 else "FAIL",
            "tree_depth": depth,
            "child_count": child_count,
            "children": child_ids,
            "deliverable_runs": ok,
            "calls": H._call_count,
        }
    except B.BuildFailure as e:
        H.flush_logs()
        return {"status": "FAIL", "reason": str(e), "calls": H._call_count}


# ═════════════════════════════════════════════════════════════════════
# T6 — Stress / honesty probe
# ═════════════════════════════════════════════════════════════════════

STRESS_CONTRACT = {
    "id": "safe_divide_and_log",
    "description": "Return a/b, but if b is zero return a sentinel value (-1) and skip normal logging path",
    "interface": {"operations": [{"name": "safe_divide_and_log", "inputs": {"a": "float", "b": "float"}, "outputs": {"result": "float"}, "errors": []}]},
    "dependencies": [],
    "behavior": [
        {"id": "divide", "prose": "Return a / b when b != 0"},
        {"id": "zero_guard", "prose": "When b == 0, return -1 and do NOT log the operation"},
    ],
}

def phase_t6():
    H.install()
    import build as B

    try:
        root = B.build(STRESS_CONTRACT, allow_decompose=True, use_canned=False)
        status = json.loads(root.status_path().read_text())

        # Was it forced to leaf or decomposed?
        is_leaf = root.is_leaf
        child_count = len(root.children)

        # Does the source actually handle the zero case?
        src_file = root.src_path() / "safe_divide_and_log.py"
        handles_zero = False
        if src_file.exists():
            content = src_file.read_text()
            handles_zero = "== 0" in content or "!= 0" in content or "b == 0" in content or "zero" in content.lower()

        ok, stdout = check_deliverable_runs()
        H.flush_logs()

        return {
            "status": "PROBE",
            "is_leaf": is_leaf,
            "child_count": child_count,
            "handles_zero_case": handles_zero,
            "deliverable_runs": ok,
            "finding": (
                "Forced to leaf (no decomposition)" if is_leaf
                else f"Decomposed into {child_count} children despite conditional logic"
            ),
            "calls": H._call_count,
        }
    except B.BuildFailure as e:
        H.flush_logs()
        return {"status": "PROBE", "reason": str(e), "calls": H._call_count,
                "finding": f"Build failed: {str(e)[:200]}"}


# ═════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════

ALL_PHASES = [
    ("T0", phase_t0),
    ("T1", phase_t1),
    ("T2", phase_t2),
    ("T3", phase_t3),
    ("T4", phase_t4),
    ("T5", phase_t5),
    ("T6", phase_t6),
]


def main():
    # Verify key
    from llm import is_available, RICH_MODEL
    if not is_available():
        print("STOP: OPENROUTER_API_KEY not set. Cannot run.")
        sys.exit(1)

    print(f"Model: {RICH_MODEL}")
    print(f"Phases: {' → '.join(p[0] for p in ALL_PHASES)}")
    print()

    results = {}
    for name, fn in ALL_PHASES:
        result = H.run_phase(name, fn)
        results[name] = result

        # Stop on hard FAIL (except T6 which is PROBE)
        if result.get("status") == "FAIL" and name != "T6":
            print(f"\n  STOP: {name} FAILED. Not proceeding to harder phases.")
            break

    # Write summary
    summary = {"model": RICH_MODEL, "results": results}
    (H.TESTLOG / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nSummary written to {H.TESTLOG / 'summary.json'}")


if __name__ == "__main__":
    main()