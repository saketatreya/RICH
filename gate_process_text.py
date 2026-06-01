"""§5 P4 GATE — close-the-decomposition-gap fixes, validated end-to-end.

Runs the `process_text` decomposition (strip_html → collapse_ws → truncate)
through the UNMODIFIED build() loop using the Claude Code subagent backend for
the two real skills under test:

  • Fix 1 — internal-node DERIVE_TESTS: the root's tests must be class-based with
    FAKE dependencies (assume-guarantee). If Fix 1 is wrong the root cannot
    self-verify and build() raises BuildFailure here.
  • Fix 2 — assemble(): build/main.py must instantiate the verified wiring class
    and run the real pipeline. We drive it on the brief's two cases.

PLAN is PINNED (the decomposition is authored as the seed architecture — its
real-LLM form was already validated in Phase 1). This isolates the two fixes and
makes the gate deterministic. IMPLEMENT and DERIVE_TESTS are REAL subagent calls.
"""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/zaphod/dev/rich")
sys.path.insert(0, str(REPO))

import subagent_skill
subagent_skill.install()
subagent_skill.reset_telemetry()

import build
import skills

# ── Authored decomposition (pinned PLAN) ────────────────────────────
STR = "string"

ROOT = {
    "id": "process_text",
    "description": "Clean a snippet of HTML into plain text: strip tags, collapse "
                   "whitespace, then truncate. A linear three-stage pipeline.",
    "interface": {"operations": [
        {"name": "process", "inputs": {"text": STR},
         "outputs": {"result": STR}, "errors": []},
    ]},
    "dependencies": [
        {"name": "strip_html", "id": "strip_html"},
        {"name": "collapse_ws", "id": "collapse_ws"},
        {"name": "truncate", "id": "truncate"},
    ],
    "behavior": [
        {"id": "order", "prose": "Stages run in order: strip_html, then collapse_ws, then truncate."},
        {"id": "result", "prose": "process(text).result is the truncated, whitespace-collapsed, tag-free text."},
    ],
}

CHILDREN = [
    {
        "id": "strip_html",
        "description": "Remove every HTML tag from a string, leaving only text content.",
        "interface": {"operations": [
            {"name": "strip", "inputs": {"text": STR}, "outputs": {"result": STR}, "errors": []},
        ]},
        "dependencies": [],
        "behavior": [
            {"id": "remove_tags", "prose": "Delete every <...> tag. Keep the text between tags exactly, do not add or remove other whitespace."},
        ],
    },
    {
        "id": "collapse_ws",
        "description": "Collapse runs of whitespace to a single space and trim the ends.",
        "interface": {"operations": [
            {"name": "collapse", "inputs": {"text": STR}, "outputs": {"result": STR}, "errors": []},
        ]},
        "dependencies": [],
        "behavior": [
            {"id": "collapse", "prose": "Replace every run of whitespace (spaces, tabs, newlines) with one space."},
            {"id": "trim", "prose": "Strip leading and trailing whitespace."},
        ],
    },
    {
        "id": "truncate",
        "description": "Truncate a string to at most 200 characters.",
        "interface": {"operations": [
            {"name": "truncate", "inputs": {"text": STR}, "outputs": {"result": STR}, "errors": []},
        ]},
        "dependencies": [],
        "behavior": [
            {"id": "max200", "prose": "Return at most the first 200 characters of the input; shorter strings are unchanged."},
        ],
    },
]

PINNED_DECISION = {
    "is_leaf": False,
    "children": CHILDREN,
    "edges": [
        {"from": "strip_html", "to": "collapse_ws", "name": "stripped"},
        {"from": "collapse_ws", "to": "truncate", "name": "collapsed"},
    ],
}

# ── Pin PLAN (build imported `plan` into its own namespace) ─────────
_real_plan = build.plan

def pinned_plan(contract, allow_decompose=False):
    if contract["id"] == "process_text" and allow_decompose:
        return PINNED_DECISION
    # children are leaves
    return {"is_leaf": True}

