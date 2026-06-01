"""PHASE 4 — force the dark regimes, UNBROKEN and UNPINNED.

Phase 3 proved live PLAN authors sensible SINGLE-LEVEL LINEAR pipelines. The two
thesis-critical questions stayed dark because no goal forced them:
  • live recursion past depth-1 (model-authored nesting, not canned)
  • live non-pipeline composition (model-authored fan-in, not canned)

This harness runs the UNMODIFIED build() loop with LIVE, UNPINNED PLAN end to end
(build → assemble → run) in a SINGLE execution — no pin, no memo-stitching. The seam
between "PLAN decided live" and "the engine carried it through" is crossed in one run.

Two goals, each shaped so the CORRECT decomposition can only be nested / non-linear:

  • publish_article — a parse → analyze → render pipeline where ANALYZE reads as ONE
    stage at the article level but is internally several separable sub-analyses. A
    correct cut nests (root is 3 stages; analyze is itself a sub-pipeline). This is the
    only way to beat the "prefer shallowest clean decomposition" bias and observe depth-2.

  • doc_similarity — jaccard + cosine, BOTH over the SAME tokenization, joined by a
    verdict. A correct cut is a DIAMOND (tokenize feeds two siblings feed a join), not a
    line. Observes whether live PLAN authors a shared dependency (fan-in) or duplicates.

Whatever PLAN does is the result. Flattening is DATA, not failure.

Run:  python gate_forcing.py
"""
import importlib.util
import json
import shutil
import sys
from pathlib import Path

REPO = Path("/home/zaphod/dev/rich")
sys.path.insert(0, str(REPO))

import subagent_skill
import build

STR, NUM, LST, BOOL, DCT = "string", "number", "list", "bool", "dict"


# ── Goal 1: depth-2 forcing — analyze is one stage but internally multi-step ──
PUBLISH_ARTICLE = {
    "id": "publish_article",
    "description": ("Publish a markdown article: parse it, analyze its content, then render "
                    "the published record. Three stages run in order: parse, analyze, render."),
    "interface": {"operations": [
        {"name": "publish", "inputs": {"raw": STR},
         "outputs": {"title": STR, "body": STR, "analysis": DCT, "published": DCT},
         "errors": []},
    ]},
    "dependencies": [],
    "behavior": [
        {"id": "parse", "prose": "Stage 1 (parse): from the raw markdown, take the first line "
                                 "starting with '# ' as the title (without the '# '), and the "
                                 "remaining lines joined as the body."},
        {"id": "analyze", "prose": "Stage 2 (analyze) produces a content-analysis dict for the "
                                   "body. The analysis is ONE deliverable at the article level, "
                                   "but computing it requires FOUR separable, independent "
                                   "sub-analyses, each its own concern: (1) word_count = number "
                                   "of whitespace-separated words; (2) reading_level = 'easy' if "
                                   "average words-per-sentence < 12 else 'hard' (sentences split "
                                   "on '.'); (3) top_keyword = the most frequent word "
                                   "(lowercased, ties broken alphabetically); (4) has_links = "
                                   "true if the body contains 'http'. The analysis dict has keys "
                                   "word_count, reading_level, top_keyword, has_links."},
        {"id": "render", "prose": "Stage 3 (render): assemble the published dict with keys "
                                  "title, body, analysis, and a 'summary' string of the form "
                                  "'<title> (<word_count> words, <reading_level>)'."},
        {"id": "order", "prose": "Stages run strictly in order; analyze consumes parse's body; "
                                 "render consumes title, body, and analysis."},
    ],
}


# ── Goal 2: fan-in forcing — two metrics over the SAME tokenization, joined ───
DOC_SIMILARITY = {
    "id": "doc_similarity",
    "description": ("Compare two documents and report how similar they are. Two similarity "
                    "metrics are computed over the SAME tokenization of both documents, then "
                    "combined into a verdict."),
    "interface": {"operations": [
        {"name": "compare", "inputs": {"doc_a": STR, "doc_b": STR},
         "outputs": {"jaccard": NUM, "cosine": NUM, "verdict": STR}, "errors": []},
    ]},
    "dependencies": [],
    "behavior": [
        {"id": "tokenize", "prose": "Both metrics depend on tokenizing each document the SAME "
                                    "way: lowercase the text, strip punctuation, split on "
                                    "whitespace into a list of word tokens. The two metrics MUST "
                                    "use identical tokenization — it is a shared concern, not "
                                    "duplicated per metric."},
        {"id": "jaccard", "prose": "jaccard = |A ∩ B| / |A ∪ B| over the token SETS of the two "
                                   "documents, rounded to 3 decimals (0.0 if the union is empty)."},
        {"id": "cosine", "prose": "cosine = dot(va, vb) / (||va|| * ||vb||) over term-frequency "
                                  "VECTORS built from the tokens of each document, rounded to 3 "
                                  "decimals (0.0 if either vector is all zeros)."},
        {"id": "verdict", "prose": "verdict combines both metrics: 'duplicate' if jaccard > 0.8, "
                                   "else 'similar' if cosine > 0.5, else 'distinct'."},
    ],
}


