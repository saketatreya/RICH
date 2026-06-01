"""PHASE 4 (focused) — ONE goal, fresh, UNBROKEN, UNPINNED.

Runs only publish_article (the depth-2 + fan-in forcing goal that already demonstrated
it makes live PLAN nest). Fresh build/, no pin, one execution: PLAN authors AND the
engine carries it to a deliverable in the same run — the seam Phase 3 never crossed.
Budget-conscious: one goal (~20 calls) to fit a single quota window.

Run:  python gate_forcing_one.py
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
from gate_forcing import PUBLISH_ARTICLE, _print_tree, _max_depth, _has_nested_child, _fan_in_nodes


def _read_decisions():
    """Even if the build dies mid-way, report what PLAN actually authored on disk."""
    out = {}
    for d in sorted(build.BUILD_ROOT.glob("*/decision.json")):
        try:
            dec = json.loads(d.read_text())
            if not dec.get("is_leaf", True):
                out[d.parent.name] = [c["id"] for c in dec.get("children", [])]
        except Exception:
            pass
    return out


print("=" * 70)
print("PHASE 4 (focused) — publish_article, fresh / unbroken / unpinned")
print("=" * 70)

if build.BUILD_ROOT.exists():
    shutil.rmtree(build.BUILD_ROOT)
build.BUILD_ROOT.mkdir(parents=True)

ok = False
try:
    root = build.build(PUBLISH_ARTICLE, allow_decompose=True)
    print("\n  build() returned a verified tree:")
    _print_tree(root)
    depth = _max_depth(root)
    nested = _has_nested_child(root) or depth >= 2
    fan_in = _fan_in_nodes(root)
    print(f"\n  depth={depth}  nested(depth-2+)={nested}  fan-in={list(fan_in) or 'none'}")

    build.assemble(root)
    build_abs = build.BUILD_ROOT.resolve()
    sys.path.insert(0, str(build_abs))
    spec = importlib.util.spec_from_file_location("pa_main", str(build_abs / "main.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["pa_main"] = m
    spec.loader.exec_module(m)
    demo = m.assemble()

    raw = "# My Trip\nWe walked far. The day was long and the path was hard. See http://x.io"
    out = demo.publish(raw=raw)
    print(f"\n  deliverable run — publish(raw=...) ->")
    for k, v in (out.items() if isinstance(out, dict) else [("result", out)]):
        print(f"    {k}: {v!r}")
    assert set(out.keys()) >= {"title", "body", "analysis", "published"}, f"missing keys: {out}"
    ok = True
    print(f"\n  ✓ UNBROKEN: live PLAN authored a depth-{depth} "
          f"{'nested+fan-in' if (nested and fan_in) else 'tree'} decomposition AND the engine "
          f"carried it to a running deliverable in ONE execution.")

except build.BuildFailure as e:
    print(f"\n  ✗ BuildFailure: {e}")
    print(f"  PLAN authored (from disk): {json.dumps(_read_decisions(), indent=2)}")
except Exception as e:
    import traceback
    traceback.print_exc()
    print(f"  PLAN authored (from disk): {json.dumps(_read_decisions(), indent=2)}")

subagent_skill.print_telemetry()
print(f"\n{'=' * 70}\nFOCUSED PHASE 4:", "UNBROKEN PASS" if ok else "INCOMPLETE", f"\n{'=' * 70}")
sys.exit(0 if ok else 1)
