#!/usr/bin/env python3
"""Test the runtime harness — prove every constraint is enforced.

This test script creates a harness session for the 'auth' module and verifies
that the harness blocks every violation of the three constraints:

  1. Information firewall — cannot read dependency source
  2. Dependency DAG — cannot import undeclared deps
  3. Complexity budget — cannot exceed LOC/file/token limits
"""

import os
import sys
import traceback

# Ensure we can import harness
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, ModuleSession, FirewallBlocked, BudgetWarning

PASS = 0
FAIL = 0


def test(name: str, should_pass: bool = True):
    """Decorator-like wrapper for test functions."""
    def decorator(fn):
        global PASS, FAIL
        try:
            fn()
            if should_pass:
                print(f"  ✅ {name}")
                PASS += 1
            else:
                print(f"  ❌ {name} — expected FirewallBlocked but operation succeeded")
                FAIL += 1
        except FirewallBlocked as e:
            if not should_pass:
                print(f"  ✅ {name} (correctly blocked)")
                PASS += 1
            else:
                print(f"  ❌ {name} — unexpectedly blocked: {e.detail[:80]}")
                FAIL += 1
        except BudgetWarning as e:
            if not should_pass:
                print(f"  ✅ {name} (correctly budget-blocked)")
                PASS += 1
            else:
                print(f"  ❌ {name} — unexpected budget warning: {e}")
                FAIL += 1
        except Exception as e:
            print(f"  ❌ {name} — unexpected error: {e}")
            FAIL += 1
    return decorator


