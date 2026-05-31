# RICH — Recursive Agent Build System

> A system that takes a single high-level goal and recursively decomposes it into a
> tree of modules, where each module is built by an LLM agent. Modules are specified
> by contracts authored by their parent, implemented against dependency contracts
> only (never source), verified by consumer-derived tests, and assembled by a
> deterministic topological fold that injects each module's dependencies into it,
> producing a runnable deliverable.

## Architecture

```
                    ┌─────────────────────────────┐
                    │  build(contract) → Node     │  ← single recursive procedure
                    └─────────────┬───────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
        ┌──────────┐       ┌────────────┐      ┌────────────┐
        │   PLAN   │       │ IMPLEMENT  │      │DERIVE_TESTS│    ← 3 LLM skills
        │ (architect)      │ (coder)    │      │ (tester)   │
        └──────────┘       └────────────┘      └────────────┘
              │                   │                   │
              ▼                   ▼                   ▼
        ┌──────────┐       ┌────────────┐      ┌────────────┐
        │ DAG      │       │ Retry loop │      │ Pytest     │    ← deterministic
        │ validate │       │ K_IMPL=3   │      │ subprocess │       engines
        └──────────┘       └────────────┘      └────────────┘
                                                     │
                                                     ▼
                                              ┌────────────┐
                                              │  assemble  │    ← topological fold
                                              │  → main.py │       + injection
                                              └────────────┘
```

**The entire system is one recursive procedure + three LLM skills + two deterministic engines.**

### The three LLM skills (non-deterministic)

| Skill | Role | Input | Output |
|-------|------|-------|--------|
| **PLAN** | Architect | Module contract | `{is_leaf: true}` or `{is_leaf: false, children: [...], edges: [...]}` |
| **IMPLEMENT** | Coder | Contract + dep contracts | Python source code |
| **DERIVE_TESTS** | Tester | Contract | pytest test file |

### The two deterministic engines (rock-solid)

| Engine | Role | Implementation |
|--------|------|---------------|
| **run_tests** | Verification | Pytest subprocess, timeout-guarded, captures pass/fail detail |
| **assemble** | Delivery | Topological fold, generates `build/main.py`, shared deps instantiated once |

### The central idea

**Contracts flow DOWN from demand, not UP from supply.** A module's contract describes
what it must provide to its consumer. The consumer (the parent) authors it. A module
never writes its own contract — it receives it as its task and is responsible only
for satisfying it.

### Key architecture properties

| # | Property | How |
|---|----------|-----|
| D1 | Contract authored by parent | PLAN's decomposition output IS the children's contracts |
| D2 | Leaf XOR internal | PLAN chooses; governed by budget |
| D3 | Implementation against dep CONTRACTS only | IMPLEMENT receives dep contracts, never source |
| D4 | Dependencies injected by name | Modules receive deps as named constructor params; never `import` |
| D5 | Firewall is the prompt | Single stateless LLM call; dep source never in context |
| D6 | Assembly is deterministic | Topological fold instantiates leaves, injects upward by name |
| D7 | Budget is the base case | PLAN decides leaf vs decompose; soft (PLAN) + hard (post-check) |
| D8 | Verification is existential | "Passed" = no violation observed on tested inputs — not a proof |
| D9 | Wiring is pipeline-only | Sequential/dataflow composition; no conditionals/loops in v1 |

## File Reference

| File | Lines | Purpose |
|------|-------|---------|
| `build.py` | 1057 | Core recursive procedure, verification engine, assembly engine, CLI, memoization, REPLAN, caps |
| `skills.py` | 742 | PLAN / IMPLEMENT / DERIVE_TESTS — LLM prompts + canned fallback data + contract validation |
| `llm.py` | 183 | OpenRouter client — call, retry with backoff, JSON parse defense, error types |
| `node.py` | 124 | Node dataclass, on-disk persistence (YAML/JSON), topological sort |
| `deep_test.py` | 194 | M-G canned test data for depth-2 recursion |
| `spec.md` | 272 | Full design document — locked decisions, milestones, contract schema, non-goals |

### `build.py` — Core driver

Constants:
```
K_IMPL = 3          Leaf implementation retry limit
K_WIRE = 3          Internal wiring retry limit
MAX_DEPTH = 3       Maximum recursion depth (hard cap)
MAX_CHILDREN = 8    Maximum children per node (hard cap)
MAX_LLM_CALLS = 50  Global LLM call ceiling (hard cap)
REPLANS_MAX = 2     Maximum replan attempts on child failure
```

Key functions:
```
build(contract, allow_decompose, use_canned, depth, llm_call_counter) → Node
run_tests(src_dir, tests_dir) → {passed, failures}
assemble(root) → main.py path
_contract_hash(contract) → 16-char SHA-256
_load_verified_node(contract) → Node | None      (memoization)
_save_memo(node, contract)                        (memoization)
_topo_sort_contracts(children, edges) → list      (DAG sort for dicts)
_validate_dag(children, edges, parent_id)         (cycle detection)
_validate_child_contracts(children, parent_id)    (schema validation)
_print_tree(node, indent)                         (debug)
test_single_leaf(module_id, desc)                 (--test-leaf)
test_decompose(desc, goal)                        (--decompose)
test_fan_in()                                     (--fan-in)
test_deep()                                       (--deep)
test_memo()                                       (--memo-test)
```

