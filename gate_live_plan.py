"""LIVE-PLAN BATTERY — test the bet, not the machinery.

Every gate that closed so far PINNED the decomposition. This harness runs the
UNMODIFIED build() loop with **live, unpinned PLAN** and the Claude Code subagent
backend for all three skills, then reads the decompositions PLAN actually authors.
The deliverable is a JUDGMENT on decomposition quality (GO / NO-GO / REDIRECT),
not a green checkmark.

Structure (revised after the first runs showed PLAN correctly FLATTENS small,
single-concern goals — see SMALL_GOALS below):

  • BIG_PIPELINE — a genuinely decomposition-scale, multi-stage linear pipeline.
    FULL build + assemble + run. This is the real test: does live PLAN decompose a
    goal that actually warrants it, does the tree self-verify through build() (now
    that Move 1 unblocked internal-node dep_contracts), and does it assemble + run?

  • SMALL_GOALS — three goals deliberately at the scale a competent engineer would
    write as ONE module (a 2-check validator, mean+stddev, a 3-branch router). One
    of them is fan-in-shaped (mean+stddev share a rounder). PLAN-DECISION
    OBSERVATION ONLY: we read what PLAN chooses (expected: leaf — it should NOT
    over-decompose) and, for the fan-in one, whether it would share or duplicate.

Run:  python gate_live_plan.py
"""
import importlib.util
import json
import shutil
import sys
from pathlib import Path

REPO = Path("/home/zaphod/dev/rich")
sys.path.insert(0, str(REPO))

import subagent_skill
subagent_skill.install()
subagent_skill.reset_telemetry()

import build
import skills

STR, NUM, LST, BOOL = "string", "number", "list", "bool"


# ── The decomposition-scale goal (FULL build) ───────────────────────
BIG_PIPELINE = {
    "id": "comment_ingest",
    "description": ("A blog-comment ingestion pipeline. Given raw comment text, produce a "
                    "published-comment record. The pipeline runs four distinct stages in "
                    "order, each consuming the previous stage's output: sanitize, moderate, "
                    "enrich, then format."),
    "interface": {"operations": [
        {"name": "ingest", "inputs": {"text": STR},
         "outputs": {"body": STR, "flagged": BOOL, "word_count": NUM,
                     "reading_time": NUM, "preview": STR}, "errors": []},
    ]},
    "dependencies": [],
    "behavior": [
        {"id": "sanitize", "prose": "Stage 1 (sanitize): remove all HTML tags from the text and collapse every run of whitespace to a single space, trimming the ends."},
        {"id": "moderate", "prose": "Stage 2 (moderate): scan the sanitized text for banned words (use this list: 'spam', 'scam', 'viagra'). Replace each banned word with '***' and set flagged=true if any was found, else flagged=false."},
        {"id": "enrich", "prose": "Stage 3 (enrich): from the moderated body compute word_count (number of whitespace-separated words), reading_time (seconds, ceil of word_count/3), and preview (the first 50 characters of the body)."},
        {"id": "format", "prose": "Stage 4 (format): assemble the final record dict with keys body, flagged, word_count, reading_time, preview."},
        {"id": "order", "prose": "The stages run strictly in order; each stage's output feeds the next."},
    ],
}


