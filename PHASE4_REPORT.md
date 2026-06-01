# RICH — Phase 4: force the dark regimes (live, unpinned) — Report

**Date:** 2026-06-01 (three live attempts at the forcing goal; results below)
**Goal:** run the unmodified `build()` with **live, unpinned PLAN** end to end, on goals
shaped so the *correct* decomposition can only be **nested** (depth-2) or **non-linear**
(fan-in) — the two regimes Phase 3 left dark because no goal forced them. Harness:
`gate_forcing.py`. Evidence: `phase4_evidence/`.

---

## 0. Result (honest — a win, a reproducibility caveat, and one cell still open)

### 0.1 The seam IS crossed — for a non-linear shape, unbroken, unpinned

The single most important result: in one unbroken, unpinned execution, live PLAN authored a
**non-linear (diamond)** decomposition of `publish_article` AND the engine carried it all the
way to a **correct running deliverable** — no pin, no memo-stitch.

```
publish_article (internal, VERIFIED)        publish(raw) ->
├─ parse_markdown  (leaf)   ─┐                 title:'My Trip'
├─ analyze_content (leaf)   ─┤ fan-out/fan-in  body:'We walked far...'
└─ render_article  (leaf)   ─┘                 analysis:{word_count:14, reading_level:'easy',
edges: parse→analyze, parse→render,                      top_keyword:'the', has_links:True}
       analyze→render                          published:{... summary:'My Trip (14 words, easy)'}
```

`parse_markdown` fans out to *both* `analyze_content` and `render_article`; `render_article`
fans in from *both* `parse_markdown` and `analyze_content`. That is a genuine non-pipeline DAG
— not a line — authored by live PLAN, wired by live IMPLEMENT, and assembled + run correctly
by the deterministic fold. **This is the unbroken end-to-end Phase 3 only achieved stitched.**

### 0.2 Live depth-2 recursion: authored, but NOT yet carried, and NOT reproducible

Across **three** live attempts at the *same* `publish_article` goal this session:

| attempt | `analyze` stage | depth | outcome |
|---------|-----------------|-------|---------|
| A (pre-quota) | **internal → 5 leaves** | **2** | nested + 4-way join authored; build cut by quota at `analyze`'s wiring → **carry not completed** |
| B (this run, clean) | leaf | 1 | **built + assembled + ran correctly** (the §0.1 result) |
| C (this run) | leaf | 1 | built; assemble crashed on a **harness artifact** (§0.3), not the engine |

So: **live depth-2 recursion was *authored* once** (attempt A — `analyze_content` handed to a
separate PLAN call that chose `is_leaf:false` and authored 5 grandchildren, incl. a 4-way join
into `analysis_assembler`; the 6 leaves verified live, real code). But the same goal **flattened
to depth-1 on the other two attempts** — PLAN's nesting here is **non-deterministic (~1/3)**, and
both flat versions are arguably correct (a 4-sub-analysis `analyze` *can* be one module). And on
the one attempt that nested, the external quota cut the build before the internal-node wiring +
assembly ran. So **live depth-2 *carried to a deliverable* is still unshown** — gated by PLAN
non-determinism + quota, not by engine capability (canned `--deep` proves the engine wires
depth-2).

### 0.3 Correction: the attempt-C assemble crash was MY harness bug, not a RICH bug

Attempt C crashed with `TypeError: PublishArticle.__init__() got an unexpected keyword argument
'parse_article'`. Root cause: `gate_forcing_one.py` did `from gate_forcing import …`, but
`gate_forcing.py` ran its battery **on import** — so attempt B's build executed first in the same
process and cached `publish_article` in `sys.modules` (with B's child names
`parse_markdown/analyze_content`). Attempt C rebuilt with different child names
(`parse_article/analyze_body`), but `import publish_article` returned the **stale cached module**,
so assembly injected `parse_article=` into B's class → crash ("did you mean render_article" — the
one name both trees shared). The on-disk files are internally consistent and assemble fine in a
clean process. **Fixed:** `gate_forcing.py`'s side-effecting code is now guarded under
`if __name__ == "__main__":`, so importing it no longer runs the battery or pollutes
`sys.modules`. No RICH change was needed.

### 0.4 The dedicated fan-in goal flattened

`doc_similarity` (jaccard + cosine over a shared tokenizer) was **flattened to a single leaf** by
live PLAN — the leaf bias again. The fan-in evidence we *do* have came incidentally from
`publish_article`'s root diamond (§0.1), not from the goal designed to force it.

### Scoreboard

| question | status |
|----------|--------|
| Live unpinned PLAN *authors* a nested (depth-2) decomposition? | **YES** (attempt A; ~1/3 of the time) |
| Live unpinned PLAN *authors* a non-linear (fan-in/diamond) shape? | **YES** (the `publish_article` root DAG) |
| Engine *carries* a live **non-linear** decomposition to a running deliverable, unbroken? | **YES (new)** — §0.1 |
| Engine *carries* a live **depth-2** decomposition to a running deliverable, unbroken? | **STILL OPEN** — the one nested attempt was cut by quota |
| Is live nesting reliable? | **NO** — non-deterministic (~1/3 on this goal) |

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