### `skills.py` — LLM skills + canned data

Functions:
```
plan(contract, allow_decompose) → decision       (LLM or fallback)
plan_canned(contract) → decision                 (always canned)
implement(contract, dep_contracts, pipeline, prior_failures) → source
implement_canned(contract, ...) → source         (always canned)
derive_tests(contract) → pytest source
derive_tests_canned(contract) → pytest source    (always canned)
```

Canned data:
```
CANNED_PIPELINE_DEMO_DECISION    normalize → validate (2 children, 1 edge)
CANNED_FAN_IN_DECISION           email checker — regex_engine shared by format + domain checker
CANNED_IMPLS                     10 canned source implementations
CANNED_TESTS                     10 canned pytest files
```

### `llm.py` — OpenRouter client

```
call_llm(system, user, ...) → raw text      One API call
call_with_retry(...) → raw text              Exponential backoff (3 attempts)
parse_json_response(raw, context) → dict     Defensive JSON parsing
is_available() → bool                        API key check
```

Error types: `LLMError`, `LLMParseError` (dumps raw response to disk), `LLMNotConfigured`

Config: `RICH_MODEL` env var, defaults to `google/gemini-2.0-flash-001`. API key via `OPENROUTER_API_KEY`.

### `node.py` — Node model

```
Node(id, contract, is_leaf, children, edges, dependencies)   Dataclass
save_contract(node)        → build/<id>/contract.yaml
save_decision(node)        → build/<id>/decision.json
save_status(node, status)  → build/<id>/status.json
save_deps(node)            → build/<id>/deps.yaml
topological_order(children, edges) → sorted Node list
```

## On-Disk Layout

Each node is a directory under `build/`:

```
build/<id>/
  contract.yaml      # Authored by parent (or root seed)
  decision.json      # {is_leaf: true} or {is_leaf: false, children: [...], edges: [...]}
  deps.yaml          # Resolved dependencies [{name, id}, ...]
  src/               # Implementation source
    <id>.py
  tests/             # Pytest from DERIVE_TESTS
    test_<id>.py
  status.json        # {status: planned|implemented|verified|failed, reason?}
  memo.txt           # SHA-256 hash of contract (for memoization)
build/main.py         # Generated entrypoint — assembly fold
```

## Contract Schema

```yaml
id: <unique string>              # Also the node directory name
description: <one-line goal>
interface:
  operations:
    - name: <op name>
      inputs:  {<param>: <type>}          # string|int|float|bool|list<...>
      outputs: {<param>: <type>}
      errors:  [<error name>, ...]
dependencies:                              # Present on internal nodes
  - name: <inject_param_name>
    id: <child id this name binds to>
behavior:                                   # Consumer-authored, prose in v1
  - id: <stable prop id>
    prose: <what must be true>
```

## CLI Reference

```bash
# Canned pipeline demo — always works, zero LLM calls
python build.py

# Single-module LLM generate + verify (PLAN→leaf, IMPLEMENT, DERIVE_TESTS, run_tests)
python build.py --test-leaf <module_id> --contract "<description>"

# Decomposition pipeline — PLAN can decompose, all skills real LLM
python build.py --decompose "<desc>" --contract "<goal description>"

# Shared dependency test (fan-in) — regex_engine instantiated once
python build.py --fan-in

# Depth-2 recursion test — validate_registration → password_pipeline → children
python build.py --deep

# Memoization test — build twice, second run is ~0.01s from cache
python build.py --memo-test
```

## Milestone History

Built in strict milestone order per `spec.md` §8. Each milestone leaves the system runnable.

| Milestone | Commit | What | Test |
|-----------|--------|------|------|
| **M-A** | `aa2f647` | Skeleton + node model + build() recursion + canned pipeline demo (normalize→validate). **Zero LLM.** | `python build.py` |
| **M-B** | `ea5737a` | Real pytest verification (subprocess) + real assembly (topological fold → `main.py`). Deterministic back-half trustworthy. | `python build.py` |
| **M-C** | `c0d406b` | Real IMPLEMENT + DERIVE_TESTS via OpenRouter. Retry+backoff, parse defense, prior-failure injection, canned fallback. | `--test-leaf` |
| **M-D** | `ab88106` | Real PLAN (leaf-only). All 3 skills real — full autonomous single-module loop. | `--test-leaf` |
| **M-E** | `54b4f90` | Decomposition enabled. PLAN authors child contracts, DAG validation, depth-1 recursion. MVP architecture proven. | `--decompose` |
| **M-F** | `a642ea0` | Shared dependency (fan-in). regex_engine instantiated once, injected into both format_checker and domain_checker. | `--fan-in` |
| **M-G** | `02fa0e1` | Depth>1 recursion, REPLAN on child failure, hard caps (max_depth/max_children/max_llm_calls), memoization (contract hash). | `--deep`, `--memo-test` |

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `OPENROUTER_API_KEY` | (required) | OpenRouter API key for LLM calls |
| `RICH_MODEL` | `google/gemini-2.0-flash-001` | Model for all LLM skills |

