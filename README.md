# RICH — Recursive Agent Build System

> **Active direction — RICH v2:** turn approved product intent into an immutable,
> budgeted, sandbox-built, independently verified software release. The original
> recursive Python engine remains below as v1 and is still supported.

## RICH v2: an intent-to-verified-software compiler

The long-term goal is to generalize software development without reducing it to
“prompt → code.” RICH treats development as a compilation and evidence problem:

```text
interviewed intent
  → approved product specification
  → approved typed architecture and ownership map
  → durable dependency-ordered tasks
  → frozen target-pack scaffold
  → bounded model-authored source
  → independent lint / type / unit / build / browser gates
  → content-addressed release snapshot
  → separately approved preview deployment
```

The current vertical slice implements that chain for a production-grade Next.js
monorepo target pack. It includes:

- immutable spec and architecture revisions with explicit approval gates;
- requirement traceability plus human-approved, data-only browser oracles that
  compile into protected acceptance tests and attempt-bound coverage evidence;
- fail-closed source ownership and protected build/test/toolchain inputs;
- SQLite-backed runs, tasks, events, artifacts, idempotency, and fenced execution
  leases;
- atomic owner-token fencing for scheduler state/evidence writes plus reaped
  process-group cancellation on lease loss or deadline;
- a complete approved model/cost/time budget with crash-conservative recovery;
- CAS-backed source write-ahead transactions that recover the apply/persist crash
  window without trusting unrecorded workspace bytes;
- one exact trusted model policy (`anthropic/claude-sonnet-5`) reached by two
  explicitly chosen routes -- an API key or an existing `claude` login -- which
  are never fallbacks for one another;
- exact Node 22.22.3 and pnpm 10.34.5 identity checks;
- Bubblewrap isolation, network-off verification, bounded processes/heaps/output,
  and no unsafe local fallback;
- independent lint, TypeScript, unit, contract-obligation, production-build, and
  Playwright gates;
- proof obligations declared by a contract compiled into a runnable property
  suite against a pinned operations interface, so a claim is executed rather
  than merely stated;
- generation memoized by the exact request, so an unchanged task is not re-paid
  for -- and a single node can be marked stale and rebuilt without redoing its
  siblings;
- gate output fed back into the next attempt, redacted to what the failing task
  is allowed to read;
- cooperative durable cancellation, observable from any surface;
- an immutable full-source ZIP tied to the acceptance evidence; and
- digest-bound, separately approved Neon/Vercel previews, immutable uploads,
  trusted SQL-only migrations, and teardown.

The generated source cannot modify its verifier, tests, dependency graph, or
toolchain. A successful model response is never evidence. Only independently
observed command results can publish task/run success. Evidence may flow the
other way -- a failed gate's output informs the retry -- but never back again:
evidence may inform generation, generation may never become evidence.

This is a working generalized-development **kernel**, not yet a claim that arbitrary
software is solved. The compiler/target-pack boundary is designed for more languages
and deployment shapes; the implemented pack today is Next.js, local live-workspace
execution is deliberately serialized, and distributed systems still require new
resource, migration, observability, and failure-semantics packs. See
[the v2 architecture and operating contract](docs/v2-architecture.md).

### Run the v2 control plane

```bash
python -m pip install -e '.[test]'
python -m rich_v2.cli doctor

npm --prefix web ci
npm --prefix web run build
python canvas.py
```

Open `http://127.0.0.1:8765` for the Canvas. The v2 JSON API is mounted under
`/v2`. Mutating API calls require an `Idempotency-Key`; execution and external
preview actions remain approval- and digest-gated.

For a model-backed run, export `OPENAI_API_KEY`. Preview deployment additionally
uses `NEON_API_TOKEN` and `VERCEL_TOKEN`. Credentials are resolved lazily and are
not persisted in run documents or model events.

The default test suite is offline:

```bash
ruff check .
python -m pytest
```

Host/toolchain conformance, including a fresh generated app through the complete
sandbox pipeline, is explicit:

```bash
python -m pytest --run-live \
  --basetemp=.rich/live-tests \
  tests/test_v2_public_runtime_live.py
```