# ── Small goals — PLAN-decision observation only ─────────────────────
SMALL_GOALS = [
    {
        "id": "validate_registration",
        "description": ("Validate a user registration form: check the username is acceptable "
                        "and assess the password's strength, reporting whether the "
                        "registration is acceptable."),
        "interface": {"operations": [
            {"name": "validate", "inputs": {"username": STR, "password": STR},
             "outputs": {"username_ok": BOOL, "password_ok": BOOL, "reason": STR}, "errors": []},
        ]},
        "dependencies": [],
        "behavior": [
            {"id": "username_rule", "prose": "username_ok is true when the username is non-empty and at least 3 characters long."},
            {"id": "password_rule", "prose": "password_ok is true only when the password is at least 8 characters long AND contains at least one digit and at least one letter."},
        ],
        "_note": "small validator — leaf is the correct call",
    },
    {
        "id": "number_stats",
        "description": ("Compute summary statistics for a list of numbers: the arithmetic mean "
                        "and the population standard deviation, BOTH rounded to 2 decimals "
                        "using the SAME rounding rule."),
        "interface": {"operations": [
            {"name": "compute", "inputs": {"numbers": LST},
             "outputs": {"mean": NUM, "stddev": NUM}, "errors": []},
        ]},
        "dependencies": [],
        "behavior": [
            {"id": "mean", "prose": "mean is the arithmetic mean of the numbers, rounded to 2 decimals."},
            {"id": "stddev", "prose": "stddev is the population standard deviation, rounded to 2 decimals using the SAME rounding utility used for the mean."},
        ],
        "_note": "fan-in-shaped — if it DID decompose, would it share the rounder?",
    },
    {
        "id": "route_request",
        "description": ("Route a simple HTTP-like request based on its method: GET -> read "
                        "response, POST -> write response, otherwise a 405 error. The "
                        "behavior BRANCHES on the method."),
        "interface": {"operations": [
            {"name": "route", "inputs": {"method": STR, "path": STR},
             "outputs": {"status": NUM, "body": STR}, "errors": []},
        ]},
        "dependencies": [],
        "behavior": [
            {"id": "get", "prose": "A GET request returns status 200 and a read body."},
            {"id": "post", "prose": "A POST request returns status 201 and a write body."},
            {"id": "other", "prose": "Any other method returns status 405 and an error body."},
        ],
        "_note": "branching/out-of-scope shape — does PLAN force a leaf?",
    },
]


def _print_tree(node, indent=1):
    marker = "L" if node.is_leaf else "I"
    st = node.status_path()
    status = json.loads(st.read_text()).get("status") if st.exists() else "?"
    print(f"    {'  ' * indent}{marker} {node.id} ({status})")
    for c in node.children:
        _print_tree(c, indent + 1)


def _describe_decision(decision: dict):
    if decision.get("is_leaf", True):
        print("    PLAN chose: LEAF (no decomposition)")
        return
    children = decision.get("children", [])
    edges = decision.get("edges", [])
    print(f"    PLAN chose: DECOMPOSE into {len(children)} children")
    for c in children:
        cdeps = [d.get("id") for d in c.get("dependencies", [])]
        ops = [o["name"] for o in c.get("interface", {}).get("operations", [])]
        print(f"      • {c['id']:24s} ops={ops}  child.deps={cdeps}")
    if edges:
        print(f"    edges:")
        for e in edges:
            print(f"      {e.get('from')} --[{e.get('name')}]--> {e.get('to')}")


def _shared_dep_analysis(decision: dict):
    if decision.get("is_leaf", True):
        return {}
    child_ids = {c["id"] for c in decision.get("children", [])}
    consumers = {cid: set() for cid in child_ids}
    for e in decision.get("edges", []):
        if e.get("from") in consumers:
            consumers[e["from"]].add(e.get("to"))
    for c in decision.get("children", []):
        for d in c.get("dependencies", []):
            if d.get("id") in consumers:
                consumers[d["id"]].add(c["id"])
    return {cid: cs for cid, cs in consumers.items() if cs}


# ════════════════════════════════════════════════════════════════════
print("=" * 70)
print("LIVE-PLAN BATTERY — unpinned PLAN through the unmodified build() loop")
print("=" * 70)

verdict = []

# ── BIG_PIPELINE: full build + assemble + run ────────────────────────
print(f"\n{'─' * 70}\nDECOMPOSITION-SCALE GOAL — full build (live PLAN authors the tree)\n{'─' * 70}")
print(f"  goal: {BIG_PIPELINE['id']} — four-stage comment ingestion pipeline")

if build.BUILD_ROOT.exists():
    shutil.rmtree(build.BUILD_ROOT)
build.BUILD_ROOT.mkdir(parents=True)

