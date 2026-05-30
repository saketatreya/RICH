# RICH — Rishav's Insane Coding Harness

Runtime enforcement of agent-native software architecture. Two phases, two jobs:

| Phase | Job | Mechanism |
|-------|-----|-----------|
| **v1** | Every agent tool call mediated against module boundaries | `harness.py` — `ModuleSession` wraps `read_file`, `write_file`, `search_files`, `terminal` |
| **v2** | Every function call checked against formal contracts with blame | `runtime_checker.py` — `contract_checked()`, `DependencyProxy`, expression evaluator |

135 tests. 21 commits. Built test-first, bootstrapped from v1.

---

## Why

Most software is structured for human programmers. When you replace humans with AI agents, you lose all the social coordination technologies (shared mental models, code reviews, hallway conversations) while inheriting every structural assumption those practices produced. Agents must constantly trace imports across module boundaries, build mental models of entire codebases, track down hidden dependencies.

RICH inverts this: **design software for agent cognitive constraints.** Three constraints — enforced by construction, not convention — collapse agent reasoning cost from O(whole codebase) to O(current module).

---

## Quickstart

### v1: Runtime Harness

```python
from harness import Harness, FirewallBlocked

h = Harness(".")
session = h.session("auth")

# ✅ Allowed — within module boundary
session.read_file("modules/auth/src/auth.py")
session.read_file("modules/token_store/contract.yaml")  # dep contract only
session.write_file("modules/auth/src/helper.py", "def f(): pass")

# ❌ Blocked — crosses firewall
session.read_file("modules/token_store/src/token_store.py")   # FirewallBlocked
session.write_file("...", "import token_store")                # FirewallBlocked (D4)

# Info
session.context()          # Full system prompt for the agent
session.boundary_summary() # What the agent may/may not access
session.budget_status()    # LOC/files/tokens consumed
```

### v2: Formal Contract Language

```python
from runtime_checker import contract_checked
from properties import PostconditionProperty, RaisesProperty

# Wrap an implementation with contract checking
checked = contract_checked(
    authenticate,
    postconditions=[
        PostconditionProperty(id="token_on_success",
            expr="len(result.token) > 0")
    ],
    raises_props=[
        RaisesProperty(id="reject_invalid",
            when="not deps.user_repo.verify_password(username, password).ok",
            error="invalid_credentials")
    ],
)

# Postcondition enforced on success, raises property on failure
checked(username="admin", password="secret", ...)  # ✅
checked(username="admin", password="wrong", ...)   # → AuthError("invalid_credentials")
```

---

## Table of Contents