The generated workspace, locked dependency store, and Chromium installation can
exceed 2 GiB, so the explicit base directory avoids small RAM-backed `/tmp`
filesystems.

---

## RICH v1: recursive module synthesis

The original RICH engine builds software by **recursive decomposition with bounded LLM agents**. You give it
one top-level *contract* (what a program must do); a single recursive procedure
`build(contract)` either implements it directly as a **leaf** or asks an LLM **architect**
to split it into child modules whose contracts it authors, then recurses. Three LLM skills
do the thinking — **PLAN** (decompose), **IMPLEMENT** (write code), **DERIVE_TESTS** (write
the verification) — and two deterministic engines do the rest — **run_tests** (verify) and
**assemble** (a topological fold that injects each module's dependencies and emits one
runnable `main.py`). Every module is written against its dependencies' *contracts*, never
their source; tests are derived by the *consumer*; and what gets verified is exactly what
ships.

> **v1 status:** RICH is a working tool for genuinely-useful,
> **stateless-dataflow** programs that decompose into pipelines, fan-in/fan-out diamonds,
> held-capability sharing, and **compositional depth** (a stage that is itself several
> sub-modules — builds two levels deep, uncoached, live). It correctly keeps
> *algorithmically* recursive code (e.g. an expression parser) in a single leaf.
>
> **Depth delivery continuation:** the two Phase 11 delivery residuals now have engine fixes:
> concrete shape examples flow **parent→child** as well as across sibling edges, and a child
> failure triggers a bounded local child replan instead of re-decomposing the parent and
> rebuilding verified siblings. This is the candidate GREEN mechanism. The remaining proof
> step is the fresh live gate with the Codex backend. It still does **not** do cross-module
> conditional/branching composition or distribution across processes. See
> [§7](#7-what-is-proven--the-verification-boundary-map),
> and [§8](#8-known-boundaries--non-goals) for the precise regime.

---

## 1. What RICH is

The entire system is **one recursive procedure + three LLM skills + two deterministic
engines.**

```
                    ┌─────────────────────────────┐
                    │  build(contract) → Node      │  ← single recursive procedure
                    └─────────────┬───────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                    ▼
        ┌──────────┐       ┌────────────┐      ┌────────────┐
        │   PLAN   │       │ IMPLEMENT  │      │DERIVE_TESTS│   ← 3 LLM skills
        │(architect)│      │  (coder)   │      │  (tester)  │      (non-deterministic)
        └────┬─────┘       └─────┬──────┘      └─────┬──────┘
             ▼                   ▼                    ▼
        ┌──────────┐       ┌────────────┐      ┌────────────┐
        │   DAG    │       │ verify loop│      │  pytest    │   ← deterministic engines
        │ validate │       │  K_IMPL=3  │      │ subprocess │      (rock-solid)
        └──────────┘       └────────────┘      └─────┬──────┘
                                                     ▼
                                              ┌────────────┐
                                              │  assemble  │   ← topological fold
                                              │  → main.py │      + dependency injection
                                              └────────────┘
```

| Skill | Role | Input | Output |
|-------|------|-------|--------|
| **PLAN** | Architect | A module contract | `{is_leaf:true}` or `{is_leaf:false, children:[…], edges:[…]}` (children's contracts authored here) |
| **IMPLEMENT** | Coder | Contract + dependency *contracts* | Python source |
| **DERIVE_TESTS** | Tester | Contract (+ dep contracts) | pytest source |

| Engine | Role | How |
|--------|------|-----|
| **run_tests** | Verification | pytest in a timeout-guarded subprocess; returns pass/fail + failure detail |
| **assemble** | Delivery | topological fold → `build/main.py`; shared deps instantiated once and injected by name |

---

## 2. Core principles

- **The information firewall.** A module is generated by a single stateless LLM call that
  sees its *own* contract and its dependencies' *contracts* — never their source. The
  firewall is the prompt: dependency source is never placed in context. This is what makes
  the decomposition compositional rather than one giant prompt.

- **Contracts flow DOWN from demand, not UP from supply.** A module never writes its own
  contract. The **consumer (parent)** authors it: PLAN's decomposition output *is* the
  children's contracts. A module receives its contract as its task and is responsible only
  for satisfying it.

- **Dependencies are injected by name, never imported.** A module that needs a sibling
  receives it as a named constructor parameter and calls it (`self.<dep>.<op>(…)`). It never
  `import`s another module. Assembly does the wiring.

- **The budget is the recursion's base case — and it is PLAN's disposition, not a code
  enforcer.** PLAN decides leaf-vs-decompose on *behavior*: a leaf is "one cohesive
  computation a competent engineer would write as a single small module"; it decomposes when
  the behavior names separable stages or concerns. A hard `MAX_DEPTH` cap bounds runaway
  recursion. **There is no implement-then-check size enforcer** that measures generated code
  and forces a split — see [§8](#8-known-boundaries--non-goals). In practice PLAN's judgment
  tracks genuine modular structure well (Phase 10): it keeps an algorithmic leaf whole and
  decomposes a genuinely compositional stage.

- **Assembly is a deterministic topological injection fold.** `assemble` traverses the
  verified tree in dependency order, instantiates each leaf, injects dependencies (a shared
  dependency is constructed **once** and injected into every consumer), and instantiates each
  internal node's wiring class with its children. No LLM is involved in delivery.

- **Verify-it / ship-it consistency.** Assembly instantiates the *same* IMPLEMENT class that
  passed verification. What was verified is what runs. Verification is **existential** —
  "passed" means no violation was observed on the tested inputs, not a proof of correctness.

---

## 3. Architecture / file reference

| File | Lines | Purpose |
|------|------:|---------|
| `build.py` | ~1200 | **The engine.** The recursion (`build`), verification (`run_tests`), delivery (`assemble`), memoization + resumption, local child REPLAN, hard caps, integration verification, sibling + parent→child concrete shape handoff, the call manifest |
| `cli.py` | ~510 | **The CLI + canned demos.** argparse entrypoint and the no-LLM regression demos (M-A pipeline, M-F fan-in, M-G deep + memo) plus the live single-leaf / decompose drivers. `python cli.py --help` |
| `skills.py` | ~1130 | PLAN / IMPLEMENT / DERIVE_TESTS — prompts, JSON/DAG validation, canned (no-LLM) fallback data, model routing |
| `backend.py` | ~60 | **Backend selector.** `RICH_BACKEND=claude|codex|openrouter`, with shared install/availability/telemetry hooks |
| `subagent_skill.py` | ~330 | **Claude CLI backend.** Runs each skill as a `claude -p --model <m> --tools "" --max-turns 6` subprocess (Phase 11 Fix 1: zero tool affordance + recovery headroom, firewall via `--disallowedTools` kept); per-call token/cost logging → `build/llm_calls.jsonl` |
| `codex_skill.py` | ~190 | **Codex CLI backend.** Runs Codex as a bounded schema-validated text generator through `codex exec` in an empty temp dir with read-only sandbox and no approvals |
| `llm.py` | 183 | **OpenRouter backend seam** (call/retry/JSON-defense), selected with `RICH_BACKEND=openrouter` and `OPENROUTER_API_KEY` |
| `node.py` | 125 | `Node` dataclass + on-disk persistence (contract.yaml / decision.json / status.json / deps.yaml) + topological sort |
| `canvas.py` | — | **Canvas backend.** Stdlib HTTP server: the engine as a JSON API (`/api/plan`, `/api/vibe`, `/api/build`, `/api/vibe-edit`, `/api/node`, `/api/statuses`, `/api/architecture/*`) + serves the built React app from `web/dist`. Scoped single-module rebuild via `build.invalidate_node` |
| `web/` | — | **The canvas (React + Vite + React Flow).** Browser design IDE: module graph, Vibe Bar with previewed diffs, inspector showing each module's **generated code + test results** inline, one-click **rebuild-this-module** + **vibe-a-fix**, live per-module build status, project-file persistence, adapter boundaries, validation/root-cause cards |
| `deep_test.py` | — | Canned decompositions/impls/tests for the `--deep` regression |
| `tree_viewer.py` | 232 | **Read-only** inspector: renders `build/` to a graphviz HTML (structure + state views; highlights fan-in and shared-stateful "dragon" nodes). Never writes into `build/` |
| `tests/run_tests.py` | 448 | Live end-to-end test runner (T0→T6) |
| `tests/test_harness.py` | 342 | Live test instrumentation: wraps the skills at the boundary, monitors calls, checks the firewall |
| `docs/spec.md` | — | The v1 design document |

`build.py` constants (hard caps + retry limits):

```
K_IMPL       = 3    leaf IMPLEMENT retry limit (verify loop)
MAX_DEPTH    = 3    maximum recursion depth (hard cap; permits depth, does not force it)
MAX_CHILDREN = 8    maximum children per node (hard cap)
MAX_LLM_CALLS = 50  global live-call ceiling (hard cost cap; also a clean resume checkpoint)
REPLANS_MAX  = 2    maximum REPLAN attempts when a child fails to build
```

---

## 4. The build lifecycle

`build(contract, allow_decompose)`:

1. **Memo / resume check.** If a hash-identical contract was verified before, return the
   cached subtree (no LLM). This is what makes builds **resumable** across a quota cut.
2. **PLAN.** Decide leaf or decompose. On resume, a node's *own* prior live decision is
   reused if the contract hash matches (frozen tree shape) — the PLAN-level analogue of
   memoization. Either path is recorded to the call manifest.
3. **Leaf path:** DERIVE_TESTS → IMPLEMENT, then `run_tests`; retry up to `K_IMPL` with the
   failure detail fed back into the next IMPLEMENT prompt. A leaf may be a **stateless
   transformation** (top-level functions) or a **stateful component** (one class verified by
   operation *sequences*), and it may **hold an injected capability** (a sibling utility it
   calls) — resolved from the sibling contracts the parent passes down.
4. **Internal path:** recurse on each child in dependency order. Concrete shape examples
   are threaded both across sibling dataflow edges and from a parent's inbound example down
   to matching child inputs. If a child fails, only that child gets a bounded local replan
   (up to `REPLANS_MAX`); verified siblings are not rebuilt by a parent re-decomposition.
   Then generate + verify the **wiring class** that threads the children. When an internal
   node's children **share a stateful dependency**, an additional **integration test** runs
   over the *real* assembled subtree (no fakes) — the sound check for cross-module shared
   state (Phase 8).
5. **Assemble.** `assemble(root)` folds the verified tree into `build/main.py`.

---

## 5. The contract schema

```yaml
id: my_module                    # unique, lowercase_underscore
description: what it does
interface:
  operations:
    - name: do_thing
      inputs:  {text: string}    # types: string | int | float | bool | list  (and dict)
      outputs: {result: float, error: string}
      errors:  []
dependencies: []                 # [{name: <param>, id: <module_id>}] — capabilities this module HOLDS
behavior:                        # prose properties with stable ids — the basis for derived tests
  - id: precedence
    prose: "multiplication binds tighter than addition, so 2+3*4 = 14"
stateful: false                  # true → a class verified by operation sequences
```

- A **live root** has **no** `dependencies` key — PLAN authors the children and edges.
- **Edges** (`{from, to, name}`) express **dataflow handoff**: the parent threads one
  child's output into the next as a named argument; the downstream child knows nothing about
  the upstream one.
- **Dependencies** express a **held capability**: a utility a consumer holds and calls at
  points of its own choosing. A utility shared by several consumers is **one** child injected
  into each (fan-in), constructed once by assembly.
- The type vocabulary is `string | int | float | bool | list` (plus `dict`). Nested lists /
  dicts carry structured data (e.g. an AST as `["+", 3, ["*", 4, 2]]`). There are **no
  opaque module-defined types** across contract boundaries.

---

## 6. How to run it

### Canned demos (no LLM, deterministic — start here)

```bash
python cli.py                # M-A/B: a normalize→validate pipeline, assembled + run
python cli.py --fan-in       # M-F: a shared regex_engine injected into two checkers (one construct)
python cli.py --deep         # M-G: a depth-2 tree (password_pipeline → length + complexity checks)
python cli.py --memo-test    # memoization: second build is instant from cache
```

These exercise the engine end-to-end with **canned** PLAN/IMPLEMENT/DERIVE_TESTS — no
backend, no key — and are the regression suite (keep them green).

### Live backends

Select with `RICH_BACKEND=claude|codex|openrouter`. The CLI and canvas default to
`claude`. A live build harness looks like:

```python
import backend, build
backend.install_from_env()                # monkeypatch the skills onto the selected backend
build.MAX_DEPTH = 3
root = build.build(MY_ROOT_CONTRACT, allow_decompose=True)   # live, recursive
build.assemble(root)                      # → build/main.py
```

Claude mode runs each skill as `claude -p --model <m> --tools "" --max-turns 6`.
Model routing (optional): `RICH_PLAN_MODEL` / `RICH_IMPL_MODEL` / `RICH_TESTS_MODEL`.

Codex mode treats Codex as module text generation only: one prompt in, one
schema-validated JSON object out. It runs from an empty temp directory with
`codex exec --ephemeral --sandbox read-only --ask-for-approval never --skip-git-repo-check`
and never needs write access to this repo. Optional: `RICH_CODEX_MODEL`.

OpenRouter mode uses `llm.py` (`OPENROUTER_API_KEY`, `RICH_MODEL`) for higher rate limits /
lower per-call overhead when desired.

### Resumption across a quota cut (first-class)

Subscription session limits are real. If a live build is cut (a `429`), **just re-run the
same build** (no reset): `build()` memo-hits every verified subtree, reuses prior live
decisions, and spends new calls only on the unbuilt remainder. An append-only **call
manifest** (`build/manifest.jsonl`) records one line per event — `live-PLAN`,
`live-IMPLEMENT`, `live-DERIVE_TESTS`, `live-INTEGRATION`, `live-REPLAN`, `memo-hit`,
`decision-reuse`, `manual-plan-reuse` — so a build's integrity is **auditable**: every node
was live-authored, hash-traceable, or explicitly marked as a hand-authored canvas plan.
For live gates, `manual-plan-reuse` remains a forbidden event. The manifest doubles as the
cost ledger.

### Inspect the tree (read-only)

```bash
python tree_viewer.py        # renders build/ → /tmp/rich_tree.html (structure + state views)
```

---

## 7. What is proven — the verification-boundary map

Established phase-by-phase on real live runs. The table distinguishes what is
proven *by a live end-to-end carry* from what is shown only by mechanism.

| Regime | Status | Evidence |
|---|---|---|
| **Linear dataflow pipeline** | ✅ proven live | P3 (`comment_ingest`), P9 |
| **Dataflow fan-in/out (diamond)** | ✅ proven live | P4 (`publish_article`), P9 (`statement_analyzer`) |
| **Held-capability fan-in** (a shared utility several leaves call) | ✅ proven live, end-to-end | P7 (`financial_report` — one `currency_formatter` injected into 3 consumers) |
| **Stateful module** (a class verified by operation sequences) | ✅ proven live | P6 (`todo_store`) |
| **Stateful composition** (a stateful core + readers) | ✅ proven live | P6 (via REPLAN to a dataflow shape) |
| **Shared-mutable state *verification*** | ✅ proven (mechanism + generation) | P8 — per-module fakes are *unsound* when modules share mutable state (the frame rule's disjoint-footprint side-condition); an **integration trace test over the real subtree** catches the false pass and is auto-generated from behavior prose |
| **A real, useful, wide-shallow build, end-to-end** | ✅ proven live | P9 (`statement_analyzer`, across a real ~4.7h quota cut, $1, 0 pins) |
| **Compositional depth (depth-2) — builds & computes** | ✅ proven live | **P10/P11** (`log_health_report` → `assess_health` decomposes into 3 analyses + a fan-in assembler; depth-2, uncoached; verifies, assembles, RUNS, and computes **correct aggregate output** on the real log batch) |
| **Compositional depth — *coherent* end-to-end delivery** | 🟡 **candidate GREEN, live proof pending** | The two located P11 residuals now have engine fixes: sibling + parent→child shape handoff, and local child replan instead of parent-wide rebuild. The required proof is a fresh Codex-backed Phase 11 gate. |
| **Algorithmic recursion** (e.g. a precedence parser) | ✅ correctly kept in **one leaf** | P10 (a full expression evaluator built correctly as a single leaf, 13/13) |

**The single-owner / stateless-dataflow regime is the sound, PLAN-preferred regime.** When
state is offered but not forced, PLAN routes around shared mutability toward a stateless fold
(P9). And **depth arises for the right reason**: compositional nesting (separable
sub-modules) summons depth-2 and it carries; algorithmic nesting (one recursive algorithm)
is correctly kept whole. RICH finds the depth that is really there.

---

## 8. Known boundaries / non-goals

- **No implement-then-check budget enforcer.** The base case is PLAN's disposition + the
  `MAX_DEPTH` cap. There is no mechanism that measures generated code and forces a split.
  (Confirmed in P10 §5.1 — the brief assumed one existed; it does not.) In practice PLAN's
  leaf-vs-decompose judgment is good, so this has not been a correctness problem — but depth
  cannot be *forced*, only offered by problem structure.

- **No cross-module conditional / branching composition.** Wiring is dataflow (threaded
  values + held capabilities). Conditionals, loops, and error-branching currently live
  *inside* leaves as ordinary code, not as graph structure between modules. The
  "composition-language" frontier (a module that routes between siblings on a condition) is
  not built.

- **Backend friction on large IMPLEMENT calls — mostly fixed (P11 Fix 1), residual remains.**
  `claude -p` now runs with `--tools ""` (zero tool affordance) + a no-tools system rule +
  `--max-turns 6`. This removed the `error_max_turns`/`stop_reason: tool_use` wall for ordinary
  leaves (render_report builds reliably). It still **recurs on harder generations** (a complex
  section-renderer hit it at `num_turns: 7`). Follow-up: higher max-turns / a per-node
  hard-generation retry. Firewall unchanged (`--disallowedTools` kept).

- **Depth delivery needs a fresh live proof.** The located P11 coherence fixes are now in the
  engine, but the claim should not be upgraded from candidate to proven until
  a fresh Codex-backed build passes with a fully live manifest.

- **Distribution / deployment topology is deferred.** Genuinely-unavoidable shared-mutable
  state *across processes* (separate services, a shared DB) is a future frontier; in-process
  shared state is verifiable today (P8).

- **The canvas is the product surface, and it closes the build loop.** `canvas.py` (JSON API
  + static server) and the React app in `web/` let you design a module tree, build it, and
  **see + fix the generated code in one place**: click a verified module to read its source
  and passing tests; click a failed one to see the failing test and error, then **rebuild
  just that module** (a scoped `invalidate_node` rebuild — verified siblings memo-hit, 0 extra
  LLM calls) or **vibe a fix**. Vibe edits and architecture proposals preview as a reviewable
  diff before they apply. Local-first / single-user; `tree_viewer.py` remains the standalone
  read-only inspector.

  Run it:
  ```bash
  npm --prefix web install      # once
  npm --prefix web run build    # build the frontend → web/dist
  python canvas.py              # serve API + app at http://localhost:8765
  # frontend dev (hot reload): npm --prefix web run dev  (Vite proxies /api → :8765)
  ```

- **Type vocabulary is closed** (`string|int|float|bool|list|dict`). No opaque
  module-defined types cross contract boundaries → **self-hosting is blocked**.

- **Verification is existential, not formal.** "Passed" = no violation observed on the
  derived tests' inputs.

---

## 9. Roadmap

- **The vibe architecture canvas** (design-then-compile). The next frontier is richer AI
  transactions: split/merge modules, refine contracts, explain graph diffs, and rebuild only
  the affected cone after a project-file change.
- **Run a fresh Codex-backed depth build** with a fully live manifest before
  upgrading coherent depth delivery from candidate to proven.
- **Backend robustness** for large IMPLEMENT calls — Fix 1 removed the `tool_use`/`max_turns`
  wall for ordinary leaves; it still recurs on harder generations (raise max-turns / per-node
  hard-generation retry).
- **Canvas hardening** — selective rebuild cones, edit validation, richer graph operations,
  and clearer manifest/provenance UX.
- **Named forks:** cross-module conditional composition; distribution / deployment topology;
  SMT / formal verification to replace existential tests; multi-language generation;
  self-hosting (needs opaque types).
