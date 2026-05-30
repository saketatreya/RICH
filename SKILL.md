---
name: agentctl
description: Runtime harness for agent-native software — enforces information firewalls, dependency injection, and complexity budgets on every agent tool call. Use this skill whenever working on or with the agentctl project.
triggers:
  - agentctl
  - firewall
  - harness
  - ModuleSession
  - contract.yaml
  - agentnative
  - dependency injection
  - information firewall
---

# agentctl — Runtime Harness for Agent-Native Software

## What it is

A Python harness that mediates every agent tool call (`read_file`, `write_file`, `search_files`, `terminal`) against three constraints:

1. **Information firewall** — agents cannot read dependency source code, only contracts
2. **Dependency injection** — agents cannot `import` any other module; dependencies arrive as injected arguments
3. **Complexity budget** — LOC, file count, and token limits enforced per module

The harness sits between the agent and its tools. Operations that cross module boundaries are blocked with `FirewallBlocked` or `BudgetWarning`.

## Project structure

```
/home/zaphod/dev/rich/
├── harness.py          ← RUNTIME: ModuleSession mediates every agent tool call
├── agentctl.py         ← ADMIN CLI: static validate, context, graph, wrap, init
├── test_harness.py     ← 23/23 test suite proving every constraint
├── agentnative.yaml    ← workspace config (module_root, budgets)
├── modules/            ← example project
│   ├── auth/           ← deps: user_repo, token_store (injection-based)
│   ├── token_store/    ← no deps (in-memory, 24h expiry)
│   └── user_repo/      ← no deps (constant-time password verify)
└── .agentctl/          ← generated workspace trees (gitignored)
```

## How to use the harness

### Creating a session

```python
from harness import Harness, FirewallBlocked, BudgetWarning

h = Harness(".")
session = h.session("auth")  # agent works on 'auth'
```

### What the agent may read

- `modules/<module>/src/*` — own source code
- `modules/<module>/tests/*` — own test files
- `modules/<module>/contract.yaml` — own contract
- `modules/<dep>/contract.yaml` — dependency contracts (INTERFACE ONLY, no source)

### What the agent may write

- `modules/<module>/src/*` — own source (imports of any module are scanned and blocked)
- `modules/<module>/tests/*` — own test files

### What is BLOCKED

- Reading dependency source (`modules/token_store/src/*`)
- Reading sibling module source (`modules/user_repo/src/*`)
- Reading anything outside the workspace
- Writing dependency source
- Writing with `import <module>` or `from <module> import ...` — even declared deps

### Getting context

```python
session.context()           # Full system prompt for the agent
session.boundary_summary()  # What the agent may/may not access
session.budget_status()     # Current budget consumption
session.stats_summary()     # Operations allowed vs blocked
```

## Key conventions (DO NOT VIOLATE)

### D4: Dependency Injection

```python
# ❌ WRONG — will be blocked by the harness
import token_store
from user_repo import UserRepo

# ✅ CORRECT — dependencies arrive by injection
def authenticate(username, password, *, user_repo, token_store):
    result = user_repo.verify_password(username, password)
    return token_store.issue(username)
```

### D2: Direct-only dependencies

An agent editing `auth` sees the contracts of `auth`'s **direct** dependencies — never their source, never transitive dependencies. The information horizon is one dependency deep.

### D3: Contracts must be complete enough to code against blind

If an agent must peek at a dependency's implementation to call it correctly, the firewall has already failed. Every dependency's `contract.yaml` must specify: `name`, typed `inputs`, typed `outputs`, enumerated `errors`.

### Tests use fakes, never real implementations

```python
# ✅ CORRECT
class FakeTokenStore:
    def issue(self, subject):
        return {"token": f"fake-{subject}"}

def test_auth():
    result = authenticate("alice", "pass",
                          user_repo=FakeUserRepo(),
                          token_store=FakeTokenStore())
```

## Running tests

```bash
# Harness test suite (proves all 3 constraints)
python3 test_harness.py

# Module unit tests
PYTHONPATH=modules/token_store/src python3 -m pytest modules/token_store/tests/ -v
PYTHONPATH=modules/user_repo/src python3 -m pytest modules/user_repo/tests/ -v
PYTHONPATH=modules/auth/src python3 -m pytest modules/auth/tests/ -v

# Static validation
python3 agentctl.py validate

# Visualize dependency graph
python3 agentctl.py graph
python3 agentctl.py graph --dot
```

## Contract format

```yaml
name: <module_name>         # Must match directory name
version: 0.1.0

interface:                  # Machine-readable (enforced now)
  operations:
    - name: <op_name>
      inputs:
        <param>: <type>     # string, int, float, bool, list<T>
      outputs:
        <param>: <type>
      errors:
        - <error_name>

dependencies:               # Direct deps by name — defines the firewall
  - <dep_name>

behavior:                   # Progressively formalizable (prose now, formal later)
  - id: <stable_id>         # v2 fills formal: in place
    prose: "<description>"
    formal: null
```

## Type vocabulary (v1)

`string`, `int`, `float`, `bool`, `list<string>`, `list<int>`, `list<float>`, `list<bool>`

## When editing harness.py

- `ModuleSession.__init__` builds whitelists from workspace + module + deps
- `_build_whitelists` sets readable and writable paths based on contracts
- `read_file`, `write_file`, `search_files`, `terminal` all call `_is_under_whitelist`
- `_scan_imports` blocks ALL module imports (even declared deps) per D4
- `_IMPORT_RE` catches both `import X` and `from X import Y`
- `FirewallBlocked` and `BudgetWarning` are the two blocking exceptions

## When editing agentctl.py (CLI)

- Single file, Python 3.10+, stdlib + PyYAML only
- Dataclasses: `Budget`, `Operation`, `Contract`, `Module`, `Workspace`
- `load_workspace` discovers modules from `agentnative.yaml`
- `validate_workspace` runs schema checks (name match, types, ids, deps exist)
- `detect_cycles` uses DFS with 3-coloring
- `check_module_budget` counts non-blank LOC, files, estimated tokens
- `check_module_boundaries` scans for imports of sibling modules not in deps (static check, less strict than harness)
- `estimate_tokens` is `ceil(len/4)` — single function for future tokenizer replacement

## Pitfalls

- The static boundary check in `agentctl.py` only flags undeclared deps. The harness in `harness.py` blocks ALL module imports. These intentionally differ — the harness is stricter (D4 full enforcement).
- `search_files` in harness scopes to whitelisted directories only. Searching from a path outside the whitelist raises `FirewallBlocked`.
- `terminal` in harness does heuristic path scanning, not full enforcement. For strong terminal isolation, use `bwrap` or `chroot` on a materialized workspace tree.
- When adding new modules, remember to create `src/` and `tests/` directories — the harness whitelists them even if empty.