- [v1: Runtime Harness](#v1-runtime-harness)
  - [The Three Constraints](#the-three-constraints)
  - [Commands](#commands)
  - [API Reference](#v1-api-reference)
  - [Using With an Agent](#using-with-an-agent)
- [v2: Formal Contract Language](#v2-formal-contract-language)
  - [Property Kinds](#property-kinds)
  - [Expression Language](#expression-language)
  - [Runtime Checker](#runtime-checker)
  - [Blame Assignment](#blame-assignment)
- [Contracts](#contracts)
  - [Format Reference](docs/contracts.md)
- [Expression Language](docs/expression-language.md)
  - [Grammar](docs/expression-language.md#grammar)
  - [Type System](docs/expression-language.md#type-system)
- [Example Project](#example-project)
- [Testing](#testing)
- [Architecture](#architecture)
- [3-Phase Program](#3-phase-program)
- [Pitfalls](#pitfalls)

---

## v1: Runtime Harness

`harness.py` mediates every agent tool call against module boundaries. `rich.py` provides static validation and scaffolding.

### The Three Constraints

**1. Information Firewall.** An agent working on `auth` reads:

```
modules/auth/src/auth.py          # ✅ own source
modules/auth/contract.yaml        # ✅ own contract
modules/token_store/contract.yaml # ✅ dep CONTRACT ONLY
modules/token_store/src/*         # ❌ BLOCKED — dep source
modules/user_repo/src/*           # ❌ BLOCKED — sibling source
```

Enforced on every `read_file`, `write_file`, `search_files`, `terminal`.

**2. Dependency Injection (D4).** Modules receive dependencies by injection, never `import`. The harness scans every `write_file` for `import X` or `from X import Y` — even for declared dependencies — and blocks the write.

```python
# ❌ BLOCKED
import token_store

# ✅ ALLOWED
def authenticate(username, password, *, user_repo, token_store):
    ...
```

**3. Complexity Budget.** LOC, file count, estimated tokens tracked per session. Writes that would exceed the budget raise `BudgetWarning`. Budgets set per-workspace in `agentnative.yaml`, overridable per-module in `contract.yaml`.

### Commands

```bash
rich init             # Scaffold workspace (agentnative.yaml + modules/ + .gitignore)
rich validate         # Schema, DAG, budgets, boundary scan — exit non-zero on failure
rich context <module> # Materialize firewall tree for chroot/bwrap deployment
rich graph            # Print DAG (--dot for Graphviz)
rich wrap <file> --name <name>  # Scaffold module from existing code
```

### v1 API Reference

```python
class Harness:
    def __init__(self, workspace_root: str = ".")
    def session(self, module_name: str) -> ModuleSession
    def module_names(self) -> list[str]
    def validate(self) -> bool

class ModuleSession:
    # Tool mediation
    def read_file(self, path: str) -> str                     # → FirewallBlocked if OOB
    def write_file(self, path: str, content: str) -> None      # → FirewallBlocked or BudgetWarning
    def search_files(self, pattern: str, path: str = None,
                     target: str = "content") -> list[str]     # → FirewallBlocked if OOB
    def terminal(self, command: str) -> tuple[int, str]        # heuristic scan only

    # Information
    def context(self) -> str              # Full system prompt for the agent
    def boundary_summary(self) -> str     # What the agent may/may not access
    def budget_status(self) -> str        # LOC/files/tokens consumed
    def stats_summary(self) -> str        # Allowed vs blocked count

    # Properties
    module: Module                        # The module the agent works on
    deps: list[Module]                    # Direct dependency modules
    budget: Budget                        # Effective budget
    graph: dict[str, list[str]]           # Dependency adjacency list

class FirewallBlocked(Exception):
    operation: str  # e.g. "read_file"
    detail: str     # Human-readable explanation

class BudgetWarning(Exception):
    metric: str     # "LOC", "files", or "tokens"
    used: int       # Current usage
    limit: int      # Budget limit
```

### Using With an Agent

**Option 1: Import and wrap.** Create a session, inject `session.context()` into the system prompt, wrap every tool call through `session.read_file`, `session.write_file`, etc.

**Option 2: Chroot.** Materialize the workspace tree (`rich context auth`) and `bwrap`/`chroot` the agent into it. Physical filesystem enforcement — dependency source doesn't exist.

**Option 3: Both.** Harness for in-process enforcement (catches imports, tracks budgets) + chroot for filesystem isolation.

---

## v2: Formal Contract Language

v2 makes the `formal: null` placeholder real. Three files:

| File | Phase | Job |
|------|-------|-----|
| `properties.py` | v2.0 | Property schema — `kind` discriminator, parser, validation |
| `expr_lang.py` | v2.1 | Expression language — parser, AST, type checker |
| `runtime_checker.py` | v2.2 | Evaluator, `contract_checked()`, `DependencyProxy` |

### Property Kinds

| Kind | When Checked | Example Property | Status |
|------|-------------|-----------------|--------|
| `postcondition` | After each successful call | `len(result.token) > 0` | ✅ v2.2 |
| `raises` | Guard before call + error match | `not deps.X.Y(...).ok → invalid_credentials` | ✅ v2.2 |
| `trace_invariant` | After each call against history | Token uniqueness across calls | ✅ v2.2 |
| `temporal` | Cross-call temporal logic | `G(issue(t) → within(86400, validate(t)))` | Deferred to v2.4 |
| `nonfunctional` | N/A — declared out-of-scope | Constant-time comparison, latency | Declared only |

**Schema:**

```yaml
behavior:
  - id: token_on_success
    prose: "On valid credentials, returns a non-empty token."
    formal:
      kind: postcondition
      expr: "len(result.token) > 0"

  - id: reject_invalid
    prose: "On invalid credentials, raises invalid_credentials."
    formal:
      kind: raises
      when: "not deps.user_repo.verify_password(username, password).ok"
      error: "invalid_credentials"

  - id: token_uniqueness
    prose: "Each call to issue produces a unique token."
    formal:
      kind: trace_invariant
      expr: "true"

  - id: token_validity_window
    prose: "Tokens valid 24h, then expired_token."
    formal:
      kind: temporal
      expr: "G(issue(t) → within(86400, validate(t)))"

  - id: constant_time_compare
    prose: "Password comparison is constant-time."
    formal:
      kind: nonfunctional
```

### Expression Language

A small, total, side-effect-free predicate language. Same AST for runtime evaluation today and SMT compilation in v2.3.

```
expr     → or_expr
or_expr  → and_expr ("or" and_expr)*
and_expr → not_expr ("and" not_expr)*
not_expr → "not" not_expr | comparison
comparison → term (CMP_OP term)?
term     → factor (ADD_OP factor)*
factor   → unary (MUL_OP unary)*
unary    → "-" unary | primary
primary  → bool_lit | number | string | "(" expr ")"
         | "result" ("." IDENT)?
         | "len" "(" expr ")"
         | "deps." IDENT "." IDENT ("(" args ")")? ("." IDENT)?
         | IDENT
```

Special forms:
- `result.field` — access return value field
- `deps.module.op(args...).field` — call dependency, extract field
- `len(expr)` — string/list length

Full grammar and type system: [docs/expression-language.md](docs/expression-language.md)

### Runtime Checker

**Wrapping an implementation:**

```python
from runtime_checker import contract_checked

checked = contract_checked(
    fn,
    postconditions=[...],    # checked after successful return
    raises_props=[...],      # guard checked before call, error matched on raise
    trace_invariants=[...],  # checked after each call against accumulated history
    op_name="authenticate",
)
```

**Wrapping a dependency with blame:**

```python
from runtime_checker import DependencyProxy

proxy = DependencyProxy(
    dep=RealTokenStore(),
    module_name="token_store",
    op_name="issue",
    preconditions=[...],     # if violated → blame CALLER
    postconditions=[...],    # if violated → blame DEP
)

proxy(subject="alice")  # contract checked at the injection boundary
```

**Expression evaluation:**

```python
from expr_lang import parse_expr
from runtime_checker import EvalContext, evaluate

ctx = EvalContext(
    inputs={"username": "alice", "password": "secret"},
    result={"token": "abc123"},
    deps={"user_repo": FakeUserRepo()},
)

evaluate(parse_expr("len(result.token) > 0"), ctx)          # → True
evaluate(parse_expr("deps.user_repo.verify_password(username, password).ok"), ctx)  # → True|False
```

### Blame Assignment

`ContractViolation` carries the blamed party:

```
[token_store] postcondition violation: returns_token — expected token to be non-empty
```

The injection point (D4) is the one place in the system where contract violations have an unambiguous responsible party. `DependencyProxy` sits at that exact seam.

---

## Contracts

Every module has a `contract.yaml`. The contract is the interface — the single artifact that crosses the firewall.

**Format reference:** [docs/contracts.md](docs/contracts.md)

**Type vocabulary (v1):** `string`, `int`, `float`, `bool`, `list<string>`, `list<int>`, `list<float>`, `list<bool>`

**v1→v2 seam:** The `formal` field widens from `Optional[str]` to a structured object with `kind`. Old contracts with `formal: null` still parse correctly — `parse_formal_property(None, id)` returns `None`.

---

## Example Project

Three modules demonstrating every constraint:

```
token_store (no deps) ──┐
                         ├──→ auth
user_repo (no deps) ────┘
```

| Module | Operations | Dependencies | Key Design |
|--------|-----------|-------------|------------|
| `token_store` | `issue(subject) → {token}`, `validate(token) → {subject}` | None | In-memory, 24h expiry. Temporal + trace properties. |
| `user_repo` | `verify_password(username, password) → {ok}` | None | Constant-time hash comparison. Nonfunctional property. |
| `auth` | `authenticate(username, password) → {token}` | user_repo, token_store | **Injection-based.** Never imports deps. Raises + postcondition properties. |

**Tests:** auth's tests use fakes — never the real dependency implementations. The `test_authenticate_does_not_import_deps` test verifies D4 physically by scanning auth's source for import statements.

---

## Testing

```bash
# ── v1 ──
python3 test_harness.py                              # 23/23 — every firewall constraint
python3 rich.py validate                              # static: schema, DAG, budgets, boundaries
PYTHONPATH=modules/token_store/src python3 -m pytest modules/token_store/tests/ -v  # 4 tests
PYTHONPATH=modules/user_repo/src python3 -m pytest modules/user_repo/tests/ -v      # 3 tests
PYTHONPATH=modules/auth/src python3 -m pytest modules/auth/tests/ -v                 # 4 tests

# ── v2 ──
python3 test_properties.py                            # 15/15 — property schema + classifier
python3 test_expr_lang.py                             # 40/40 — parser, AST, type checker
python3 test_runtime_checker.py                       # 28/28 — evaluator, checker, proxy
python3 test_v2_integration.py                        # 18/18 — real modules, all 4 properties

# ── Total: 135 tests ──
```

---

## Architecture

```
harness.py               ← v1 RUNTIME: mediates every agent tool call
  ├── ModuleSession      — read_file, write_file, search_files, terminal
  └── Harness            — session factory

rich.py                  ← v1 CLI: validate, context, graph, wrap, init

runtime_checker.py       ← v2 RUNTIME: contract checking + blame
  ├── contract_checked() — wraps implementation with postcondition/raises/trace checks
  ├── DependencyProxy    — wraps dependency handle at injection boundary
  ├── EvalContext        — execution context for expression evaluation
  └── evaluate()         — walks expression AST

expr_lang.py             ← v2 LANGUAGE: expression parser + type checker
  ├── parse_expr()       — string → AST
  ├── TypeChecker        — validates against I/O types + dep contracts
  └── AST nodes          — Literal, Variable, ResultAccess, DepCall, ...

properties.py            ← v2 SCHEMA: property kind classifier
  ├── PropertyKind       — postcondition | raises | trace_invariant | temporal | nonfunctional
  └── parse_formal_property() — raw dict → FormalProperty subclass
```

---

## 3-Phase Program

| Phase | Scope | Status |
|-------|-------|--------|
| **v1 — Harness** | Filesystem-level firewall, DAG, budgets. Runtime tool mediation. | ✅ Done |
| **v2 — Language** | Formal contract language. Property kinds, expression language, runtime checker with blame. | ✅ Done (v2.0–2.3) |
| v2.3 — Static checker | SMT compilation for decidable expression core. ∀-guarantees. | Future |
| v2.4 — Temporal | Controllable clock, state-machine models. | Future |
| v3 — Compiler | Composition language, deployment compilation, contract verification. | Future |

---

## Pitfalls

**Harness boundary checking (static vs runtime).** `rich.py`'s `check_module_boundaries` only flags undeclared deps. `harness.py`'s `_scan_imports` blocks ALL module imports (per D4). The harness is stricter.

**Search scoping.** `session.search_files()` raises `FirewallBlocked` on out-of-bounds paths — not an empty result set. Intentional: ambiguity is worse than a hard block.

**Terminal is heuristic.** `session.terminal()` does basic path scanning, not sandboxing. Use `rich context` + `chroot`/`bwrap` for strong isolation.

**Budget is session-scoped.** `SessionStats` resets per session. Track externally for cumulative enforcement.

**Nonfunctional + temporal are skipped.** `NonfunctionalProperty` and `TemporalProperty` are explicitly not checked at runtime. They're declared for documentation and deferred to future phases.

**Dep calls actually execute.** `deps.X.Y(args...).field` calls the real dependency at evaluation time. The dep handle must be in `EvalContext.deps`. This is assume-guarantee: verify the caller assuming the dep satisfies its contract.

**Transitive deps are invisible.** D2 says direct-only. An agent working on `auth` cannot see `token_store`'s dependencies. By design — transitive concerns are encapsulated.

**Old contracts still work.** `formal: null` parses as `None`. v2 is an extension, not a rewrite.

---

> *"The information firewall must be enforced by construction, not by convention."*
