# RICH — Close the Decomposition Gap — Fix Report

**Date:** 2026-06-01
**Scope touched (per §0):** `skills.py` `DERIVE_TESTS` (prompt + `derive_tests()` signature),
its call site in `build.py`, and `assemble()` (plus its private helper
`topological_order_for_assembly` and a new private helper `_injection_deps`, both of
which exist only to serve `assemble()`).
**Not touched (per §0):** `subagent_skill.py`, `build()` recursion / REPLAN, memoization,
DAG validation, `node.py`, `llm.py`, `PLAN`, `IMPLEMENT`. No edge-schema change was made
(the optional `from_output` field was **not** needed — see §1).

---

## 0. TL;DR

Both gaps are closed and the §5 P4 gate is **GREEN**: `process_text` decomposes
(`strip_html → collapse_ws → truncate`), **self-verifies through the unmodified `build()`
loop**, and the assembled `build/main.py` runs the real pipeline correctly.

```
process("<html>…<p>Some   body   text.</p>…")  ->  "TitleSome body text."   [OK]
process("<p>" + "x"*250 + "</p>")              ->  "x"*200                   [OK]
process("<p>Hello, <b>world</b>!</p>")         ->  "Hello, world!"           [OK]
process("<div>  word  </div>")                 ->  "word"                    [OK]
```

Cold end-to-end run through the Claude Code subagent backend: **8 LLM calls, ~100s, ~$0.18–0.30**,
all four nodes verified on the first attempt. Canned demos (`pipeline_demo`, `--fan-in`,
`--deep`, `--memo-test`) still pass — and two of them now produce *real* deliverables that
were previously silent stubs.

---

## 1. The two fixes are one composition — and where I deviated from the brief

The brief framed Fix 2 as: `assemble()` should **re-derive** the pipeline wiring from the
graph/edges, using a §2 "composition convention" (C1–C5), possibly needing a new
`from_output` edge field for the C3 threading rule.

**I did not do that, on purpose.** Re-deriving composition inside `assemble()` means the
wiring you *verify* (the `IMPLEMENT` class, exercised by the Fix-1 tests) is *different code*
than the wiring you *ship* in `main.py`. That is precisely the "verify X, ship Y" divergence
the spec warns against (D6 / §6.2 / Trap 2: *assembly must not think*), and it is what forces
the `from_output` schema change.

**What I did instead (Approach B):** `assemble()` **instantiates the already-verified
`IMPLEMENT` wiring class**, injecting dependencies by name. Composition stays authored and
verified in exactly one place (the `IMPLEMENT` class); the Fix-1 test exercises that same
class with fakes; `main.py` runs that same class with the real children. **Consistency by
construction, and no schema change.**

The §2 "convention" therefore collapses to a single shared **discovery rule** used by both
the test (Fix 1) and assembly (Fix 2): *the wiring class is the single class defined in the
node's own module*. That rule is the whole convention; C3 "threading" doesn't exist as a
separate concept because neither the test nor assembly threads anything — `IMPLEMENT` does,
once.

This is the one substantive change from the brief. Everything else follows the brief's intent.

---

## 2. The composition convention, as actually built (C1–C5)

- **C1 — module shape.** A node with **no injected dependencies** exports its operations as
  top-level functions. A node **with injected dependencies** exposes **exactly one class**
  (the wiring class) defined in its own module, whose `__init__` receives the deps as keyword
  parameters.
- **C2 — injection by name (D4).** Deps are injected by name, never imported. `assemble()`
  constructs each node **once** (shared deps → one instance) and injects the same instance
  everywhere it is needed.
- **C3 — composition is owned by `IMPLEMENT`.** The stage-to-stage data flow lives only in the
  verified wiring class. Neither the test nor `assemble()` re-derives it. *(This is the
  deviation from the brief; it removes the need for a `from_output` edge field.)*
- **C4 — discovery rule (shared by Fix 1 and Fix 2).** The wiring class is located by
  introspection: the single class `c` in the module with `c.__module__ == <module_id>`
  (imported helper classes like `re.Pattern` are ignored). The Fix-1 test discovers it this
  way and injects **fakes**; `assemble()` discovers it the same way and injects **reals**.
- **C5 — fail loud (§6).** If a deps-bearing module does not contain *exactly one* own class,
  `assemble()` raises `RuntimeError` rather than guess. v1 supports a single injected wiring
  class per node; anything else fails loudly.

**What gets injected, and by what name** — centralized in one helper, `build._injection_deps(node)`,
which is the single source of truth shared by construct-generation, the fold, and the
topological order:

```python
def _injection_deps(node):           # -> list[(param_name, source_id)]
    if node.children:                # internal: it composes its CHILDREN, by child id
        return [(c.id, c.id) for c in node.children]
    return [(d["name"], d["id"]) for d in (node.dependencies or [])]  # leaf w/ a declared dep
```

(Internal nodes key on **children**, not `contract.dependencies` — see the depth-2 finding in §6.)

---

## 3. Fix 1 — `DERIVE_TESTS` for internal nodes (result)