big_ok = False
try:
    root = build.build(BIG_PIPELINE, allow_decompose=True)
    print(f"\n  ✓ build() returned a verified tree:")
    _print_tree(root)

    if root.is_leaf:
        verdict.append("BIG: live PLAN FLATTENED even the 4-stage pipeline to a single leaf — "
                       "strong leaf bias; recursion bet NOT exercised. NO-GO/REDIRECT signal.")
    else:
        nested = [c.id for c in root.children if c.children]
        depthnote = f"depth-2 (nested: {nested})" if nested else "depth-1 (children all leaves)"
        verdict.append(f"BIG: live PLAN DECOMPOSED into {len(root.children)} children at {depthnote}; "
                       f"the tree self-verified through build().")

        main_py = build.assemble(root)
        build_abs = build.BUILD_ROOT.resolve()
        sys.path.insert(0, str(build_abs))
        spec = importlib.util.spec_from_file_location("big_main", str(build_abs / "main.py"))
        m = importlib.util.module_from_spec(spec)
        sys.modules["big_main"] = m
        spec.loader.exec_module(m)
        demo = m.assemble()

        print(f"\n  Deliverable run (live-assembled pipeline):")
        samples = [
            "<p>Hello   world this is a    nice comment</p>",
            "Buy cheap <b>viagra</b> now, this spam scam is great!!!",
        ]
        for raw in samples:
            out = demo.ingest(raw)
            shown = raw if len(raw) < 46 else raw[:43] + "..."
            print(f"    ingest({shown!r}) ->")
            for k, v in (out.items() if isinstance(out, dict) else []):
                print(f"        {k}: {v!r}")
        sample = demo.ingest(samples[0])
        assert set(sample.keys()) >= {"body", "flagged", "word_count", "reading_time", "preview"}, \
            f"missing declared output keys: {sample}"
        big_ok = True
        print(f"\n  ✓ deliverable runs and returns the declared output keys.")

except build.BuildFailure as e:
    print(f"\n  ✗ BIG build FAILED: {e}")
    verdict.append(f"BIG: live build FAILED at the wiring/verify loop — {e}. "
                   f"Decomposition may be authored but not self-verifiable end-to-end.")
except Exception as e:
    import traceback
    traceback.print_exc()
    verdict.append(f"BIG: error — {type(e).__name__}: {e}")


# ── SMALL_GOALS: PLAN-decision observation only ──────────────────────
print(f"\n{'─' * 70}\nSMALL GOALS — PLAN-decision observation (should NOT over-decompose)\n{'─' * 70}")
for g in SMALL_GOALS:
    print(f"\n  • {g['id']}  [{g['_note']}]")
    try:
        dec = build.plan(g, allow_decompose=True)
        _describe_decision(dec)
        if g["id"] == "number_stats" and not dec.get("is_leaf", True):
            shared = _shared_dep_analysis(dec)
            shared_children = [cid for cid, cs in shared.items() if len(cs) > 1]
            if shared_children:
                verdict.append(f"SMALL/{g['id']}: decomposed AND SHARED {shared_children} (recognized shared need).")
            else:
                verdict.append(f"SMALL/{g['id']}: decomposed but did NOT share — likely duplicated rounding.")
        elif dec.get("is_leaf", True):
            verdict.append(f"SMALL/{g['id']}: LEAF (correctly did not over-decompose a small goal).")
        else:
            verdict.append(f"SMALL/{g['id']}: decomposed a small goal into {len(dec.get('children', []))} children "
                           f"(possible OVER-decomposition — inspect).")
    except Exception as e:
        print(f"    ✗ PLAN error: {type(e).__name__}: {e}")
        verdict.append(f"SMALL/{g['id']}: PLAN error — {type(e).__name__}: {e}")


# ── Verdict ──────────────────────────────────────────────────────────
subagent_skill.print_telemetry()
print(f"\n{'=' * 70}\nVERDICT — decomposition quality under live PLAN\n{'=' * 70}")
for v in verdict:
    print(f"  • {v}")
print()
sys.exit(0 if big_ok else 1)