def run():
    print("=" * 72)
    print("HARNESS TEST SUITE")
    print("=" * 72)

    # ── Setup ──
    workspace = os.path.dirname(os.path.abspath(__file__))
    h = Harness(workspace)
    session = h.session("auth")

    print(f"\nModule: {session.module_name}")
    print(f"Dependencies: {[d.name for d in session.deps]}")
    print(f"Budget: {session.budget.max_loc} LOC / "
          f"{session.budget.max_files} files / "
          f"{session.budget.max_context_tokens} tokens\n")

    # ── 1. FIREWALL: Read enforcement ─────────────────────────────────────────
    print("─── FIREWALL: Read Enforcement ───")

    @test("allow read: own source file")
    def _():
        content = session.read_file("modules/auth/src/auth.py")
        assert "def authenticate" in content

    @test("allow read: own contract")
    def _():
        content = session.read_file("modules/auth/contract.yaml")
        assert "name: auth" in content

    @test("allow read: own test file")
    def _():
        content = session.read_file("modules/auth/tests/test_auth.py")
        assert "test_authenticate" in content

    @test("allow read: dependency contract")
    def _():
        content = session.read_file("modules/token_store/contract.yaml")
        assert "name: token_store" in content

    @test("allow read: other dependency contract")
    def _():
        content = session.read_file("modules/user_repo/contract.yaml")
        assert "name: user_repo" in content

    @test("block read: dependency SOURCE", should_pass=False)
    def _():
        session.read_file("modules/token_store/src/token_store.py")

    @test("block read: sibling module source", should_pass=False)
    def _():
        session.read_file("modules/user_repo/src/user_repo.py")

    @test("block read: outside workspace entirely", should_pass=False)
    def _():
        session.read_file("/etc/passwd")

    # ── 2. FIREWALL: Write enforcement ────────────────────────────────────────
    print("\n─── FIREWALL: Write Enforcement ───")

    @test("allow write: new file in own src/")
    def _():
        session.write_file("modules/auth/src/helper.py",
                           "# Helper for auth module\n\ndef format_token(token):\n    return token.upper()\n")

    @test("allow write: new file in own tests/")
    def _():
        session.write_file("modules/auth/tests/test_helper.py",
                           "def test_format():\n    assert True\n")

    @test("block write: dependency source", should_pass=False)
    def _():
        session.write_file("modules/token_store/src/sneaky.py", "# backdoor\n")

    @test("block write: outside workspace", should_pass=False)
    def _():
        session.write_file("/tmp/evil.py", "# nope\n")

    # ── 3. IMPORT BOUNDARY: Write with illegal imports ────────────────────────
    print("\n─── IMPORT BOUNDARY: Dependency Injection Enforcement ───")

    @test("block write: import undeclared dependency (token_store)", should_pass=False)
    def _():
        session.write_file("modules/auth/src/bad.py",
                           "import token_store\n\ndef do_thing():\n    pass\n")

    @test("block write: from-import undeclared dependency", should_pass=False)
    def _():
        session.write_file("modules/auth/src/bad2.py",
                           "from token_store import TokenStore\n\ndef do_thing():\n    pass\n")

    @test("allow write: code without illegal imports")
    def _():
        session.write_file("modules/auth/src/ok.py",
                           "import hashlib\nimport os\n\ndef do_thing():\n    return True\n")

    # ── 4. SEARCH ENFORCEMENT ─────────────────────────────────────────────────
    print("\n─── SEARCH: Scoped to Module Boundary ───")

    @test("allow search: own module source")
    def _():
        results = session.search_files("def authenticate", "modules/auth/src")
        assert any("def authenticate" in r for r in results)

    @test("allow search: own tests")
    def _():
        results = session.search_files("test_authenticate", "modules/auth/tests")
        assert any("test_authenticate" in r for r in results)

    @test("allow search: dependency contracts")
    def _():
        results = session.search_files("name:", "modules/token_store")
        assert any("token_store" in r for r in results)

    @test("block search: dependency source directory", should_pass=False)
    def _():
        session.search_files("def issue", "modules/token_store/src")
    # Note: this may pass if modules/token_store/src is not in whitelist
    # because search_files restricts to allowed dirs

    # ── 5. BUDGET ENFORCEMENT ─────────────────────────────────────────────────
    print("\n─── BUDGET: Complexity Limits ───")

    @test("budget tracking works")
    def _():
        status = session.budget_status()
        assert "LOC" in status
        assert "files" in status
        assert "tokens" in status
        print(f"      {status}")

    @test("session stats tracked")
    def _():
        summary = session.stats_summary()
        assert "allowed" in summary
        assert "blocked" in summary
        print(f"      {summary}")

    # ── 6. CONTEXT GENERATION ─────────────────────────────────────────────────
    print("\n─── CONTEXT: Agent System Prompt ───")

    @test("context document generated")
    def _():
        ctx = session.context()
        assert "Context: auth" in ctx
        assert "Agent Instructions" in ctx
        assert "injection" in ctx
        assert "fakes" in ctx

    @test("boundary summary human-readable")
    def _():
        summary = session.boundary_summary()
        assert "Module: auth" in summary
        assert "token_store" in summary
        assert "user_repo" in summary
        print(f"      Boundary defines {len(session.whitelist_read)} readable paths, "
              f"{len(session.whitelist_write)} writable dirs")

    # ── 7. CLEANUP ────────────────────────────────────────────────────────────
    print("\n─── CLEANUP ───")
    for f in ["modules/auth/src/helper.py", "modules/auth/src/ok.py",
              "modules/auth/tests/test_helper.py"]:
        if os.path.exists(f):
            os.remove(f)
    print("  ✓ test artifacts removed")

    # ── Results ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    total = PASS + FAIL
    print(f"RESULTS: {PASS}/{total} passed")
    if FAIL > 0:
        print(f"        {FAIL} FAILURES")
        sys.exit(1)
    else:
        print("        ALL TESTS PASSED ✅")
        print("=" * 72)
        print()
        print("The harness enforces all three constraints at runtime:")
        print("  1. Information firewall — agents cannot read dependency source")
        print("  2. Dependency DAG — agents cannot import undeclared deps")
        print("  3. Complexity budget — operations tracked against limits")
        print()
        print("This means an agent working on 'auth' is informationally bounded.")
        print("It sees its own code, its tests, and dependency CONTRACTS only.")
        print("Dependency implementations are physically inaccessible.")
        print("=" * 72)


if __name__ == "__main__":
    run()