**Change.** `derive_tests(contract, dep_contracts=None, pipeline=False)`. When
`pipeline=True` and dep contracts are present, the system prompt gets an addendum
(`DERIVE_TESTS_INTERNAL_ADDENDUM`) that **overrides** the leaf rule "import the op as a
function". It instructs the generator to (a) discover the wiring class by introspection
(C4, so the test never depends on the class name `IMPLEMENT` happens to pick), (b) build a
fake per dependency honoring its declared output keys, (c) inject by name, and (d) assert on
the composed data-flow. The user prompt now includes the dependency contracts. The call site
in `build.py` was updated to build `dep_contracts` *before* `derive_tests` and pass
`dep_contracts=…, pipeline=True`.

**Result.** The subagent generated this test for the internal root (excerpt — full file in
`build/process_text/tests/test_process_text.py`), and the verified wiring class passed it:

```python
_mod = importlib.import_module("process_text")
WiringClass = next(c for _n, c in inspect.getmembers(_mod, inspect.isclass)
                   if c.__module__ == "process_text")        # C4 discovery

def _make_wiring(strip_result="stripped", collapse_result="collapsed", truncate_result="truncated"):
    class _FakeStripHtml:                                    # fakes honor dep contracts
        def strip(self, text): return {"result": strip_result}
    ...
    return WiringClass(strip_html=_FakeStripHtml(), collapse_ws=_FakeCollapseWs(),
                       truncate=_FakeTruncate())             # inject by name (D4)

def test_process_truncate_receives_collapse_ws_output():     # verifies COMPOSITION, not leaves
    ...
    assert received["text"] == "after_collapse"
```

