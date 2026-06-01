# RICH — Phase 3: "Test the bet, not the machinery" — Report

**Date:** 2026-06-01
**Brief:** Move 1 (unify "an internal node depends on its CHILDREN" across assembly
**and** the IMPLEMENT/DERIVE_TESTS call sites — test-enablement, not capability-building)
+ Move 2 (run the *unmodified* `build()` with **live, unpinned PLAN** to depth ≥ 2 on a
varied battery and deliver a **GO / NO-GO / REDIRECT judgment on decomposition quality**).
**Standing instruction honored:** *"don't just implement this blindly… if there's anything
not good here change it and be explicit about what you changed in the final report."* —
§5 below lists every deviation.

---

## 0. Verdict (the Move 2 deliverable)

**GO on the *easy regime* of the thesis only. The two thesis-critical questions —
live recursion and live non-pipeline composition — remain wide open.**

What is genuinely demonstrated: live, unpinned PLAN (Claude Code subagent, sonnet) can
author a sensible single-level, linear, multi-stage decomposition of a goal that warrants
cutting (`comment_ingest`: sanitize → moderate → {enrich, format}), and the engine can
carry that decomposition to a verified, correctly-running deliverable. Separately, live
PLAN correctly *refuses* to over-decompose three small single-concern goals (it held the
fan-in-shaped stats goal as a leaf rather than manufacturing a sub-module to host a shared
rounder). Both results are real, and they are the first observations of live-PLAN
decomposition *quality* at all.

**Three honest limits on that GO — the first is a method caveat, the other two are the
ballgame:**

1. **The end-to-end run was stitched, not unbroken.** PLAN authored the decomposition live
   in one run; the final verification re-**pinned** PLAN to that on-disk decision so the
   four children memo-hit and only the root's two skill calls ran fresh (see §1's method
   note). The seam between "PLAN decided this live" and "the engine carried it through" was
   never crossed in a single execution — it is two separately-validated halves connected by
   hash-identical memos. That is the most defensible pin in the project's history (it pins
   *after* PLAN ran, on a genuine artifact), and the deterministic-fix argument is correct
   for what it proves (Move 1 + assembly work on a real decomposition). But it is still a
   pin, and "we didn't re-pay to re-author an identical tree" is a cost argument, not an
   evidence argument. **Full unbroken live end-to-end has not been shown.** (Phase 4
   crosses this seam — see §8.)

2. **Live recursion past depth-1 is unobserved.** PLAN chose depth-1 (correctly — the
   stages are leaf-sized). The *only* depth-2 evidence is the canned `--deep` test, where a
   human wrote both the decomposition and the implementations and the engine merely wired
   them. The model has never authored a nested decomposition.

3. **Live non-pipeline composition is unobserved.** Every live decomposition RICH has ever
   produced is a single-level linear pipeline. All three non-linear small goals flattened to
   leaves (correctly, they are leaf-sized). The model has never authored a fan-in or a
   conditional; the only non-pipeline evidence (`--fan-in`) is, again, canned.

Put (2) and (3) together: **the entire live evidence base is single-level linear pipelines.**
Recursion (what makes RICH more than "an agent writes some modules") and non-pipeline
composition (where real software lives) are supported by canned tests only. §0's GO is a GO
on the easiest regime, which is about where an honest first live result should land.

**Why the battery couldn't surface (2) and (3):** PLAN only decomposes when a goal warrants
it, and its leaf bias is strong and correctly calibrated. No goal in the battery was large
enough to *force* nesting or non-linearity (`comment_ingest` warranted decomposing but is
naturally a pipeline). The flattening is PLAN judging correctly, not failing — which means
the battery was mis-designed to answer the questions that matter most. The fix is a goal
constructed specifically so that nesting / non-linearity is the *only* correct cut. That is
Phase 4 (§8), and it is run unbroken and unpinned.

---

## 1. The live decomposition (authored unpinned) + its stitched verification

Goal handed to live PLAN (`comment_ingest`): one operation, `ingest(text) -> {body,
flagged, word_count, reading_time, preview}`, described as a four-stage ingestion pipeline.

**Live PLAN's decision** (verbatim from `build/comment_ingest/decision.json`, preserved in
`phase3_evidence/comment_ingest.decision.json`): `is_leaf: false`, four children with a
clean linear edge set —

```
sanitize --[text]--> moderate
moderate --[body]--> enrich,  moderate --[body,flagged]--> format
enrich   --[word_count,reading_time,preview]--> format
```

All four children built and **self-verified live** (each a leaf). The root internal node's
wiring was authored by live IMPLEMENT (`phase3_evidence/comment_ingest.wiring.py`):

```python
class CommentIngestPipeline:
    def __init__(self, sanitize, moderate, enrich, format):  # params == child ids
        ...
    def ingest(self, text):
        s = self.sanitize.sanitize(text=text)
        m = self.moderate.moderate(text=s["text"])
        e = self.enrich.enrich(body=m["body"])
        return self.format.format(body=m["body"], flagged=m["flagged"],
                                  word_count=e["word_count"],
                                  reading_time=e["reading_time"], preview=e["preview"])
```