def _print_tree(node, indent=1):
    marker = "L" if node.is_leaf else "I"
    st = node.status_path()
    status = json.loads(st.read_text()).get("status") if st.exists() else "?"
    print(f"    {'  ' * indent}{marker} {node.id} ({status})")
    for c in node.children:
        _print_tree(c, indent + 1)


def _max_depth(node, d=0):
    return d if not node.children else max(_max_depth(c, d + 1) for c in node.children)


def _has_nested_child(node):
    """True if any child is itself internal (i.e. live PLAN recursed past depth-1)."""
    return any(c.children for c in node.children)


def _fan_in_nodes(node):
    """child ids that feed 2+ sibling consumers (a diamond / shared dependency)."""
    consumers = {}
    for e in node.edges:
        consumers.setdefault(e.get("from"), set()).add(e.get("to"))
    return {src: tos for src, tos in consumers.items() if len(tos) >= 2}


def _run_goal(goal, op_name, call_args, expect_keys, observe):
    print(f"\n{'─' * 70}\nGOAL: {goal['id']}  —  {observe}\n{'─' * 70}")
    result = {"id": goal["id"], "built": False, "ran": False, "note": ""}
    try:
        root = build.build(goal, allow_decompose=True)
        result["built"] = True
        print("  build() returned a verified tree:")
        _print_tree(root)
        depth = _max_depth(root)
        nested = _has_nested_child(root) or depth >= 2
        fan_in = _fan_in_nodes(root)
        print(f"\n  tree depth: {depth}   nested(depth-2+): {nested}   "
              f"fan-in nodes: {list(fan_in) or 'none'}")
        if root.edges:
            print("  root edges:")
            for e in root.edges:
                print(f"    {e.get('from')} --[{e.get('name')}]--> {e.get('to')}")
        result["depth"] = depth
        result["nested"] = nested
        result["fan_in"] = list(fan_in)

        if root.is_leaf:
            result["note"] = "PLAN FLATTENED to a leaf (no decomposition forced)."
            return result

        build.assemble(root)
        build_abs = build.BUILD_ROOT.resolve()
        if str(build_abs) not in sys.path:
            sys.path.insert(0, str(build_abs))
        modname = f"main_{goal['id']}"
        spec = importlib.util.spec_from_file_location(modname, str(build_abs / "main.py"))
        m = importlib.util.module_from_spec(spec)
        sys.modules[modname] = m
        spec.loader.exec_module(m)
        demo = m.assemble()
        out = getattr(demo, op_name)(**call_args)
        result["ran"] = True
        print(f"\n  deliverable run: {op_name}({call_args}) ->")
        for k, v in (out.items() if isinstance(out, dict) else [("result", out)]):
            print(f"    {k}: {v!r}")
        missing = set(expect_keys) - set(out.keys() if isinstance(out, dict) else [])
        result["note"] = ("WORKS — declared keys present." if not missing
                          else f"ran but MISSING keys: {missing}")
    except build.BuildFailure as e:
        result["note"] = f"BuildFailure: {e}"
        print(f"\n  ✗ BuildFailure: {e}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        result["note"] = f"{type(e).__name__}: {e}"
    return result


def main():
    subagent_skill.install()
    subagent_skill.reset_telemetry()
    print("=" * 70)
    print("PHASE 4 — FORCE THE DARK REGIMES (live, unpinned, unbroken)")
    print("=" * 70)

    if build.BUILD_ROOT.exists():
        shutil.rmtree(build.BUILD_ROOT)
    build.BUILD_ROOT.mkdir(parents=True)

    results = []
    results.append(_run_goal(
        PUBLISH_ARTICLE, "publish",
        {"raw": "# My Trip\nWe walked far. The day was long and the path was hard. See http://x.io"},
        ["title", "body", "analysis", "published"],
        "depth-2 forcing (does live PLAN nest the analyze stage?)"))

    results.append(_run_goal(
        DOC_SIMILARITY, "compare",
        {"doc_a": "the quick brown fox jumps", "doc_b": "the quick brown dog runs"},
        ["jaccard", "cosine", "verdict"],
        "fan-in forcing (does live PLAN share one tokenizer across two metrics?)"))

    subagent_skill.print_telemetry()
    print(f"\n{'=' * 70}\nPHASE 4 FINDINGS — read honestly\n{'=' * 70}")
    for r in results:
        flags = []
        if r.get("nested"):
            flags.append("DEPTH-2 LIVE ✓")
        if r.get("fan_in"):
            flags.append(f"FAN-IN LIVE ✓ ({r['fan_in']})")
        tag = (" | ".join(flags)) if flags else "single-level linear (or flattened)"
        print(f"  • {r['id']}: built={r['built']} ran={r['ran']} — {tag}")
        print(f"      {r['note']}")


if __name__ == "__main__":
    main()
