"""SMALL-GOAL PLAN OBSERVATION — does live PLAN over-decompose?

PLAN-decision observation ONLY (no build, does NOT touch build/). Three goals
deliberately at the scale a competent engineer writes as ONE module:
  • validate_registration — a 2-check validator
  • number_stats          — mean + stddev sharing one rounding rule (fan-in-shaped)
  • route_request         — a 3-branch method router (branching/out-of-scope shape)

Expected: LEAF for all three (PLAN should not over-decompose). For number_stats,
if it DID decompose we observe whether it would SHARE the rounder or DUPLICATE it.

Run:  python gate_small_goals.py
"""
import json
import sys
from pathlib import Path

REPO = Path("/home/zaphod/dev/rich")
sys.path.insert(0, str(REPO))

import subagent_skill
subagent_skill.install()
subagent_skill.reset_telemetry()

import build

STR, NUM, LST, BOOL = "string", "number", "list", "bool"

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


def _describe(decision):
    if decision.get("is_leaf", True):
        print("    PLAN chose: LEAF (no decomposition)")
        return
    children = decision.get("children", [])
    edges = decision.get("edges", [])
    print(f"    PLAN chose: DECOMPOSE into {len(children)} children")
    for c in children:
        ops = [o["name"] for o in c.get("interface", {}).get("operations", [])]
        print(f"      • {c['id']:24s} ops={ops}  child.deps={c.get('dependencies', [])}")
    for e in edges:
        print(f"      {e.get('from')} --[{e.get('name')}]--> {e.get('to')}")


def _shared_dep_analysis(decision):
    child_ids = {c["id"] for c in decision.get("children", [])}
    consumers = {cid: set() for cid in child_ids}
    for e in decision.get("edges", []):
        if e.get("from") in consumers:
            consumers[e["from"]].add(e.get("to"))
    return {cid: cs for cid, cs in consumers.items() if len(cs) > 1}


print("=" * 70)
print("SMALL-GOAL PLAN OBSERVATION — should NOT over-decompose")
print("=" * 70)

verdict = []
for g in SMALL_GOALS:
    print(f"\n  • {g['id']}  [{g['_note']}]")
    try:
        dec = build.plan(g, allow_decompose=True)
        _describe(dec)
        if dec.get("is_leaf", True):
            verdict.append(f"{g['id']}: LEAF (correctly did not over-decompose).")
        elif g["id"] == "number_stats":
            shared = _shared_dep_analysis(dec)
            verdict.append(f"{g['id']}: DECOMPOSED; shared children: {list(shared) or 'NONE (likely duplicated rounding)'}.")
        else:
            verdict.append(f"{g['id']}: DECOMPOSED into {len(dec.get('children', []))} children (inspect for over-decomposition).")
    except Exception as e:
        print(f"    ✗ PLAN error: {type(e).__name__}: {e}")
        verdict.append(f"{g['id']}: PLAN error — {type(e).__name__}: {e}")

subagent_skill.print_telemetry()
print(f"\n{'=' * 70}\nSMALL-GOAL VERDICT\n{'=' * 70}")
for v in verdict:
    print(f"  • {v}")