This is genuine assume-guarantee verification: it assumes each child honors its contract and
checks only that `process_text` wires them in order — exactly what an internal node's tests
should do. Before the fix, `DERIVE_TESTS` emitted `from process_text import process` (a
function that doesn't exist on a dependency-injected class), so the root could never
self-verify.

---

## 4. Fix 2 — `assemble()` for any internal node (result)

**Change.** `gen_construct()` is now keyed on `_injection_deps(node)` (not the old
`node.id == "pipeline_demo"` hardcode and `pass # TODO` stub):

- **no injected deps** → emit a thin handle delegating each declared op to the module's
  top-level function, **module-qualified** (`_m.op`) so op names never collide;
- **injected deps** → import the module and `return _wiring_class(_m, '<id>')(**by_name)` —
  i.e. instantiate the *verified* class.

A `_wiring_class()` helper (emitted once into `main.py`) implements C4/C5. The top-level
`from <id> import *` block was removed (it was the source of a latent op-name collision —
see §6).

**Result.** Generated `build/main.py` for the gate:

```python
# Wiring node: process_text — instantiate verified class, inject ['strip_html', 'collapse_ws', 'truncate']
def construct_process_text(strip_html, collapse_ws, truncate):
    import process_text as _m
    return _wiring_class(_m, 'process_text')(strip_html=strip_html, collapse_ws=collapse_ws, truncate=truncate)

def construct_strip_html():
    import strip_html as _m
    class _Handle:
        def strip(self, *args, **kwargs):
            return _m.strip(*args, **kwargs)
    return _Handle()

def assemble():
    collapse_ws  = construct_collapse_ws()
    truncate     = construct_truncate()
    strip_html   = construct_strip_html()
    process_text = construct_process_text(strip_html=strip_html, collapse_ws=collapse_ws, truncate=truncate)
    return process_text
```

The class instantiated here is the *same* `ProcessText` the Fix-1 test verified:

```python
class ProcessText:
    def __init__(self, strip_html, collapse_ws, truncate): ...
    def process(self, text):
        r1 = self.strip_html.strip(text=text)
        r2 = self.collapse_ws.collapse(text=r1["result"])
        r3 = self.truncate.truncate(text=r2["result"])
        return {"result": r3["result"]}
```

---

## 5. Gate result (§5)

Harness: `gate_process_text.py`. It runs `process_text` through the **unmodified `build()`**
using the subagent backend for the two real skills under test. **PLAN is pinned** (the
decomposition is authored as the seed architecture; its real-LLM form was already validated in
Phase 1). This isolates the two fixes and makes the gate deterministic; `IMPLEMENT` and
`DERIVE_TESTS` are real subagent calls. `GATE_FRESH=1` forces a cold rebuild.

- **Fix 1 proven:** `build()` returns `verified` for `process_text` and all three leaves —
  the internal root self-verified against its own fake-injected tests.
- **Fix 2 proven:** `assemble()` → `main.py` runs the real pipeline; all four cases match
  (including the brief's two canonical cases).
- **Cost (cold):** 8 subagent calls (3 leaves × {DERIVE_TESTS, IMPLEMENT} + root × {DERIVE_TESTS,
  IMPLEMENT}; PLAN pinned), ~100s wall, ~$0.18–0.30, **zero retries**.

---

## 6. Things I changed because they weren't good (explicit, per your instruction)

1. **Re-derivation → instantiation (the big one).** Documented in §1. I rejected the brief's
   "assemble re-derives wiring" because it ships unverified composition; I instantiate the
   verified class instead. This also made the proposed `from_output` schema change unnecessary,
   so I made **no** schema change.

2. **Unified rule keyed on injected-deps, not `is_leaf`.** The old code branched on `is_leaf`.
   That is wrong for two real cases already in the repo:
   - **Fan-in (`--fan-in`):** `format_checker`/`domain_checker` are *leaves that take an
     injected dependency* (a class `FormatChecker(regex)`). The old leaf branch generated a
     wrapper delegating to a non-existent top-level `check_format` function; it "worked" only
     because the root op was a `pass` stub, so the broken wrappers were never called. Now they
     are instantiated correctly and the fan-in deliverable is **real** (it returns an actual
     dict instead of `None`).
   - **Depth-2 (`--deep`):** `password_pipeline` is a *non-root internal node*. Its
     `contract.dependencies` is `[]` (authored by its parent), yet it composes children
     `length_check`/`complexity_check`. Keying on `node.dependencies` (my first attempt)
     therefore mis-built it as a function module and the deliverable crashed
     (`module 'password_pipeline' has no attribute 'check'`). **An internal node's injected
     deps are its CHILDREN.** `_injection_deps` encodes this, and the depth-2 deliverable now
     wires and runs correctly end-to-end:
     ```
     validate(alice, pass1234) -> {'username_ok': True, 'password_ok': True, 'token': 'welcome_…'}
     validate(al, …)           -> {'username_ok': False, …, 'reason': 'Too short (min 3)'}
     ```

3. **Topological order must use the same truth.** `topological_order_for_assembly` built its
   dependency edges from `node.dependencies`, which is empty for non-root internal nodes — so
   children weren't ordered before their parent. It now uses `_injection_deps`, so children are
   always constructed first. (This helper exists only to serve `assemble()`, so it's within the
   §0 seam.)

4. **Fixed the `{name}={name}` fold bug.** The fold emitted `construct_x(dep=dep)` using the
   dep **name** on both sides. Whenever a dep's name ≠ its id (fan-in: `name="regex"`,
   `id="regex_engine"`) this referenced an undefined variable. The fold now injects
   `param_name = source_id` via `_injection_deps`.

5. **Removed `from <id> import *` from `main.py`.** It caused a latent collision: `--deep` has
   three leaves all exporting `check`, so the star-imports shadowed each other and the old leaf
   wrappers (which referenced the bare global) would have called the wrong function. Each
   `construct_*` now imports its own module and references it module-qualified — no globals, no
   collisions.

### Fail-loud cases verified (§6 / C5)
`_wiring_class` was unit-checked directly:
- module with **0** own classes → `RuntimeError` (refuses to guess);
- module with **2** own classes → `RuntimeError`;
- module with **1** own class + an imported class (`re.Pattern`) → returns the one own class
  (imported classes correctly ignored).

---

## 7. Consistency check (test wiring ≡ assembly wiring)

The Fix-1 test and the Fix-2 assembly drive the **same** object — the verified `ProcessText`
class — discovered by the **same** rule (C4) and injected by the **same** names (C2/D4). The
only difference is fakes (test) vs. reals (assembly). There is no second copy of the
composition logic anywhere, so they cannot diverge. This is the property the brief's §1 asked
for, achieved by *not* re-deriving.

---

## 8. Known limitations / observations (not fixed — out of §0 scope)

- **Real-mode depth-2 `IMPLEMENT` is still blind to children.** `build()` builds the
  `IMPLEMENT`/`DERIVE_TESTS` `dep_contracts` from `node.dependencies`, which is empty for
  non-root internal nodes. So a *real-LLM* depth-2 build can't tell a nested internal node what
  to inject. This lives in the `IMPLEMENT` call site (off-limits per §0) and the gate is
  depth-1, so it isn't exercised here. Canned depth-2 works because the canned impls already
  encode the right classes; my `assemble()` fix makes that canned deliverable run correctly.
  Recommended follow-up (separate change): source internal-node `dep_contracts` from children.

- **Generated `__main__` demo call is fixed-arity.** `main.py`'s `if __name__ == '__main__'`
  block calls `demo.<op>('test input')` with a single positional arg. For `--deep`
  (`validate(username, password)`) this raises `TypeError` — pre-existing, unrelated to these
  fixes, and harmless to the gate (op `process` is single-input). The `assemble()` function
  itself is correct; only the convenience demo line is naive.

---

## 9. Files changed

| File | Change |
|---|---|
| `skills.py` | `DERIVE_TESTS_INTERNAL_ADDENDUM` (new); `derive_tests()` gains `dep_contracts`/`pipeline` and branches the prompt for internal nodes. |
| `build.py` | Internal-node `derive_tests` call site passes `dep_contracts`/`pipeline`; `_injection_deps()` (new); `gen_construct()` rewritten; `_wiring_class` emitted into `main.py`; `from <id> import *` removed; fold and `topological_order_for_assembly` use `_injection_deps`. |
| `gate_process_text.py` | New §5 gate harness (pinned PLAN + real subagent IMPLEMENT/DERIVE_TESTS). |