build.plan = pinned_plan

# ── Run the gate ────────────────────────────────────────────────────
# GATE_FRESH=1 forces a full subagent rebuild; otherwise build()'s memoization
# reuses an already-verified build/ (fast re-runs of the assemble/run checks).
import os as _os
if _os.environ.get("GATE_FRESH") == "1" and build.BUILD_ROOT.exists():
    shutil.rmtree(build.BUILD_ROOT)
build.BUILD_ROOT.mkdir(exist_ok=True)

print("=" * 64)
print("§5 P4 GATE — process_text via UNMODIFIED build() + subagent backend")
print("=" * 64)

try:
    root = build.build(ROOT, allow_decompose=True)
except build.BuildFailure as e:
    print(f"\n✗ GATE FAILED at build(): {e}")
    subagent_skill.print_telemetry()
    sys.exit(1)
finally:
    build.plan = _real_plan

print(f"\n✓ Fix 1 — build() self-verified the decomposition:")
print(f"    root: {root.id}  is_leaf={root.is_leaf}  children={[c.id for c in root.children]}")
for c in [root] + root.children:
    st = c.status_path()
    import json as _j
    status = _j.loads(st.read_text()).get("status") if st.exists() else "?"
    print(f"      {c.id:14s} {status}")

# Show the root's generated (internal-node) test — proof of Fix 1
root_test = (root.tests_path() / f"test_{root.id}.py")
print(f"\n  Root internal-node test ({root_test.name}) — first lines:")
for ln in root_test.read_text().splitlines()[:24]:
    print(f"    | {ln}")

# ── Fix 2: assemble + run the deliverable ───────────────────────────
print(f"\n{'=' * 64}\n  Fix 2 — assemble() + run deliverable\n{'=' * 64}")
main_py = build.assemble(root)
print(f"  Generated: {main_py}")
print(f"\n  main.py construct + fold:")
content = Path(main_py).read_text()
for ln in content.splitlines():
    if ln.startswith("def construct_") or ln.startswith("# Wiring") or ln.startswith("# Module") \
       or ln.strip().startswith(("process_text =", "strip_html =", "collapse_ws =", "truncate =", "return process_text")):
        print(f"    | {ln}")

# Load assembled pipeline and drive the brief's gate cases. Put the (absolute)
# build dir on sys.path — exactly the flat layout RICH runs main.py against — so
# the per-node `import <id>` calls resolve to the flat module files there.
build_abs = build.BUILD_ROOT.resolve()
sys.path.insert(0, str(build_abs))
spec = importlib.util.spec_from_file_location("gate_main", str(build_abs / "main.py"))
m = importlib.util.module_from_spec(spec)
sys.modules["gate_main"] = m
spec.loader.exec_module(m)
demo = m.assemble()

cases = [
    ("<html><body><h1>Title</h1><p>Some   body   text.</p></body></html>", "TitleSome body text."),
    ("<p>" + "x" * 250 + "</p>", "x" * 200),
    ("<p>Hello, <b>world</b>!</p>", "Hello, world!"),
    ("<div>  word  </div>", "word"),
]
print(f"\n  Deliverable run (real assembled pipeline):")
all_ok = True
for raw, expected in cases:
    got = demo.process(raw)["result"]
    ok = got == expected
    all_ok = all_ok and ok
    shown = raw if len(raw) < 42 else raw[:39] + "..."
    print(f"    [{'OK' if ok else 'XX'}] process({shown!r}) -> {got!r}")
    if not ok:
        print(f"          expected {expected!r}")

subagent_skill.print_telemetry()
print("\n" + ("=" * 64))
print(">>> GATE GREEN: Fix 1 (internal-node tests self-verify) + Fix 2 "
      "(assemble runs the verified pipeline) both pass." if all_ok
      else ">>> GATE RED: deliverable output mismatch.")
sys.exit(0 if all_ok else 1)
