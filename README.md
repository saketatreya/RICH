# agentctl v1 — Runtime Harness for Agent-Native Software

> **The claim:** software designed for agent cognitive constraints — not human ones — changes the cost profile of agent reasoning. This harness proves the simplest version of that claim works today.

## What this is

A runtime harness that enforces **three constraints** on every agent operation:

| Constraint | Mechanism | Tested |
|---|---|---|
| **Information firewall** | Mediates every `read_file` — dependency source is physically inaccessible | ✅ |
| **Dependency injection** | Scans every `write_file` — imports of *any* module are blocked | ✅ |
| **Complexity budget** | Tracks LOC, files, tokens — refuses writes that exceed limits | ✅ |

The harness sits between the agent and its tools. It wraps `read_file`, `write_file`, `search_files`, and `terminal` — allowing or blocking each operation based on the module's declared boundaries.

## Table of Contents

1. [The enforced constraints](#the-enforced-constraints)
2. [Quickstart: 2-minute example](#quickstart-2-minute-example)
3. [Full test suite output](#full-test-suite-output)
4. [Architecture](#architecture)
5. [Module contracts](#module-contracts)
6. [Using the harness with an agent](#using-the-harness-with-an-agent)
7. [CLI companion tools](#cli-companion-tools)
8. [Repository layout](#repository-layout)
9. [Development](#development)

---

## The enforced constraints

### 1. Information Firewall

When an agent works on module `M`, it can read:

- `M`'s own source code
- `M`'s own test suite
- `M`'s contract.yaml
- The **contract.yaml** of each direct dependency

It **cannot** read:

- Any dependency's source code
- Any sibling module at all
- Anything outside the workspace

No convention. No "please don't read that." The harness blocks the operation.

### 2. Dependency Injection

Modules receive dependencies by injection, never by import. The harness scans every `write_file` for `import <module>` or `from <module> import ...` — **even if the module is a declared dependency** — and blocks the write.

```python
# ❌ Blocked — imports violate the firewall
import token_store
from user_repo import UserRepo

# ✅ Allowed — dependencies arrive by injection
def authenticate(username, password, *, user_repo, token_store):
    ...
```

### 3. Complexity Budget

Each module has a budget (LOC, files, estimated tokens). The harness tracks all writes in the session and refuses operations that would exceed the limit. Budgets are calibrated to the target agent's context window.

---

## Quickstart: 2-minute example

```python
from harness import Harness, FirewallBlocked

# Create a harness for the workspace
h = Harness(".")
session = h.session("auth")      # Start working on the 'auth' module

# ✅ READ: own source — allowed
session.read_file("modules/auth/src/auth.py")
# → "def authenticate(username, password, *, user_repo, token_store): ..."

# ✅ READ: dependency contract — allowed
session.read_file("modules/token_store/contract.yaml")
# → "name: token_store\ninterface:\n  operations:\n    - name: issue\n..."

# ❌ READ: dependency SOURCE — BLOCKED
session.read_file("modules/token_store/src/token_store.py")
# → FirewallBlocked: 'modules/token_store/src/token_store.py' is outside
#    module 'auth' boundary. You may only read files within your module
#    and dependency contracts.

# ❌ WRITE: importing a dependency — BLOCKED
session.write_file("modules/auth/src/bad.py",
    "import token_store\n\ndef do_thing():\n    pass\n")
# → FirewallBlocked: boundary violation — imports 'token_store'.
#    auth modules receive dependencies by INJECTION, never by import.

# ✅ WRITE: clean code — allowed
session.write_file("modules/auth/src/helper.py",
    "def format_token(token):\n    return token.upper()\n")
# → written successfully

# Track budget usage
session.budget_status()
# → "Budget: LOC 3/5000  files 1/100  tokens 14/100000"

session.stats_summary()
# → "Session: 5 operations (3 allowed, 2 blocked)"
```

---

## Full test suite output

The test suite exercises every constraint. Here's the actual output:

```
$ python3 test_harness.py
========================================================================
HARNESS TEST SUITE
========================================================================

Module: auth
Dependencies: ['token_store', 'user_repo']
Budget: 5000 LOC / 100 files / 100000 tokens

─── FIREWALL: Read Enforcement ───
  ✅ allow read: own source file
  ✅ allow read: own contract
  ✅ allow read: own test file
  ✅ allow read: dependency contract
  ✅ allow read: other dependency contract
  ✅ block read: dependency SOURCE (correctly blocked)
  ✅ block read: sibling module source (correctly blocked)
  ✅ block read: outside workspace entirely (correctly blocked)

─── FIREWALL: Write Enforcement ───
  ✅ allow write: new file in own src/
  ✅ allow write: new file in own tests/
  ✅ block write: dependency source (correctly blocked)
  ✅ block write: outside workspace (correctly blocked)

─── IMPORT BOUNDARY: Dependency Injection Enforcement ───
  ✅ block write: import undeclared dependency (token_store) (correctly blocked)
  ✅ block write: from-import undeclared dependency (correctly blocked)
  ✅ allow write: code without illegal imports

─── SEARCH: Scoped to Module Boundary ───
  ✅ allow search: own module source
  ✅ allow search: own tests
  ✅ allow search: dependency contracts
  ✅ block search: dependency source directory (correctly blocked)

─── BUDGET: Complexity Limits ───
      Budget: LOC 9/5000  files 3/100  tokens 43/100000
  ✅ budget tracking works
      Session: 19 operations (11 allowed, 8 blocked)
  ✅ session stats tracked

─── CONTEXT: Agent System Prompt ───
  ✅ context document generated
      Boundary defines 5 readable paths, 2 writable dirs
  ✅ boundary summary human-readable

========================================================================
RESULTS: 23/23 passed
        ALL TESTS PASSED ✅
========================================================================
```

---

## Architecture

Two layers. The harness is the runtime; the CLI is the companion.

```
harness.py          ← RUNTIME: mediates every agent tool call
    │
    ├── ModuleSession    — wraps read_file, write_file, search_files, terminal
    │   ├── read_file        → check whitelist → allow or FirewallBlocked
    │   ├── write_file       → check whitelist + scan imports + track budget
    │   ├── search_files     → scope to whitelisted directories
    │   ├── terminal         → heuristic path scanning
    │   ├── context()        → generated agent system prompt
    │   ├── boundary_summary() → what the agent may/may not access
    │   └── budget_status()  → current budget consumption
    │
    └── Harness           — session factory, validates workspace on load

agentctl.py         ← ADMIN CLI: static validation, scaffolding, DAG visualization
    ├── init              — scaffold new workspace
    ├── validate          — schema, DAG, budgets, boundaries (one-shot)
    ├── context           — materialize workspace tree for a module
    ├── graph             — print dependency DAG (text or DOT)
    └── wrap              — scaffold module around existing code
```

**Key difference:** `agentctl.py` validates at a point in time. `harness.py` enforces on every operation.

---

## Module contracts

Every module has a `contract.yaml` that defines its interface, dependencies, and behavioral properties:

```yaml
name: auth
version: 0.1.0

# ── MACHINE-READABLE (enforced by the harness) ──
interface:
  operations:
    - name: authenticate
      inputs:
        username: string
        password: string
      outputs:
        token: string
      errors:
        - invalid_credentials

dependencies:               # Direct deps only — defines the firewall
  - user_repo
  - token_store

# ── BEHAVIORAL (progressively formalizable) ──
behavior:
  - id: token_on_success
    prose: "On valid credentials, returns a token that token_store.validate accepts."
    formal: null
  - id: reject_invalid
    prose: "On invalid credentials, raises invalid_credentials and issues no token."
    formal: null
```

### The contract format is designed for progressive formalization

The machine-readable section (typed I/O, errors, deps) is fully enforceable today. The behavioral section has stable `id` fields and `formal: null` placeholders — v2 formalizes these one property at a time without changing the format.

### Type vocabulary (v1)

`string`, `int`, `float`, `bool`, `list<string>`, `list<int>`, `list<float>`, `list<bool>`

---

## Using the harness with an agent

The harness is a Python library. To use it with an agent:

### Option 1: Import and wrap

```python
from harness import Harness, FirewallBlocked, BudgetWarning

h = Harness(".")
session = h.session("auth")

# Inject the session's context into the agent's system prompt
system_prompt = session.context()

# Wrap every tool call the agent makes
def agent_read_file(path):
    try:
        return session.read_file(path)
    except FirewallBlocked as e:
        return f"[BLOCKED] {e}"

def agent_write_file(path, content):
    try:
        session.write_file(path, content)
        return f"[OK] written to {path}"
    except FirewallBlocked as e:
        return f"[BLOCKED] {e}"
    except BudgetWarning as e:
        return f"[BUDGET] {e}"

def agent_search_files(pattern, path):
    try:
        return session.search_files(pattern, path)
    except FirewallBlocked as e:
        return f"[BLOCKED] {e}"

# Give the agent its context + tool wrappers
# The agent can now work on 'auth' with enforced boundaries
```

### Option 2: Chroot into the materialized workspace

For stronger enforcement, use the CLI to materialize the workspace and chroot the agent:

```bash
agentctl context auth --out /tmp/auth-sandbox
bwrap --ro-bind /tmp/auth-sandbox /workspace --dev /dev --proc /proc \
      agent --workdir /workspace
```

The agent physically cannot escape — dependency source doesn't exist in the tree.

---

## CLI companion tools

The `agentctl.py` CLI provides static analysis and scaffolding:

```bash
# Validate the workspace
$ python agentctl.py validate
Workspace: /home/user/project
Modules: 3
  ✓ auth  ops=1  deps=[user_repo, token_store]  behavior=2
  ✓ token_store  ops=2  deps=[none]  behavior=2
  ✓ user_repo  ops=1  deps=[none]  behavior=1
All checks passed.

# Visualize the dependency DAG
$ python agentctl.py graph
Dependency DAG:
  [token_store]  ←  deps: none
  [user_repo]    ←  deps: none
  [auth]         ←  deps: user_repo, token_store

# Materialize a firewall tree
$ python agentctl.py context auth
Materialized context for 'auth' → .agentctl/workspaces/auth

# Scaffold a new module from existing code
$ python agentctl.py wrap sample.py --name greeter
Wrapped 'sample.py' → module 'greeter'
  Inferred 2 operation(s) from function signatures
```

---

## Repository layout

```
project/
├── harness.py               ← RUNTIME HARNESS: mediates every agent operation
├── agentctl.py              ← ADMIN CLI: static validation + scaffolding
├── test_harness.py          ← COMPREHENSIVE TEST: 23/23 proving the harness works
├── agentnative.yaml         ← Workspace config
├── .gitignore
├── modules/
│   ├── auth/
│   │   ├── contract.yaml    ← Declares deps: user_repo, token_store
│   │   ├── src/auth.py      ← Injection-based: receives deps as *, deps=...
│   │   └── tests/test_auth.py  ← Tests with FAKES, never imports real deps
│   ├── token_store/
│   │   ├── contract.yaml
│   │   ├── src/token_store.py
│   │   └── tests/test_token_store.py
│   └── user_repo/
│       ├── contract.yaml
│       ├── src/user_repo.py
│       └── tests/test_user_repo.py
└── .agentctl/               ← Generated workspace trees (gitignored)
```

---

## Development

```bash
# Run the harness test suite
python3 test_harness.py

# Run the example module tests
PYTHONPATH=modules/token_store/src python3 -m pytest modules/token_store/tests/ -v
PYTHONPATH=modules/user_repo/src python3 -m pytest modules/user_repo/tests/ -v
PYTHONPATH=modules/auth/src python3 -m pytest modules/auth/tests/ -v

# Static validation
python3 agentctl.py validate

# Show the dependency graph
python3 agentctl.py graph
python3 agentctl.py graph --dot | dot -Tpng -o dag.png
```

---

## What this proves

The harness proves a specific, load-bearing claim:

> **Structure alone — without new languages, without formal methods, without compilers — already changes what agents can reason about.**

An agent working on `auth` through this harness is informationally bounded. It sees its own code, its tests, and dependency contracts. Dependency implementations are physically inaccessible. The agent's reasoning cost is O(module), not O(codebase).

This is the cheap experiment that tells you whether to invest in v2 (formal contract language) and v3 (compiler). If structured codebases with enforced boundaries don't reduce agent reasoning cost, formal verification of structured codebases won't either.

---

> *"The information firewall must be enforced by construction, not by convention."*