## Running the Tests

```bash
# All canned tests (0 LLM cost):
python build.py                    # Pipeline demo
python build.py --fan-in           # Shared dependency
python build.py --deep             # Depth-2 recursion
python build.py --memo-test        # Memoization

# Real LLM tests (costs API credits):
python build.py --test-leaf reverse_str --contract "Reverse a string"
python build.py --decompose "text_pipeline" --contract "Count chars then check > 50"
```

## How to Add a New Canned Test

1. Define a `ROOT_CONTRACT` dict with `id`, `description`, `interface`, `dependencies`, `behavior`
2. Register a matching decision in `plan_canned()` (in `skills.py`)
3. Add source implementations to `CANNED_IMPLS` keyed by module id
4. Add pytest files to `CANNED_TESTS` keyed by module id
5. For internal nodes (decomposition), add the child contracts to the decision's `children` array
6. Wire a new `--flag` and `test_*()` function in `build.py`

## Implementation Notes

### Canned functions must be resolved at call time, not import time

`build()` calls `skills.plan_canned(contract)` via `import skills as _skills` at the call
site. This is intentional — test functions patch `skills.plan_canned` at runtime. If you
use a module-level `from skills import plan_canned`, the patch won't take effect.

### Leaf vs pipeline IMPLEMENT modes

- **Leaf:** Export top-level functions (no classes). Tests import functions directly.
- **Pipeline:** Use a class with `__init__` receiving deps. Compose dependencies sequentially.
  Do NOT reimplement logic that belongs in children.

### JSON parse defense for LLM output

LLMs produce invalid JSON containing unescaped special characters (`\{`, `\}`, `\'`).
The parse defense in `llm.py`:
1. Strips markdown fences (` ```json ... ``` `)
2. Fixes backslashes before non-standard JSON escape characters via regex
3. Uses `json.loads(text, strict=False)`
4. On failure, dumps raw response to `/tmp/rich_parse_failure_*.txt`

### Memoization

Before building a node, `build()` calls `_load_verified_node(contract)` which:
1. Checks `build/<id>/memo.txt` exists
2. Compares stored SHA-256 hash against current contract hash
3. If match + status is "verified" → returns cached Node from disk
4. If not → proceeds with full build

After successful verification, `_save_memo(node, contract)` writes the contract hash.

### Decision persistence

PLAN's raw output (with children contracts) must be persisted immediately as
`decision.json` via `_save_raw_decision()`. The Node's `children` attribute is
empty at that point — this function serializes the PLAN dict directly.

### Topological sort

Two versions exist:
- `node.topological_order(children: list[Node], edges)` — for Node objects
- `build._topo_sort_contracts(children: list[dict], edges)` — for contract dicts (used during build before children are constructed)

Both detect cycles via DFS with three-color marking (WHITE/GRAY/BLACK).

### Recursion depth and caps

`build()` tracks `depth` (parent + 1) and a shared `llm_call_counter` (mutable list).
Hard caps are checked at entry:
- `depth > MAX_DEPTH` → raises BuildFailure
- `llm_call_counter >= MAX_LLM_CALLS` → raises BuildFailure
- `len(children) > MAX_CHILDREN` → raises BuildFailure at decomposition site

### REPLAN / backtracking

When a child fails, the parent's build loop catches `BuildFailure` and:
1. Logs the failed child id and reason
2. Calls `plan(contract, allow_decompose=True)` again for a fresh decomposition
3. Updates `children_contracts` and `edges` from the new decision
4. Restarts the child-building loop
5. Capped at `REPLANS_MAX` attempts; if exhausted, propagates the failure

## Design Decisions (Locked, from spec.md §3)

These are not open for relitigation. They are load-bearing for parts of the system not yet built.

| ID | Decision |
|----|----------|
| D1 | A node's contract is authored by its parent. PLAN's output includes full children contracts. |
| D2 | A node is leaf XOR internal. PLAN chooses, governed by budget. |
| D3 | Implementation is written against dependency CONTRACTS, never dependency source. |
| D4 | Dependencies are injected by NAME, not imported. Name-keying makes assembly deterministic. |
| D5 | The firewall for v1 is the prompt. Implementation is a single stateless LLM call. |
| D6 | Assembly is deterministic. No LLM involved. Shared dep instantiated once. |
| D7 | The budget is the recursion's base case. PLAN judges + post-implementation size check. |
| D8 | Verification is running consumer-derived tests. It is existential, not a proof. |
| D9 | v1 wiring is pipeline-only. No conditional routing, error-branching, or loops. |

## Non-Goals (from spec.md §9)

- No filesystem firewall / sandbox / OS jail
- No "compiler agent" or intelligent assembler
- No SMT / formal / machine-checked behavioral verification
- No non-pipeline wiring (conditionals, error-branching, rollback, loops)
- No global reconciliation component
- No self-hosting, no multi-language
- No rich type system beyond string|int|float|bool|list<...>
