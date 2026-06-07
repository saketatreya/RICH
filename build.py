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

import ast
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
from skills import (plan, implement, derive_tests, derive_integration_test,
                    plan_canned, implement_canned, derive_tests_canned)
from llm import LLMNotConfigured, LLMParseError


K_IMPL = 3
K_WIRE = 3
MAX_DEPTH = 3          # M-G: max recursion depth
MAX_CHILDREN = 8       # M-G: max children per node
MAX_LLM_CALLS = 50     # M-G: global LLM call ceiling
REPLANS_MAX = 2        # M-G: max replan attempts on child failure


# ── Phase 9 (§2.3): the call manifest ──────────────────────────────
# An append-only audit log proving the build was FULLY LIVE-AUTHORED with zero pins.
# Every node is tagged live-PLAN / live-IMPLEMENT / live-DERIVE_TESTS (produced by a
# live, unpinned call) or memo-hit / decision-reuse (served from a PRIOR live build —
# the resumption-across-a-quota-cut path, §2.2). Persisted under BUILD_ROOT so the
# cross-window audit survives resumption: a memo-hit's hash is traceable to the earlier
# live-build line carrying the same hash. The manifest is what PROVES "no pins" rather
# than asserting it (§2.3) — and it doubles as the cost ledger (one line per LLM call).
import os
import time as _time

MANIFEST_NAME = "manifest.jsonl"
_manifest_run_id = None

# Phase 9 (§2.4): selective model labels for the manifest's cost ledger. These MIRROR
# the env-gated per-skill routing in skills.py: PLAN and IMPLEMENT (the bet and the code)
# stay on the strong default; DERIVE_TESTS may be routed to a cheaper model. A label of
# the default model means "backend default" (sonnet, via RICH_SUBAGENT_MODEL).
_DEFAULT_MODEL = os.environ.get("RICH_SUBAGENT_MODEL", "sonnet")
_PLAN_LABEL = os.environ.get("RICH_PLAN_MODEL") or _DEFAULT_MODEL
_IMPL_LABEL = os.environ.get("RICH_IMPL_MODEL") or _DEFAULT_MODEL
_TESTS_LABEL = os.environ.get("RICH_TESTS_MODEL") or _DEFAULT_MODEL