The `__init__` params are named by **child id**, exactly matching what `assemble()` injects
(`_injection_deps` keys internal nodes by child id) — tests, impl, and assembly agree on
names *by construction*, which is the whole point of Move 1.

**Deliverable runs correctly** (`assemble()` → `build/main.py` → run):

```
ingest("<p>Hello   world this is a    nice comment</p>")
  -> body='Hello world this is a nice comment'  flagged=False
     word_count=7  reading_time=3  preview='Hello world this is a nice comment'

ingest("Buy cheap <b>viagra</b> now, this spam scam is great!!!")
  -> body='Buy cheap *** now, this *** *** is great!!!'  flagged=True
     word_count=9  reading_time=3  preview='Buy cheap *** now, this *** *** is great!!!'
```

HTML stripped, whitespace collapsed, all three banned words masked, `flagged` correct,
`reading_time == ceil(word_count/3)`, declared output keys all present.

> **Run economics / honest note on method.** The decomposition above was authored by live,
> *unpinned* PLAN in an earlier run; that genuine artifact (decision + 4 verified children
> with memos) was on disk. The final end-to-end verification (`gate_verify_assembly.py`)
> **reused** it: PLAN was pinned to the on-disk decision *only* so the 4 children memo-hit
> (hashes confirmed identical) and we did not re-pay to re-author an identical tree — only
> the root's DERIVE_TESTS + IMPLEMENT ran fresh (**2 LLM calls, $0.34**). The fix being
> verified (Move 1 + assembly) is downstream of PLAN and deterministic, so reuse does not
> launder the result. The decomposition *quality* judgment in §0 is read from the genuine
> unpinned artifact, not from the pinned re-run.

---

## 2. Small goals — live PLAN does **not** over-decompose

`gate_small_goals.py` — PLAN-decision observation only, no build (**3 LLM calls, $0.085**):

| goal | shape | live PLAN decision |
|------|-------|--------------------|
| `validate_registration` | 2-check validator | **LEAF** |
| `number_stats` | mean+stddev sharing one rounder (fan-in-shaped) | **LEAF** |
| `route_request` | 3-branch method router (conditional) | **LEAF** |

All three correctly stay leaves. The fan-in question ("would it share or duplicate the
rounder?") is **moot at this scale** — PLAN keeps the goal as one module, so there is no
sharing decision to make; that is itself the finding (PLAN does not manufacture a
sub-module just to host a shared utility for a 10-line goal). This is the corrected battery
design: an earlier battery used *only* small goals like these and saw nothing but leaves —
which is correct behavior, but means it never tested a decomposition. §1's decomposition-
scale goal was added precisely to observe one.

---

## 3. Canned regression suite — all green, and now *honest*

| test | result |
|------|--------|
| canned pipeline (`pipeline_demo`) | ✓ real result dict |
| `--fan-in` | ✓ `regex_engine` instantiated **once** (shared dep), real result |
| `--deep` (depth-2: `validate_registration → [username_checker, password_pipeline→[length_check, complexity_check], token_generator]`) | ✓ **real** result `{username_ok, password_ok, token, reason}` |
| `--memo-test` | ✓ second build instant from cache |

`--deep` returning a *real* composed dict (not `None`) is the deterministic proof that
**depth-2 composition works end-to-end** — the live half is depth-1 only, so this is where
the depth-2 evidence comes from.

---

## 4. What changed (Phase 3 code)

| file / loc | change | brief? |
|------------|--------|--------|
| `build.py` ~533–535 | **Move 1:** internal-node `dep_contracts` sourced from `_injection_deps(node)` (the CHILDREN, keyed by child id), not `contract.dependencies` (empty for every live-PLAN-authored internal node). Unblocks live IMPLEMENT/DERIVE_TESTS for internal nodes. | literal brief |
| `skills.py` `PLAN_SYSTEM_DECOMPOSE` | depth relaxation: replaced "max 2 levels, children must be leaves" with a RECURSION rule (a child may itself decompose; engine caps depth; prefer the shallowest clean decomposition). | authorized (Q2) |
| `build.py` `_injection_deps` | string-filter: ignore non-`{name,id}` dependency entries — see §5.1 | deviation |
| `skills.py` `PLAN_SYSTEM_DECOMPOSE` | heuristic decouple: leaf-vs-decompose decision based on BEHAVIOR (separable concerns), not operation count — see §5.2 | deviation |
| `build.py` `assemble()` `__main__` gen | smoke-stub now builds type-appropriate kwargs from declared inputs — see §5.3 | deviation |

(The `assemble()` real-wiring rewrite and `_injection_deps` itself are **Phase 2** work,
already documented in `FIX_REPORT.md`; Phase 3 only added the three deviations above and
Move 1.)

---

## 5. Deviations from the literal brief (explicit, per standing instruction)

**5.1 `_injection_deps` string-filter.** Live PLAN emits, alongside the structured `edges`,
a redundant bare-string echo of the pipeline as each child's `dependencies` (e.g.
`moderate.dependencies = ["sanitize"]`). `_injection_deps` indexed dependency entries as
`{name,id}` dicts and crashed (`string indices must be integers`) at the assemble step.
I filtered to well-formed `{name,id}` dicts and ignore the bare strings. **Justification:**
the `edges` are the authoritative wiring and a child's stages are composed by its *parent*,
not injected into the child — the bare strings are pure redundancy. This does **not** violate
"fail loud on malformed composition": we still fail loud on a genuinely unknown shape; we
only ignore a redundant echo of information the edges already carry. **Finding worth
flagging:** live PLAN double-encodes the pipeline (edges *and* bare-string child deps), and
the bare strings don't conform to the dep schema. Cleaner long-term fixes are to tighten the
PLAN prompt to stop emitting them, or to normalize them in one place — deferred.

