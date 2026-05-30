---
name: rich
description: RICH — Rishav's Insane Coding Harness. Runtime enforcement of information firewalls, dependency injection, and complexity budgets on every agent tool call. Use this skill whenever working on or with the rich project.
triggers:
  - rich
  - RICH
  - Rishav
  - firewall
  - harness
  - ModuleSession
  - contract.yaml
  - agentnative
  - dependency injection
  - information firewall
---

# RICH — Rishav's Insane Coding Harness

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
├── rich.py             ← ADMIN CLI: static validate, context, graph, wrap, init
├── test_harness.py     ← 23/23 test suite proving every constraint
├── agentnative.yaml    ← workspace config (module_root, budgets)
├── modules/            ← example project
│   ├── auth/           ← deps: user_repo, token_store (injection-based)
│   ├── token_store/    ← no deps (in-memory, 24h expiry)
│   └── user_repo/      ← no deps (constant-time password verify)
└── .agentctl/          ← generated workspace trees (gitignored)
```

## CLI

```bash
rich init             # Scaffold workspace (agentnative.yaml + modules/ + .gitignore)
rich validate         # Schema, DAG, budgets, boundaries — exit non-zero on failure
rich context <module> # Materialize firewall tree for a module
rich graph            # Print dependency DAG (--dot for Graphviz)
rich wrap <file> --name <name>  # Scaffold module around existing code
```

## How to use the harness

```python
from harness import Harness, FirewallBlocked, BudgetWarning

h = Harness(".")
session = h.session("auth")
```

### Allowed

- `session.read_file("modules/auth/src/*")` — own source
- `session.read_file("modules/auth/tests/*")` — own tests
- `session.read_file("modules/auth/contract.yaml")` — own contract
- `session.read_file("modules/token_store/contract.yaml")` — dep contract ONLY
- `session.write_file("modules/auth/src/*")` — own source (import scanned)
- `session.write_file("modules/auth/tests/*")` — own tests
- `session.search_files("pattern", "modules/auth/")` — scoped to boundary

### Blocked

- `session.read_file("modules/token_store/src/*")` — dep source → FirewallBlocked
- `session.read_file("modules/user_repo/src/*")` — sibling source → FirewallBlocked
- `session.read_file("/etc/passwd")` — outside workspace → FirewallBlocked
- `session.write_file("modules/token_store/src/evil.py")` — dep source → FirewallBlocked
- `session.write_file("...", "import token_store")` — import violation → FirewallBlocked
- `session.write_file("...", "from user_repo import ...")` — import violation → FirewallBlocked
- `session.search_files("def issue", "modules/token_store/src")` — out of bounds → FirewallBlocked

### Info methods

- `session.context()` — full system prompt for the agent
- `session.boundary_summary()` — what the agent may/may not access
- `session.budget_status()` — current budget consumption
- `session.stats_summary()` — operations allowed vs blocked

## Key conventions

### D4: Dependency Injection

```python
# ❌ BLOCKED by the harness
import token_store
from user_repo import UserRepo

# ✅ CORRECT
def authenticate(username, password, *, user_repo, token_store):
    result = user_repo.verify_password(username, password)
    return token_store.issue(username)
```

### D2: Direct-only dependencies

An agent editing `auth` sees the contracts of `auth`'s **direct** dependencies — never their source, never transitive dependencies.

### D3: Contracts must be complete enough to code against blind

Every dependency's `contract.yaml` must specify: `name`, typed `inputs`, typed `outputs`, enumerated `errors`.

### Tests use fakes, never real implementations

```python
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
python3 test_harness.py                          # 23/23 harness tests
rich.py validate                                  # static validation
rich.py graph                                     # DAG visualization

# Module unit tests
PYTHONPATH=modules/token_store/src python3 -m pytest modules/token_store/tests/ -v
PYTHONPATH=modules/user_repo/src python3 -m pytest modules/user_repo/tests/ -v
PYTHONPATH=modules/auth/src python3 -m pytest modules/auth/tests/ -v
```

## Contract format

```yaml
name: <module_name>
version: 0.1.0
interface:
  operations:
    - name: <op_name>
      inputs: {<param>: <type>}
      outputs: {<param>: <type>}
      errors: [<error_name>]
dependencies: [<dep_name>]
behavior:
  - id: <stable_id>         # v2 fills formal: in place
    prose: "<description>"
    formal: null
```

Types: `string`, `int`, `float`, `bool`, `list<string>`, `list<int>`, `list<float>`, `list<bool>`

## Editing rules

- `harness.py`: `_scan_imports` blocks ALL module imports (even declared deps) per D4
- `rich.py`: static boundary check only flags undeclared deps — less strict than harness
- `estimate_tokens` is `ceil(len/4)` — single function, replace with real tokenizer later
- `_generative_context_md` is unused by the harness (which uses `_generate_context_str`) but kept for the CLI `context` command

## Pitfalls

- Harness `terminal` is heuristic path scanning, not full enforcement. Use `bwrap`/`chroot` for strong isolation.
- `search_files` in harness scopes to whitelist only — searching from an out-of-bounds path raises `FirewallBlocked`.
- The CLI `context` command materializes a tree; the harness enforces boundaries in-process — two different mechanisms, same contract structure.