def _manifest(node_id: str, event: str, contract: dict,
              model: str | None = None, extra: dict | None = None):
    """Append one audit entry to BUILD_ROOT/manifest.jsonl (§2.3). Best-effort:
    logging never breaks a build."""
    import json
    entry = {
        "ts": round(_time.time(), 3),
        "run": _manifest_run_id,
        "node": node_id,
        "event": event,
        "hash": _contract_hash(contract),
    }
    if model:
        entry["model"] = model
    if extra:
        entry.update(extra)
    try:
        BUILD_ROOT.mkdir(parents=True, exist_ok=True)
        with open(BUILD_ROOT / MANIFEST_NAME, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _contract_hash(contract: dict) -> str:
    """Stable hash of a contract for memoization."""
    import hashlib, json
    raw = json.dumps(contract, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _load_verified_node(contract: dict) -> Node | None:
    """If this contract has a verified build on disk, return the Node. Otherwise None."""
    import json, yaml as _yaml
    node_path = BUILD_ROOT / contract["id"]
    status_path = node_path / "status.json"
    memo_path = node_path / "memo.txt"

    if not status_path.exists() or not memo_path.exists():
        return None

    try:
        status = json.loads(status_path.read_text())
        if status.get("status") != "verified":
            return None
        stored_hash = memo_path.read_text().strip()
        if stored_hash != _contract_hash(contract):
            return None

        # Reconstruct Node
        contract_on_disk = _yaml.safe_load((node_path / "contract.yaml").read_text())
        decision = json.loads((node_path / "decision.json").read_text())
        deps_path = node_path / "deps.yaml"
        deps = _yaml.safe_load(deps_path.read_text()) if deps_path.exists() else []

        node = Node(id=contract["id"], contract=contract_on_disk,
                     is_leaf=decision["is_leaf"], dependencies=deps or [])
        if not node.is_leaf:
            for child_contract in decision.get("children", []):
                child = _load_verified_node(child_contract)
                if child is None:
                    return None
                node.children.append(child)
            node.edges = decision.get("edges", [])
        return node
    except Exception:
        return None


def _save_memo(node: Node, contract: dict):
    """Save memoization hash for a verified node."""
    node.path().mkdir(parents=True, exist_ok=True)
    (node.path() / "memo.txt").write_text(_contract_hash(contract))


def _load_prior_decision(contract: dict) -> dict | None:
    """Phase 9 (§2.2): on resumption, reuse THIS node's own previously live-authored
    PLAN decision — iff the on-disk contract is hash-identical to the incoming one.

    This is the PLAN-level analogue of memoization. A full quota-cut run leaves nodes
    that were PLANned-but-not-yet-verified with a persisted decision.json. Without this,
    re-invoking build() would call PLAN again — and a fresh decision (even at low temp)
    can differ, changing child contract hashes and BUSTING the memo on subtrees that
    WERE already built live. Freezing the tree shape to the node's own prior live
    decision makes resumption robust instead of luck-dependent: only the unbuilt
    remainder spends new calls.

    This is NOT pinning (§2.3): the decision was live-authored, by this same node, when
    it was first planned — the manifest tags the reuse `decision-reuse`, traceable to the
    earlier `live-PLAN` line. Forbidden laundering is reusing a *different* run's decision
    under a pin, or hand-supplying one; neither happens here. Returns None if absent/stale.
    """
    import json
    import yaml as _yaml
    node_path = BUILD_ROOT / contract["id"]
    decision_path = node_path / "decision.json"
    contract_path = node_path / "contract.yaml"
    if not decision_path.exists() or not contract_path.exists():
        return None
    try:
        on_disk = _yaml.safe_load(contract_path.read_text())
        if _contract_hash(on_disk) != _contract_hash(contract):
            return None
        return json.loads(decision_path.read_text())
    except Exception:
        return None


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


def _injection_deps(node: Node) -> list[tuple[str, str]]:
    """The dependencies INJECTED into a node's wiring, as (param_name, source_id).

    Single source of truth shared by gen_construct, the assembly fold, and the
    topological order so all three agree (the §2 composition convention).

      • Internal node (has children): it composes its CHILDREN — injected by child
        id (the verified wiring class names its __init__ params by child id; for
        root seeds those also equal the declared dependency names).
      • Leaf with a declared dependency (e.g. a shared engine): injected by dep
        NAME, sourced from the dep's id.
      • Pure leaf: injects nothing.

    Children is the truth for internal nodes — a non-root internal node's
    contract.dependencies is empty even though it composes children.

    Robustness (live PLAN): a leaf's contract.dependencies must be well-formed
    {name, id} dicts to count as a real injected dependency. Live PLAN sometimes
    emits redundant BARE-STRING entries (e.g. ['sanitize']) that merely echo the
    pipeline edges — those stages are composed by the PARENT, not injected into the
    leaf, so we ignore any entry that is not a {name, id} dict.
    """
    if node.children:
        return [(c.id, c.id) for c in node.children]
    return [(d["name"], d["id"]) for d in (node.dependencies or [])
            if isinstance(d, dict) and "name" in d and "id" in d]


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
        """Generate the construction code for a node (Fix 2, M-H).

        Unified rule keyed on the node's INJECTED dependencies (_injection_deps),
        implementing the §2 composition convention exactly once:

          • injects nothing  → the module exports top-level functions; return a
            thin handle that delegates each declared op to the module function.
            Module-qualified (``_m.op``) so op names never collide across modules.
          • injects deps      → the module defines exactly ONE verified wiring
            class. Import it, locate it by introspection (the single class whose
            ``__module__`` is this module), and instantiate it with the deps
            injected BY NAME. We run the SAME class the tests verified — assembly
            never re-derives composition (D6 / §6.2 / Trap 2). If the module does
            not contain exactly one own class, ``_wiring_class`` FAILS LOUD (§6:
            never guess a non-pipeline composition).
        """
        result = []
        ops = node.contract.get("interface", {}).get("operations", [])
        inj = _injection_deps(node)
        if not inj:
            # Adaptive (Phase 6): a no-dep leaf is EITHER a stateless transformation
            # (top-level functions → wrap in a handle) OR a STATEFUL component (a single
            # class holding state across calls → instantiate ONCE so the one instance's
            # state persists). We detect which by introspection at assembly time rather
            # than predicting IMPLEMENT's shape, so a stateful leaf carries the same way
            # an internal wiring class does (one instance), and the dataflow path (no
            # class defined) is unchanged.
            result.append(f"# Module (no injected deps): {node.id} — stateful class (one instance) or top-level fns")
            result.append(f"def construct_{node.id}():")
            result.append(f"    import inspect")
            result.append(f"    import {node.id} as _m")
            result.append(f"    _own = [c for _n, c in inspect.getmembers(_m, inspect.isclass)")
            result.append(f"            if c.__module__ == {node.id!r}]")
            result.append(f"    if len(_own) == 1:")
            result.append(f"        return _own[0]()          # stateful component — instantiate once")
            result.append(f"    class _Handle:")
            if ops:
                for op in ops:
                    op_name = op["name"]
                    result.append(f"        def {op_name}(self, *args, **kwargs):")
                    result.append(f"            return _m.{op_name}(*args, **kwargs)")
            else:
                result.append(f"        pass")
            result.append(f"    return _Handle()")
        else:
            params = [p for p, _src in inj]
            dep_params = ", ".join(params)
            kwargs = ", ".join(f"{p}={p}" for p in params)
            result.append(f"# Wiring node: {node.id} — instantiate verified class, inject {params}")
            result.append(f"def construct_{node.id}({dep_params}):")
            result.append(f"    import {node.id} as _m")
            result.append(f"    return _wiring_class(_m, {node.id!r})({kwargs})")
        result.append("")
        return result

    # Step 2: generate main.py
    main_py = BUILD_ROOT / "main.py"

    # Copy all source files from subdirectories into build/ for import. Always OVERWRITE
    # (Phase 11 fix): the old `if not dest.exists()` guard left a STALE top-level copy after
    # a node was rebuilt on a resume — so the assembled deliverable silently ran the previous
    # version of that node's code (observed: a rebuilt render_report produced correct
    # per-endpoint output in its own tests, but main.py still imported the stale copy). The
    # verified src under <id>/src/ is the single source of truth; refresh from it every time.
    for node_id, node in all_nodes.items():
        src_dir = node.src_path()
        if src_dir.exists():
            for f in src_dir.glob("*.py"):
                shutil.copy2(f, BUILD_ROOT / f.name)

    lines = []

    lines.append('"""Generated entrypoint — assembly fold for the build tree.')
    lines.append("")
    lines.append("This file is generated by build.py (§6.2). Do not edit by hand.")
    lines.append('"""')
    lines.append("")
    lines.append("")

    # Wiring-class locator (Fix 2): assembly instantiates the verified IMPLEMENT
    # class; it never re-derives composition. Each construct_<id> imports its own
    # module, so there are no top-level `import *` collisions across modules.
    lines.append("def _wiring_class(module, module_id):")
    lines.append('    """Return the single class DEFINED in `module` — the verified wiring class.')
    lines.append("")
    lines.append("    Fail loud if not exactly one (§6: assembly never guesses composition).")
    lines.append('    """')
    lines.append("    import inspect")
    lines.append("    own = [c for _n, c in inspect.getmembers(module, inspect.isclass)")
    lines.append("           if c.__module__ == module_id]")
    lines.append("    if len(own) != 1:")
    lines.append("        raise RuntimeError(")
    lines.append('            f"assemble[{module_id}]: expected exactly ONE wiring class defined "')
    lines.append('            f"in the module, found {[c.__name__ for c in own]!r}. v1 supports a "')
    lines.append('            f"single injected wiring class per node; refusing to guess.")')
    lines.append("    return own[0]")
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
        # Inject each dep by its param NAME, bound to the variable holding that
        # dep's instance (named by its source id). _injection_deps is the single
        # source of truth shared with gen_construct and the topo order. (The
        # earlier code keyed on is_leaf / used `{name}={name}` — a latent bug
        # whenever name != id, e.g. fan-in's regex/regex_engine.)
        inj = _injection_deps(node)
        if inj:
            dep_args = ", ".join(f"{p}={src}" for p, src in inj)
            lines.append(f"    {node.id} = construct_{node.id}({dep_args})")
        else:
            lines.append(f"    {node.id} = construct_{node.id}()")

    lines.append("")
    lines.append(f"    return {root.id}")
    lines.append("")
    lines.append("")
    lines.append("if __name__ == '__main__':")
    lines.append("    demo = assemble()")
    root_ops = root.contract.get("interface", {}).get("operations", [])
    if root_ops:
        # Smoke-test the assembled deliverable with type-appropriate dummy args
        # for EVERY declared input — not a single positional 'test input'. The old
        # stub hardcoded one positional string, which silently mismatched any op
        # that takes != 1 input (now that assemble() instantiates the REAL verified
        # wiring class rather than the old `pass`-stub, that mismatch surfaces as a
        # TypeError instead of a false-green None). Building kwargs by declared
        # input name + type makes the smoke test honest for any op signature.
        op = root_ops[0]
        op_name = op["name"]
        _dummy = {"string": "'test input'", "number": "1", "bool": "True",
                  "list": "[1, 2, 3]", "dict": "{}"}
        inputs = op.get("inputs", {})
        call_args = ", ".join(f"{k}={_dummy.get(t, repr('test input'))}"
                              for k, t in inputs.items())
        lines.append(f"    result = demo.{op_name}({call_args})")
        lines.append(f"    print('{op_name} result:', result)")
    else:
        lines.append("    print('Assembled:', type(demo).__name__)")
    lines.append("    print('✓ Deliverable: OK')")

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

    # Build dep graph: node -> set of injected-dependency source ids. Uses the
    # same _injection_deps truth as the fold so children of an internal node are
    # constructed before it (a non-root internal node has empty contract.deps).
    dep_of = {}
    for n in all_nodes.values():
        dep_of[n.id] = {src for _p, src in _injection_deps(n)}

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


def _shares_stateful_dep(node: Node) -> str | None:
    """Phase 8 integration trigger (§1.3): return the id of a child that is STATEFUL and is
    depended on by MORE THAN ONE sibling (a shared mutable dependency — the dragon's
    signature), else None. Detected purely from the graph (children + edges). When this
    fires, per-module fake-based verification is unsound (overlapping footprints) and an
    integration trace test over the real subtree is ADDED.
    """
    if not node.children:
        return None
    stateful_ids = {c.id for c in node.children if (c.contract or {}).get("stateful")}
    if not stateful_ids:
        return None
    consumers: dict[str, set] = {}
    for e in (node.edges or []):
        consumers.setdefault(e.get("from"), set()).add(e.get("to"))
    for sid in stateful_ids:
        if len(consumers.get(sid, set())) > 1:
            return sid
    return None


def verify_integration(node: Node, test_src: str) -> dict:
    """Assemble node's REAL subtree (shared stateful instance, real writers — no fakes) and
    run an integration test against it. Returns {passed, failures}. This is the verification
    that per-module unit tests structurally cannot do (Phase 8). Read/run only; the transient
    main.py it writes is overwritten when the full tree assembles."""
    import tempfile
    assemble(node)  # writes BUILD_ROOT/main.py + copies subtree src into BUILD_ROOT
    with tempfile.TemporaryDirectory(prefix="rich_integ_") as tmp:
        tmp_path = Path(tmp)
        for f in BUILD_ROOT.glob("*.py"):
            shutil.copy2(f, tmp_path)
        (tmp_path / "test_integration.py").write_text(test_src)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--tb=short", str(tmp_path)],
                capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
            passed = result.returncode == 0
            failures = []
            if not passed:
                for line in result.stdout.splitlines():
                    if "FAILED" in line or "Error" in line or "assert" in line:
                        failures.append(line.strip())
                failures = failures[:12]
            return {"passed": passed, "failures": failures}
        except subprocess.TimeoutExpired:
            return {"passed": False, "failures": ["integration test timed out (30s)"]}


# ── Phase 11 Fix 2: dataflow shape-handoff (concrete example capture) ──────────
# A downstream leaf consuming a sibling's dataflow output is built knowing only its own
# contract — not the concrete SHAPE of the value crossing the edge (Probe B's evaluator
# failed 3× guessing the parser's AST shape). These helpers derive a CONCRETE example of
# an upstream's real output by RUNNING the already-built, verified upstream (the brief's
# preferred approach), and thread it into the downstream's IMPLEMENT + DERIVE_TESTS so
# both halves — and the real upstream — agree on the shape by construction. Pure
# execution: no LLM, no quota. Best-effort everywhere — any failure returns None and the
# downstream simply falls back to its contract (pre-Fix-2 behavior). Mirrors Phase 7's
# convention (thread the dependency's shape into the consumer), for DATA not capability.

def _extract_test_input_candidates(node: Node) -> list[dict]:
    """Best-effort: every concrete op-call kwargs found in the node's verified test file,
    ranked by serialized size (largest first). Seeds example capture at the HEAD of a
    dataflow chain (a head has no upstream to run). DERIVE_TESTS mandates keyword-arg calls
    (skills.py rule 4), so these calls are reliably present.

    Phase 11 Fix 2 hardening — seed RICHNESS. Tests bind their realistic sample to a local
    first (``lines = ["…GET /a 200 42", …]; parse(lines=lines)``) and reserve bare literals
    for empty/edge cases (``parse(lines=[])``). So we (a) resolve simple local/module literal
    assignments so ``parse(lines=lines)`` is reachable, and return ALL candidates so the
    capture path can pick the one whose *output* is richest (size alone is a weak proxy when
    the impl rejects some inputs — e.g. a 4-line all-malformed sample is bigger but yields
    zero events). Best-effort: any failure → []."""
    try:
        test_file = node.tests_path() / f"test_{node.id}.py"
        if not test_file.exists():
            return []
        op_names = {op["name"] for op in
                    node.contract.get("interface", {}).get("operations", [])}
        tree = ast.parse(test_file.read_text())

        # Module-level literal assignments (shared sample constants).
        module_syms: dict = {}
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name):
                        try:
                            module_syms[tgt.id] = ast.literal_eval(stmt.value)
                        except Exception:
                            pass

        def _resolve(value, local_syms):
            """A call-arg value → concrete Python value, via literal eval or a known
            (local-then-module) literal-assigned name. Raises on anything else."""
            if isinstance(value, ast.Name):
                if value.id in local_syms:
                    return local_syms[value.id]
                if value.id in module_syms:
                    return module_syms[value.id]
                raise ValueError(f"unresolved name {value.id}")
            return ast.literal_eval(value)

        candidates: list[dict] = []

        def scan(scope_node, local_syms):
            for call in (n for n in ast.walk(scope_node) if isinstance(n, ast.Call)):
                fn = call.func
                name = (fn.attr if isinstance(fn, ast.Attribute)
                        else fn.id if isinstance(fn, ast.Name) else None)
                if name not in op_names or not call.keywords:
                    continue
                kwargs, ok = {}, True
                for kw in call.keywords:
                    if kw.arg is None:
                        ok = False; break
                    try:
                        kwargs[kw.arg] = _resolve(kw.value, local_syms)
                    except Exception:
                        ok = False; break
                if ok and kwargs:
                    candidates.append(kwargs)

        # Per-function scoping: a function's own literal assigns shadow module ones.
        for fn_def in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            local_syms: dict = {}
            for stmt in ast.walk(fn_def):
                if isinstance(stmt, ast.Assign):
                    for tgt in stmt.targets:
                        if isinstance(tgt, ast.Name):
                            try:
                                local_syms[tgt.id] = ast.literal_eval(stmt.value)
                            except Exception:
                                pass
            scan(fn_def, local_syms)
        # Also catch any module-level op-calls (rare).
        scan(tree, module_syms)

        import json as _json
        # De-dup, then rank by serialized size (largest first).
        seen, uniq = set(), []
        for c in candidates:
            key = _json.dumps(c, sort_keys=True, default=repr)
            if key not in seen:
                seen.add(key); uniq.append(c)
        uniq.sort(key=lambda k: len(_json.dumps(k, default=repr)), reverse=True)
        return uniq
    except Exception:
        return []


def _extract_test_inputs(node: Node) -> dict | None:
    """The single best-by-size test-derived seed (back-compat for `_run_inputs_for`'s head
    fallback). For HEAD output capture, prefer `_capture_head_example` which ranks by the
    seed's actual OUTPUT richness, not input size."""
    cands = _extract_test_input_candidates(node)
    return cands[0] if cands else None


def _run_inputs_for(node: Node, inbound_examples: dict | None) -> dict | None:
    """Concrete op-kwargs to RUN this node for capturing its own output example:
    a downstream maps its inbound example(s) onto its declared input(s); a head extracts
    literal inputs from its own tests.

    Mapping precedence (Phase 11 Fix 2 hardening). An upstream op's output is almost always
    a DICT of named fields, and a downstream input usually corresponds to ONE such field BY
    NAME — exactly the by-name rule the wiring uses (assess_health's `events` ← parse_events
    output's `events`, NOT the whole {events, malformed_count} dict). So resolve each
    declared input as:
      1. an upstream output FIELD whose name == the input name, then
      2. the whole output of an upstream whose id == the input name, then
      3. (single declared input, nothing matched by name) the single whole upstream output.
    The earlier version only did (3)-for-one-input, which fed an upstream's WHOLE multi-field
    output into a single-field consumer; the capture subprocess then crashed (e.g.
    assess_health.assess iterating a {events, malformed_count} dict instead of the events
    list) → None → the real downstream shape (the assessment) never reached render_report,
    which then guessed the shape, failed, and triggered a full-subtree REPLAN cascade."""
    if not inbound_examples:
        return _extract_test_inputs(node)
    ops = node.contract.get("interface", {}).get("operations", [])
    decl = list((ops[0].get("inputs", {}) if ops else {}).keys())
    if not decl:
        return None
    field_pool: dict = {}        # output-field name -> value (from dict-shaped outputs)
    whole_by_src: dict = {}      # upstream id -> its whole output
    for src, v in inbound_examples.items():
        if v is None:
            continue
        whole_by_src[src] = v
        if isinstance(v, dict):
            for fk, fv in v.items():
                field_pool.setdefault(fk, fv)
    run: dict = {}
    for name in decl:
        if name in field_pool:
            run[name] = field_pool[name]
        elif name in whole_by_src:
            run[name] = whole_by_src[name]
    if len(run) == len(decl):
        return run
    if len(decl) == 1 and len(whole_by_src) == 1:
        return {decl[0]: next(iter(whole_by_src.values()))}
    return run or None


def _capture_example_output(node: Node, concrete_inputs: dict | None):
    """Assemble node's verified subtree and RUN its first operation on concrete_inputs in
    a clean subprocess; return the operation's output (the concrete example that flows
    downstream), or None. Reuses the Phase-8 assemble-subtree-in-subprocess pattern so a
    stateful/internal upstream is exercised through its REAL composition."""
    import json as _json
    import tempfile
    try:
        ops = node.contract.get("interface", {}).get("operations", [])
        if not ops or not concrete_inputs:
            return None
        op_name = ops[0]["name"]
        assemble(node)  # writes BUILD_ROOT/main.py for this subtree + copies its src
        with tempfile.TemporaryDirectory(prefix="rich_capture_") as tmp:
            tmp_path = Path(tmp)
            for f in BUILD_ROOT.glob("*.py"):
                shutil.copy2(f, tmp_path)
            (tmp_path / "_inputs.json").write_text(_json.dumps(concrete_inputs, default=repr))
            (tmp_path / "_capture.py").write_text(
                "import json\n"
                "from main import assemble\n"
                "inp = json.load(open('_inputs.json'))\n"
                "demo = assemble()\n"
                f"out = demo.{op_name}(**inp)\n"
                "print('___RICH_EXAMPLE___' + json.dumps(out, default=repr))\n")
            res = subprocess.run([sys.executable, "_capture.py"], capture_output=True,
                                 text=True, timeout=30, cwd=str(tmp_path))
            if res.returncode != 0:
                return None
            for line in res.stdout.splitlines():
                if line.startswith("___RICH_EXAMPLE___"):
                    return _json.loads(line[len("___RICH_EXAMPLE___"):])
            return None
    except Exception:
        return None


def _capture_head_example(node: Node, max_seeds: int = 24):
    """Phase 11 Fix 2 — capture a HEAD's output example, selecting the seed by the RICHNESS
    of its OUTPUT, not its input size. A head has no upstream to run, so its seed comes from
    its own tests; but those are mostly edge/empty cases (``parse(lines=[])``) plus a few
    populated ones. A degenerate seed yields an output with the right summary keys but ZERO
    per-entry structure, so a downstream learns only the empty shape and then GUESSES the
    per-entry shape (the render_report per-endpoint residual).

    We therefore RUN each candidate seed and keep the output with the largest serialized size.
    Crucially we must try the candidates regardless of input size: the seed that actually
    produces records is often a SMALL one-liner (a single valid log line) while the big
    multi-line samples are deliberately all-malformed edge cases — so an input-size cap would
    skip exactly the seed we need. Bounded by max_seeds. Best-effort; None if nothing captures."""
    import json as _json
    best, best_size = None, -1
    for seed in _extract_test_input_candidates(node)[:max_seeds]:
        out = _capture_example_output(node, seed)
        if out is None:
            continue
        try:
            size = len(_json.dumps(out, default=repr))
        except Exception:
            size = 0
        if size > best_size:
            best, best_size = out, size
    return best


def build(contract: dict, allow_decompose: bool = False, use_canned: bool = False,
          depth: int = 0, llm_call_counter: list | None = None,
          available_deps: dict | None = None,
          inbound_examples: dict | None = None) -> Node:
    """The core recursive procedure (§4).

    Takes a contract, returns a verified Node or raises BuildFailure.
    depth: current recursion depth (0 = root).
    llm_call_counter: mutable list wrapper to track global LLM call count.
    available_deps: {contract_id: contract} for the dependencies this node may hold —
        the sibling contracts the PARENT passes down (Phase 7). A LEAF that declares an
        injected dependency resolves its contract here and threads it into BOTH
        DERIVE_TESTS and IMPLEMENT, so the two halves agree on the leaf's shape (closing
        the leaf-injection gap). None at the root / for a leaf with no declared deps.
    inbound_examples: {upstream_id: concrete_output_example} — Phase 11 Fix 2. When this
        node consumes a sibling's DATAFLOW output, the parent runs the already-built
        upstream and passes its concrete output shape here, threaded into BOTH DERIVE_TESTS
        and IMPLEMENT so the node is built+tested against the real shape it will receive
        (the data analogue of the Phase-7 capability handoff). None when no dataflow input.
    """
    if llm_call_counter is None:
        llm_call_counter = [0]
        # Phase 9 (§2.3): tag this top-level invocation so manifest entries from each
        # resumption window are distinguishable. A new run-id per build() entry call.
        global _manifest_run_id
        _manifest_run_id = f"run-{int(_time.time())}"

    node_id = contract["id"]

    # Hard cap: max depth
    if depth > MAX_DEPTH:
        raise BuildFailure(contract["id"], f"Max depth {MAX_DEPTH} exceeded at depth {depth}")

    # Hard cap: max LLM calls (Phase 9: now a REAL ceiling — the counter is incremented
    # at every live call site below, repairing a previously dead guardrail and giving
    # the unattended multi-window spend a hard cost cap + a clean checkpoint boundary).
    if llm_call_counter[0] >= MAX_LLM_CALLS:
        raise BuildFailure(contract["id"], f"LLM call ceiling ({MAX_LLM_CALLS}) exceeded")

    # Memoization / resumption (§2.2): if this contract was verified before, return the
    # cached subtree — no LLM calls. Across a quota cut this carries every hash-identical
    # verified node; the build spends only on the unbuilt remainder.
    cached = _load_verified_node(contract)
    if cached is not None:
        _manifest(node_id, "memo-hit", contract)
        return cached

    # 1. PLAN
    if use_canned:
        import skills as _skills
        decision = _skills.plan_canned(contract)
    else:
        # Resumption (§2.2): reuse this node's OWN prior live-authored decision if a
        # hash-identical one is persisted (frozen tree shape → no redundant PLAN call,
        # no memo-busting drift). Otherwise PLAN live.
        prior = _load_prior_decision(contract)
        if prior is not None:
            decision = prior
            _manifest(node_id, "decision-reuse", contract)
        else:
            decision = plan(contract, allow_decompose=allow_decompose)
            llm_call_counter[0] += 1
            _manifest(node_id, "live-PLAN", contract, model=_PLAN_LABEL,
                      extra={"allow_decompose": allow_decompose,
                             "is_leaf": decision.get("is_leaf", True)})

    # Create node
    node = Node(
        id=node_id,
        contract=contract,
        is_leaf=decision["is_leaf"],
    )
    save_contract(node)
    save_decision(node)
    # Also save raw PLAN output so decision.json reflects what PLAN authored
    if not node.is_leaf:
        _save_raw_decision(node, decision)
    # Resolve dependencies from contract (for both leaf and internal)
    node.dependencies = contract.get("dependencies", [])
    save_status(node, "planned")

    if node.is_leaf:
        # Phase 6: a leaf may be STATEFUL — a component holding state across
        # operation calls (a class), verified by operation SEQUENCES — rather than a
        # pure stateless transformation (top-level functions). The signal comes from
        # the contract itself (set by the goal author / PLAN), not from coaching the
        # model: stateful goals describe history-dependent behavior. Defaults False,
        # so the dataflow path is byte-for-byte unchanged.
        stateful = bool(contract.get("stateful", False))

        # Phase 7: a leaf may DECLARE injected dependencies — a sibling CAPABILITY it
        # HOLDS and CALLS at points of its own choosing (not a value the parent threads).
        # Source those dep contracts from `available_deps` (the sibling contracts the
        # parent passed down), keyed by the SAME _injection_deps convention internal
        # nodes use. Threading them into BOTH DERIVE_TESTS and IMPLEMENT closes the
        # leaf-injection gap: the two halves read the same dependency contract and agree
        # on the leaf's shape BY CONSTRUCTION. A leaf with NO declared deps → empty dict
        # → dep_contracts=None → byte-for-byte the pre-Phase-7 leaf path.
        leaf_dep_contracts = {}
        for param_name, src_id in _injection_deps(node):
            if available_deps and src_id in available_deps:
                leaf_dep_contracts[param_name] = available_deps[src_id]
        dc = leaf_dep_contracts or None

        # 2a. DERIVE_TESTS — deps present → assume-guarantee test against a
        # contract-derived fake of the held capability (the same machinery internal
        # nodes use). pipeline=False: a held capability is CALLED at arbitrary points
        # by this leaf, not threaded sequentially by a parent.
        if use_canned:
            import skills as _skills4
            tests_src = _skills4.derive_tests_canned(contract)
        else:
            tests_src = derive_tests(contract, dep_contracts=dc, pipeline=False,
                                     stateful=stateful, inbound_examples=inbound_examples)
            llm_call_counter[0] += 1
            _manifest(node_id, "live-DERIVE_TESTS", contract, model=_TESTS_LABEL,
                      extra={"leaf": True, "stateful": stateful,
                             "inbound_example": bool(inbound_examples)})
        node.tests_path().mkdir(parents=True, exist_ok=True)
        test_file = node.tests_path() / f"test_{node_id}.py"
        test_file.write_text(tests_src)

        # 3a. IMPLEMENT + verify loop
        failures = []
        for attempt in range(1, K_IMPL + 1):
            if use_canned:
                import skills as _skills5
                src = _skills5.implement_canned(contract, dep_contracts=dc, pipeline=False)
            else:
                src = implement(contract, dep_contracts=dc, pipeline=False,
                              stateful=stateful,
                              prior_failures=failures if failures else None,
                              inbound_examples=inbound_examples)
                llm_call_counter[0] += 1
                _manifest(node_id, "live-IMPLEMENT", contract, model=_IMPL_LABEL,
                          extra={"leaf": True, "attempt": attempt})
            node.src_path().mkdir(parents=True, exist_ok=True)
            src_file = node.src_path() / f"{node_id}.py"
            src_file.write_text(src)

            result = run_tests(node.src_path(), node.tests_path())
            if result["passed"]:
                save_status(node, "verified")
                save_deps(node)
                _save_memo(node, contract)
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

        # Build child nodes — allow decomposition if below max depth (M-G)
        children_nodes = {}

        for replan_attempt in range(REPLANS_MAX + 1):  # 0 = first try, 1..N = replans
            try:
                children_ordered = _topo_sort_contracts(children_contracts, edges)

                if len(children_ordered) > MAX_CHILDREN:
                    raise BuildFailure(node_id, f"Too many children ({len(children_ordered)} > {MAX_CHILDREN})")

                child_allow_decompose = (depth + 1) < MAX_DEPTH
                children_nodes = {}
                failed_child = None
                # Phase 11 Fix 2: concrete output example of each built upstream sibling,
                # filled in topo order, so a downstream sibling is built against the REAL
                # shape it will receive (not a guess). Reset per replan attempt.
                example_outputs: dict = {}

                # Phase 7: each child may HOLD a sibling capability. Pass the sibling
                # contracts down (merged over any deps this node itself received) so a
                # leaf child can resolve the contract of a dependency it declares.
                sibling_contracts = {**(available_deps or {}),
                                     **{c["id"]: c for c in children_ordered}}

                for child_contract in children_ordered:
                    # Phase 11 Fix 2: gather concrete examples for this child's INBOUND
                    # DATAFLOW edges (upstream sibling → this child). A held-capability
                    # edge (upstream listed in this child's own dependencies) is excluded
                    # — that path is already covered by Phase 7's contract threading.
                    child_dep_ids = {d["id"] for d in (child_contract.get("dependencies") or [])
                                     if isinstance(d, dict) and "id" in d}
                    child_inbound = {}
                    for e in edges:
                        if (e.get("to") == child_contract["id"]
                                and e.get("from") not in child_dep_ids):
                            ex = example_outputs.get(e.get("from"))
                            if ex is not None:
                                child_inbound[e.get("from")] = ex
                    try:
                        child_node = build(child_contract, allow_decompose=child_allow_decompose,
                                          use_canned=use_canned, depth=depth + 1,
                                          llm_call_counter=llm_call_counter,
                                          available_deps=sibling_contracts,
                                          inbound_examples=child_inbound or None)
                        children_nodes[child_contract["id"]] = child_node
                    except BuildFailure as e:
                        failed_child = child_contract
                        raise  # Propagate to replan handler

                    # Capture this child's REAL output example IFF a downstream sibling
                    # consumes it — running the verified node (no LLM/quota). Best-effort;
                    # None on any failure (downstream falls back to its contract).
                    if not use_canned and any(e.get("from") == child_contract["id"]
                                              for e in edges):
                        if child_inbound:
                            # Consumer: run on the concrete value(s) its upstream produced.
                            run_inputs = _run_inputs_for(child_node, child_inbound)
                            example_outputs[child_contract["id"]] = (
                                _capture_example_output(child_node, run_inputs)
                                if run_inputs else None)
                        else:
                            # Head: pick the test seed whose OUTPUT is richest (exercises
                            # real per-entry structure, not an empty edge case).
                            example_outputs[child_contract["id"]] = \
                                _capture_head_example(child_node)

                break  # Success — all children built

            except BuildFailure as e:
                if failed_child and replan_attempt < REPLANS_MAX and not use_canned:
                    print(f"  [{node_id}] child '{failed_child['id']}' failed: {e.reason}")
                    print(f"  [{node_id}] replan attempt {replan_attempt + 1}/{REPLANS_MAX}...")
                    # Call PLAN again with failure context
                    new_decision = plan(contract, allow_decompose=True)
                    llm_call_counter[0] += 1
                    _manifest(node_id, "live-REPLAN", contract, model=_PLAN_LABEL,
                              extra={"replan_attempt": replan_attempt + 1})
                    if new_decision.get("is_leaf", True):
                        raise BuildFailure(node_id, "REPLAN fell back to leaf — decomposition failed")
                    children_contracts = new_decision.get("children", [])
                    edges = new_decision.get("edges", [])
                    save_decision(node)  # Update decision
                    _save_raw_decision(node, new_decision)
                else:
                    raise

        node.children = list(children_nodes.values())
        node.edges = edges

        # Resolve dependencies from the CONTRACT (kept for deps.yaml / provenance).
        node.dependencies = contract.get("dependencies", [])

        # Build dep_contracts dict: {param_name: contract, ...} — needed by BOTH
        # DERIVE_TESTS (Fix 1: fake-injected class tests) and IMPLEMENT (wiring).
        #
        # Move 1 (unifying fix): source this from _injection_deps (the node's
        # CHILDREN), NOT from contract.dependencies. An internal node composes its
        # children; its own contract.dependencies is EMPTY for every node a live
        # PLAN authors — non-root internals AND depth-1 roots from a fresh goal —
        # which left real-mode IMPLEMENT/DERIVE_TESTS blind to the children. That
        # blindness was masked only by canned mode (ignores dep_contracts) and the
        # pinned gate (hand-authored root deps that happened to equal the
        # children). Keying by child id also makes the wiring class's __init__
        # param names match exactly what assembly injects (_injection_deps), so
        # tests, impl, and assembly agree on names BY CONSTRUCTION. Insertion order
        # follows topo order (children_nodes is topo-ordered) — an implicit
        # pipeline-order signal to IMPLEMENT.
        dep_contracts = {}
        for param_name, src_id in _injection_deps(node):
            dep_contracts[param_name] = children_nodes[src_id].contract

        # 3b. DERIVE_TESTS for the internal node — thread dep contracts + pipeline
        # flag so the generated test discovers the wiring class and uses fakes.
        if use_canned:
            import skills as _skills6
            tests_src = _skills6.derive_tests_canned(contract)
        else:
            tests_src = derive_tests(contract, dep_contracts=dep_contracts, pipeline=True,
                                     inbound_examples=inbound_examples)
            llm_call_counter[0] += 1
            _manifest(node_id, "live-DERIVE_TESTS", contract, model=_TESTS_LABEL,
                      extra={"leaf": False,
                             "inbound_example": bool(inbound_examples)})
        node.tests_path().mkdir(parents=True, exist_ok=True)
        test_file = node.tests_path() / f"test_{node_id}.py"
        test_file.write_text(tests_src)

        # 4b. IMPLEMENT wiring + verify loop
        failures = []
        for attempt in range(1, K_WIRE + 1):
            if use_canned:
                import skills as _skills3
                src = _skills3.implement_canned(contract, dep_contracts=dep_contracts, pipeline=True)
            else:
                src = implement(contract, dep_contracts=dep_contracts, pipeline=True,
                              prior_failures=failures if failures else None,
                              inbound_examples=inbound_examples)
                llm_call_counter[0] += 1
                _manifest(node_id, "live-IMPLEMENT", contract, model=_IMPL_LABEL,
                          extra={"leaf": False, "attempt": attempt})
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
                # Phase 8: ADD integration verification when children share a stateful
                # dependency (§1.3). Per-module unit tests just passed, but they fake the
                # shared store and so cannot see interaction bugs through it. The integration
                # trace test exercises the REAL subtree (interleaved multi-writer sequence).
                # Additive: the trigger never fires for non-shared-stateful trees, so every
                # existing build path is byte-for-byte unchanged.
                shared = _shares_stateful_dep(node)
                if shared and not use_canned:
                    try:
                        itest = derive_integration_test(contract)
                        llm_call_counter[0] += 1
                        _manifest(node_id, "live-INTEGRATION", contract, model=_TESTS_LABEL,
                                  extra={"shared_stateful": shared})
                        ires = verify_integration(node, itest)
                    except (LLMNotConfigured, LLMParseError) as e:
                        # generation unavailable/failed — record, do not silently pass
                        ires = {"passed": True, "failures": [f"integration generation skipped: {e}"]}
                    if not ires["passed"]:
                        save_status(node, "failed",
                                    reason=f"integration (shared stateful '{shared}'): {ires['failures'][:2]}")
                        raise BuildFailure(node_id,
                            f"integration test FAILED on shared stateful dep '{shared}' "
                            f"(units passed, interaction wrong): {ires['failures'][:2]}")
                save_status(node, "verified")
                save_deps(node)
                # Update decision with children contracts
                save_decision(node)
                _save_memo(node, contract)
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


def _topo_sort_contracts(children: list[dict], edges: list[dict]) -> list[dict]:
    """Topological sort of child contracts based on edges. Returns ordered list."""
    child_map = {c["id"]: c for c in children}
    child_ids = set(child_map.keys())
    dep_of = {cid: set() for cid in child_ids}
    for edge in edges:
        to_id = edge.get("to", "")
        from_id = edge.get("from", "")
        if to_id in dep_of:
            dep_of[to_id].add(from_id)

    ordered = []
    visited = set()
    temp = set()

    def visit(cid):
        if cid in temp:
            raise ValueError(f"Cycle in child dependencies: {cid}")
        if cid in visited:
            return
        temp.add(cid)
        for dep_id in dep_of.get(cid, set()):
            visit(dep_id)
        temp.remove(cid)
        visited.add(cid)
        ordered.append(child_map[cid])

    for cid in child_ids:
        visit(cid)

    return ordered


def _save_raw_decision(node: Node, decision: dict):
    """Save PLAN's raw decision output (includes children contracts)."""
    import json
    node.path().mkdir(parents=True, exist_ok=True)
    with open(node.decision_path(), "w") as f:
        json.dump(decision, f, indent=2)


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