# RICH — Phase 4: force the dark regimes (live, unpinned) — Report

**Date:** 2026-06-01 (PARTIAL — run cut by an external session quota; see §3)
**Goal:** run the unmodified `build()` with **live, unpinned PLAN** end to end, on goals
shaped so the *correct* decomposition can only be **nested** (depth-2) or **non-linear**
(fan-in) — the two regimes Phase 3 left dark because no goal forced them. Harness:
`gate_forcing.py`. Evidence: `phase4_evidence/`.

---

## 0. Result (honest, and partial)

**NEW — newly lit:** live, unpinned PLAN, handed a single goal (`publish_article`), authored
a decomposition that is **both nested AND non-linear**, with no pin anywhere:

```
publish_article            (internal)        ← root, 3 stages
├─ parse_markdown          (leaf, VERIFIED)
├─ analyze_content         (internal)         ← PLAN RECURSED here (depth-2, model-authored)
│  ├─ word_count_analyzer      (leaf, VERIFIED)
│  ├─ reading_level_analyzer   (leaf, VERIFIED)
│  ├─ top_keyword_analyzer     (leaf, VERIFIED)
│  ├─ has_links_checker        (leaf, VERIFIED)
│  └─ analysis_assembler       (leaf, VERIFIED)   ← 4-way JOIN (fan-in)
└─ render_article          (never reached — see §3)
```

- **Depth-2 live recursion (was canned-only):** `analyze_content` was handed to a *separate*
  PLAN call and chose `is_leaf:false`, authoring 5 grandchildren. The model produced the
  nesting — the first time recursion past depth-1 has been observed live.
- **Non-pipeline live composition (was canned-only):** two diamonds, both model-authored —
  `analyze_content`'s four analyzers all feed `analysis_assembler`; and at the root,
  `parse_markdown` feeds *both* `analyze_content` and `render_article`. Not a line.
- The forcing design worked: `analyze` read as one stage at the article level (so the root
  didn't flatten to a wide pipeline) but was internally multi-concern (so the recursive PLAN
  call decomposed it) — beating the prompt's "prefer shallowest clean decomposition" bias.
- The 6 leaves are **real, correct, self-verified** code (e.g. `top_keyword_analyzer` does
  frequency-count with an alphabetical tiebreak; `analysis_assembler`'s signature exactly
  matches its 4 incoming edges).

**NOT shown — still open:** the build **did not complete**. `analyze_content` has its tests
but an empty `src/` (the quota hit at its wiring IMPLEMENT); `render_article` was never
created; nothing assembled or ran; `doc_similarity` never started (its first PLAN call hit
the limit). So:

| question | status |
|----------|--------|
| Does live unpinned PLAN *author* nested decompositions? | **YES (new)** |
| Does live unpinned PLAN *author* non-pipeline (fan-in) shapes? | **YES (new)** |
| Does the engine *carry* a live nested/fan-in decomposition to a verified, running deliverable? | **STILL OPEN** — internal-node wiring + assembly were never reached |
| Was the full loop crossed unbroken in one run? | **NO** — an external quota (not a logic failure) cut it at the internal-node wiring |

This is the inverse of Phase 3's gap. Phase 3 showed the engine *carries* a (linear,
single-level) decomposition to a deliverable, but the *authoring* was stitched/pinned.
Phase 4 shows live PLAN *authors* the hard shapes unpinned, but the run died before the
engine could carry them. Neither phase has yet shown authoring-AND-carrying of a hard shape
in one unbroken execution.

---

## 1. Why it stopped where it did

The failure was `api_error_status: 429`, `"You've hit your session limit · resets 8:10pm
(Asia/Kolkata)"`, on the IMPLEMENT call for `analyze_content`'s wiring — **purely an external
quota**, after 21 successful calls ($0.98). Not an engine fault, not a logic failure. Every
node reached before the cut either verified or (for the internal node) got as far as
DERIVE_TESTS.

## 2. The quota constraint is now load-bearing for the unbroken test

Observed quota window this session: ~20 calls / ~$1 before a 429. A full unbroken depth-2
build of even *one* of these goals is ~25+ calls (root PLAN + per-leaf plan/derive/impl ×6 +
two internal-node derive/impl + render + root wiring, plus any retries). **So a fresh
unbroken run likely does not fit in one quota window** — which is why this run died mid-build
rather than from any routing-around. The unbroken-to-completion test is gated by quota size,
not by engineering. Options to actually achieve it are in §4.

## 3. Evidence preserved

`phase4_evidence/`: both `decision.json` files (the nested + fan-in structure), and the 6
verified leaf sources. `build/` will be overwritten by the next run; these are the artifacts.

## 4. To finish the test (post-reset, 8:10pm IST) — decision required

Three ways forward, trading off "unbroken" against "fits in quota" — see the message
accompanying this report. In brief:
- **(a) Minimal unbroken goal:** shrink the forcing goal (e.g. analyze with 2 sub-analyzers)
  so the *whole* depth-2 build fits one quota window and crosses the seam unbroken. Genuine,
  smallest nested case.
- **(b) Resume this tree (pinned):** pin the already-live-authored decisions so the 6 leaves
  memo-hit and only the wiring + `render_article` build — cheap, finishes a real deliverable,
  but the engine half is stitched (same caveat as Phase 3 §0.1).
- **(c) Stop at the authoring result:** accept the new finding (PLAN authors both hard shapes
  live) and defer the engine-carry proof.

**Status: BLOCKED until ~8:10pm IST (session quota). No more attempts until then (they only
burn failed calls).**
