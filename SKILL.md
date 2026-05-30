---
name: rich
description: RICH — Rishav's Insane Coding Harness. Runtime enforcement of information firewalls, dependency injection, and complexity budgets. v2 adds a formal contract language with expression evaluator, type checker, and runtime contract checking with blame assignment. Use this skill whenever working on or with the rich project.
triggers:
  - rich
  - RICH
  - firewall
  - harness
  - ModuleSession
  - contract.yaml
  - agentnative
  - dependency injection
  - information firewall
  - formal property
  - postcondition
  - raises
  - trace_invariant
  - nonfunctional
  - expression language
  - runtime checker
---

# RICH — Rishav's Insane Coding Harness

## v1: Runtime Harness

### What it is

A Python harness that mediates every agent tool call against three constraints: information firewall, dependency injection, complexity budget.

### Core files

- `harness.py` — ModuleSession mediates every agent tool call
- `rich.py` — CLI: validate, context, graph, wrap, init
- `test_harness.py` — 23/23 test suite

### Key conventions (D4)

```python
# ❌ BLOCKED
import token_store

# ✅ ALLOWED
def authenticate(username, password, *, user_repo, token_store):
    ...
```

### Running tests

```bash
python3 test_harness.py                          # 23/23
python3 rich.py validate                          # static check
PYTHONPATH=modules/auth/src python3 -m pytest modules/auth/tests/ -v
```

## v2: Formal Contract Language

### Core files

- `properties.py` — v2.0: PropertyKind enum, FormalProperty subclasses, parser
- `expr_lang.py` — v2.1: recursive-descent parser, AST, type checker
- `runtime_checker.py` — v2.2: evaluator, ContractChecker, DependencyProxy

### Property kinds

| Kind | When checked | Example |
|------|-------------|---------|
| `postcondition` | After each successful call | `len(result.token) > 0` |
| `raises` | Guard before call + error match after | `not deps.X.Y(...).ok → invalid_credentials` |
| `trace_invariant` | After each call against history | token uniqueness |
| `temporal` | Deferred to v2.4 | `G(issue(t) → within(86400, validate(t)))` |
| `nonfunctional` | Declared out-of-scope | constant-time, latency |

### Contract format (v2)

```yaml
behavior:
  - id: token_on_success
    prose: "..."
    formal:
      kind: postcondition
      expr: "len(result.token) > 0"

  - id: reject_invalid
    prose: "..."
    formal:
      kind: raises
      when: "not deps.user_repo.verify_password(username, password).ok"
      error: "invalid_credentials"
```

### Expression language

- Grammar: comparisons, boolean (and/or/not), arithmetic (+, -, *, /), `len()`, parentheses
- Special forms: `result.field`, `deps.module.op(args...).field`
- Type checker validates against declared I/O types + dependency contracts
- Same AST for runtime evaluation and future SMT compilation

### Runtime checker

```python
from runtime_checker import contract_checked, DependencyProxy

# Wrap implementation with contract checking
checked = contract_checked(authenticate,
    postconditions=[...], raises_props=[...])

# Wrap dependency with blame at injection boundary
proxy = DependencyProxy(dep=TokenStore(),
    module_name="token_store", op_name="issue",
    postconditions=[...])
```

### Running v2 tests

```bash
python3 test_properties.py       # 15 tests: property schema + classifier
python3 test_expr_lang.py        # 40 tests: parser, AST, type checker
python3 test_runtime_checker.py  # 28 tests: evaluator, contract checker, proxy
python3 test_v2_integration.py   # 18 tests: all 4 properties against real modules
```

### Pitfalls

- `rich.py` static boundary check only flags undeclared deps; harness blocks ALL imports
- `search_files` in harness raises FirewallBlocked on out-of-bounds paths (not empty results)
- `terminal` is heuristic path scanning — use `chroot`/`bwrap` for strong isolation
- `nonfunctional` and `temporal` properties are intentionally skipped at runtime
- `deps.X.Y(args...).field` actually calls the dependency at runtime — the dep handle must be in EvalContext
- ContractViolation carries `blamed` field identifying caller vs dep