**5.2 Heuristic decouple (op-count → behavior).** The first live battery flattened *every*
goal because the original rule said "if the contract is simple (1–2 ops…), return leaf" and
all goals had one operation. That ties decomposition to interface width, which is wrong — a
single-operation contract can still have separable internal stages (comment_ingest is
exactly that). I rewrote the rule to decide on **behavior** (separable concerns/stages),
not op count. Without this, live PLAN could never decompose a single-operation goal, and the
bet would be untestable.

**5.3 `__main__` smoke-stub arity fix.** Running the canned suite after Move 1 surfaced a
pre-existing bug: `assemble()` generated `demo.<op>('test input')` — one positional arg —
for the smoke test. Now that assembly instantiates the *real* verified wiring class (Phase 2)
instead of the old `pass`-stub, that single arg mismatches any op with ≠ 1 input: `--deep`'s
`validate(username, password)` raised `TypeError` (where the old `pass`-stub had silently
returned `None` — a false green). I changed the generator to build type-appropriate kwargs
from the op's declared inputs. **Justification:** required to make the canned suite *honestly*
green; without it `--deep` is red. Strictly beyond Move 1/Move 2, taken under the standing
"change what's not good" instruction. Scope is confined to the generated smoke-test line.

**5.4 Battery redesign (method, not code).** The brief's battery sketch leaned on small
fan-in / conditional goals. After two runs showed (correctly) nothing but leaves, I added one
genuinely decomposition-scale goal (comment_ingest) for the full build, and demoted the small
goals to PLAN-decision observation. Without a goal that warrants decomposing, the battery
cannot observe a decomposition — see §2.

---

## 6. Files added this phase

- `gate_verify_assembly.py` — finishes the live-authored comment_ingest tree (children
  memo-hit) and verifies assemble + run. **PASS.**
- `gate_small_goals.py` — observes live PLAN's leaf/decompose decision on the 3 small goals.
- `gate_live_plan.py` — the full unpinned battery (wipes `build/` and rebuilds from scratch);
  superseded for routine use by the two cheaper gates above, kept for a cold full run.
- `phase3_evidence/` — preserved artifacts: the live decision.json, the live wiring class,
  and the assembled main.py.

## 7. Total live spend this phase

Verification + small-goal observation: **5 LLM calls, ~$0.43**. (The original unpinned
authoring run that produced the decomposition is not re-counted here.)

---

## 8. Phase 4 — force the dark regimes, unbroken and unpinned

Not another fix, not widening the engine. The one test the Phase 3 battery couldn't run:
goals constructed so that the *correct* decomposition can only be nested or non-linear, big
enough that PLAN's (correct) leaf bias cannot flatten them. Run as a **single unbroken live
execution** — no pin, no memo-stitching, fresh `build/` — so the seam between "PLAN decided
live" and "the engine carried it through" is actually crossed.

Two goals:

- **Depth-2 forcing (live recursion).** A goal whose natural cut has a *stage that is itself
  a multi-stage sub-pipeline* — so a correct decomposition must nest. We shape it; we cannot
  force PLAN's hand, so if PLAN still flattens that stage to a leaf, that is the finding
  (recursion not naturally reached), not a bug to fix.

- **Fan-in forcing (live non-pipeline).** A goal whose result genuinely *combines* two
  sibling computations that both need the *same* non-trivial sub-component, each sibling big
  enough to be its own module — so a correct decomposition is a diamond (one child feeding
  two siblings feeding a join), not a line. Fan-in over conditional because the engine can
  actually *wire* a shared dep (proven canned in `--fan-in`); a conditional would observe
  PLAN's authoring but fail at assembly (unsupported) — a separate, narrower probe.

Deliverable: for each goal, PLAN's actual decomposition read honestly (nested? diamond?
flattened?), and — if it decomposes — whether the unbroken build carries it to a working
deliverable. Whatever PLAN does is the result; flattening is data, not failure.

**Status:** see `PHASE4_REPORT.md`.
