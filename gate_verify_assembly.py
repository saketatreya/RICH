"""VERIFY THE ASSEMBLY FIX — finish the live-authored comment_ingest tree.

Context: in the live-PLAN battery, unpinned PLAN ALREADY authored a sensible
4-stage decomposition of comment_ingest (sanitize -> moderate -> enrich ->
format) and all four child leaves built + self-verified live. That genuine
artifact is on disk (build/comment_ingest/decision.json + 4 verified children
with memos). The ONLY step never proven end-to-end is the root internal node's
wiring + the deterministic assemble() after the `_injection_deps` string-filter
fix (run #3 crashed at assemble; run #5 hit a 429 at the root IMPLEMENT).

This harness does NOT wipe build/. It reuses the genuine live decomposition by
pinning PLAN to the on-disk decision.json, so the four children MEMO-HIT (zero
LLM calls — hashes already confirmed to match) and only the root's DERIVE_TESTS
+ IMPLEMENT wiring run freshly. Then it assembles + runs the deliverable.

This is a deterministic-fix verification, not a re-test of decomposition
quality — that judgment is read separately from the live-authored decision.json.

Run:  python gate_verify_assembly.py
"""
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path("/home/zaphod/dev/rich")
sys.path.insert(0, str(REPO))

import subagent_skill
subagent_skill.install()
subagent_skill.reset_telemetry()

import build

# The decomposition-scale goal (same id as the on-disk tree).
BIG_PIPELINE = {
    "id": "comment_ingest",
    "description": ("A blog-comment ingestion pipeline. Given raw comment text, produce a "
                    "published-comment record. The pipeline runs four distinct stages in "
                    "order, each consuming the previous stage's output: sanitize, moderate, "
                    "enrich, then format."),
    "interface": {"operations": [
        {"name": "ingest", "inputs": {"text": "string"},
         "outputs": {"body": "string", "flagged": "bool", "word_count": "number",
                     "reading_time": "number", "preview": "string"}, "errors": []},
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

# Pin PLAN to the genuine live-authored decision for comment_ingest. Every other
# node either memo-hits before plan() is reached (the children) or never calls
# plan in this run. This reuses live PLAN's output without re-paying for it.
_ON_DISK = json.loads((build.BUILD_ROOT / "comment_ingest" / "decision.json").read_text())
_real_plan = build.plan


def _pinned_plan(contract, allow_decompose=False):
    if contract["id"] == "comment_ingest":
        print("    [plan pinned to genuine live-authored decision.json]")
        return _ON_DISK
    return _real_plan(contract, allow_decompose=allow_decompose)


build.plan = _pinned_plan


def _print_tree(node, indent=1):
    marker = "L" if node.is_leaf else "I"
    st = node.status_path()
    status = json.loads(st.read_text()).get("status") if st.exists() else "?"
    print(f"    {'  ' * indent}{marker} {node.id} ({status})")
    for c in node.children:
        _print_tree(c, indent + 1)


print("=" * 70)
print("VERIFY ASSEMBLY FIX — finish live-authored comment_ingest, assemble + run")
print("=" * 70)

ok = False
try:
    root = build.build(BIG_PIPELINE, allow_decompose=True)
    print("\n  build() returned a verified tree:")
    _print_tree(root)

    main_py = build.assemble(root)
    print(f"\n  assemble() wrote {main_py}")

    build_abs = build.BUILD_ROOT.resolve()
    sys.path.insert(0, str(build_abs))
    spec = importlib.util.spec_from_file_location("big_main", str(build_abs / "main.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["big_main"] = m
    spec.loader.exec_module(m)
    demo = m.assemble()

    print("\n  Deliverable run (live-assembled pipeline):")
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
    ok = True
    print("\n  ✓ deliverable assembles, runs, and returns the declared output keys.")

except build.BuildFailure as e:
    print(f"\n  ✗ build FAILED: {e}")
except Exception:
    import traceback
    traceback.print_exc()

subagent_skill.print_telemetry()
print(f"\n{'=' * 70}")
print("ASSEMBLY-FIX VERIFICATION:", "PASS" if ok else "FAIL")
print("=" * 70)
sys.exit(0 if ok else 1)
