# agentctl v1 — Agent-Native Software Harness

> **Keystone idea:** The information firewall must be enforced **by construction, not by convention.**

## Table of Contents

1. [Thesis](#thesis)
2. [The Three Constraints](#the-three-constraints)
3. [Installation](#installation)
4. [Quickstart: 5-minute Walkthrough](#quickstart-5-minute-walkthrough)
5. [Command Reference](#command-reference)
6. [Module Contracts](#module-contracts)
7. [Architecture](#architecture)
8. [Repository Layout](#repository-layout)
9. [Development](#development)
10. [3-Phase Program](#3-phase-program)

---

## Thesis

Most software is structured for human programmers — unrestricted code access, implicit dependencies, giant context requirements. Agents inherit those structures and pay for them in long-horizon reasoning. `agentctl` inverts this: it structures a codebase so an **agent's reasoning surface for any task is O(one module), not O(whole codebase).**

`agentctl` v1 does not make agents smarter and does not introduce any new language or compiler. It proves that **three enforceable constraints, achievable with today's tooling, already collapse the reasoning surface.**

---

## The Three Constraints

### 1. Information Firewalls (Materialized)

The weak version is a tool that *prints* "here are the files the auth agent may read." That is just documentation — the agent can still read a sibling module's source. **We do not build that.**

The strong version: when an agent works on module `M`, `agentctl` materializes a working tree that **physically contains M's own source + tests + contract, plus only the *contract files* of M's direct dependencies.** No dependency implementation exists in that tree. The agent cannot read what is not there.

```
.agentctl/workspaces/auth/
├── CONTEXT.md              # Agent instructions: what you may/may not see
├── contract.yaml           # auth's own contract
├── src/                    # auth's own source (EDITABLE)
├── tests/                  # auth's own tests (EDITABLE)
└── deps/
    ├── user_repo/
    │   └── contract.yaml   # CONTRACT ONLY — no src/
    └── token_store/
        └── contract.yaml   # CONTRACT ONLY — no src/
```

### 2. Explicit Dependency DAG

Every module declares its dependencies by name in `contract.yaml`. The harness builds a DAG and enforces it:

- **Cycles are rejected** at validation time (DFS with 3-coloring, prints the offending cycle path).
- **Boundary violations are flagged** — if a module's source imports a sibling module not listed in its dependencies, `validate` reports it.
- The information horizon is **direct-only** — an agent editing `M` sees the contracts of `M`'s direct dependencies, never transitive dependencies, never their source.

### 3. Complexity Budgets

Each module has a budget — non-blank LOC, file count, and estimated context tokens. `validate` rejects modules that exceed their budget. Budgets can be set per-workspace (default) or per-module (override).

This keeps modules size-bounded so an agent can fit the *entire* module in its context window.

---

## Installation

```bash
# Requirements: Python 3.10+, PyYAML
pip install pyyaml

# Clone or copy agentctl.py into your project
# That's it — single file, standard library + PyYAML.
```

Verify:

```bash
$ python agentctl.py --help
usage: agentctl [-h] {validate,context,graph,wrap,init} ...
```

---

## Quickstart: 5-minute Walkthrough

This walkthrough builds a 3-module project from scratch and demonstrates every constraint.

### Step 1: Initialize the workspace

```bash
$ python agentctl.py init
Workspace scaffolded at /home/user/myproject
  agentnative.yaml  — workspace config
  modules/          — module directory
  .gitignore        — ignores .agentctl/
```

This creates:

```bash
myproject/
├── agentnative.yaml     # Workspace config (budget defaults, directory names)
├── .gitignore           # Ignores .agentctl/
└── modules/             # Where your modules live
```

**`agentnative.yaml`:**

```yaml
module_root: modules
source_dir: src
tests_dir: tests
budget:
  max_loc: 5000
  max_files: 100
  max_context_tokens: 100000
```

### Step 2: Create the first module — `token_store`

Write a contract and implementation:

```bash
# Create directory
mkdir -p modules/token_store/{src,tests}
```

**`modules/token_store/contract.yaml`:**

```yaml
name: token_store
version: 0.1.0

interface:
  operations:
    - name: issue
      inputs:
        subject: string
      outputs:
        token: string
      errors: []
    - name: validate
      inputs:
        token: string
      outputs:
        subject: string
      errors:
        - invalid_token
        - expired_token

dependencies: []   # token_store has no dependencies

behavior:
  - id: token_validity_window
    prose: "Issued tokens validate for 24h from issue, then yield expired_token."
    formal: null
  - id: token_uniqueness
    prose: "Each call to issue produces a unique token."
    formal: null
```

**`modules/token_store/src/token_store.py`:**

```python
import secrets, time

class TokenStore:
    def __init__(self, ttl_seconds=86400):
        self._store = {}
        self._ttl = ttl_seconds

    def issue(self, subject):
        token = secrets.token_hex(32)
        self._store[token] = {"subject": subject, "issued_at": time.time()}
        return {"token": token}

    def validate(self, token):
        rec = self._store.get(token)
        if rec is None: raise Exception("invalid_token")
        if time.time() - rec["issued_at"] > self._ttl:
            raise Exception("expired_token")
        return {"subject": rec["subject"]}
```

### Step 3: Create the second module — `user_repo`

**`modules/user_repo/contract.yaml`:**

```yaml
name: user_repo
version: 0.1.0

interface:
  operations:
    - name: verify_password
      inputs:
        username: string
        password: string
      outputs:
        ok: bool
      errors:
        - unknown_user

dependencies: []

behavior:
  - id: constant_time_compare
    prose: "Password comparison is constant-time over a stored hash."
    formal: null
```

**`modules/user_repo/src/user_repo.py`** — hashed passwords, `hmac.compare_digest` for constant-time comparison.

### Step 4: Create `auth` — depends on both

**`modules/auth/contract.yaml`:**

```yaml
name: auth
version: 0.1.0

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

dependencies:
  - user_repo
  - token_store

behavior:
  - id: token_on_success
    prose: "On valid credentials, returns a token that token_store.validate accepts."
    formal: null
  - id: reject_invalid
    prose: "On invalid credentials, raises invalid_credentials and issues no token."
    formal: null
```

**`modules/auth/src/auth.py`** — **critical: dependencies received by injection, never imported:**

```python
class AuthError(Exception):
    pass

def authenticate(username, password, *, user_repo, token_store):
    try:
        result = user_repo.verify_password(username, password)
    except Exception:
        raise AuthError("invalid_credentials")
    if not result.get("ok"):
        raise AuthError("invalid_credentials")
    return token_store.issue(username)
```

> **Notice:** `auth` never does `import user_repo` or `import token_store`. Dependencies arrive as injected arguments (`*, user_repo, token_store`). Tests verify this is physically true — the boundary scanner also catches it.

### Step 5: Validate everything

```bash
$ python agentctl.py validate
Workspace: /home/user/myproject
Modules: 3
  ✓ auth  ops=1  deps=[user_repo, token_store]  behavior=2
  ✓ token_store  ops=2  deps=[none]  behavior=2
  ✓ user_repo  ops=1  deps=[none]  behavior=1
All checks passed.
```

### Step 6: Visualize the DAG

```bash
$ python agentctl.py graph
Dependency DAG:
  [token_store]  ←  deps: none
  [user_repo]    ←  deps: none
  [auth]         ←  deps: user_repo, token_store
```

```bash
$ python agentctl.py graph --dot
digraph agentnative {
  rankdir=LR;
  node [shape=box, style=rounded];
  "user_repo" -> "auth";
  "token_store" -> "auth";
  "token_store";
  "user_repo";
}
```

### Step 7: Materialize the firewall (the keystone)

```bash
$ python agentctl.py context auth
Materialized context for 'auth' → .agentctl/workspaces/auth

  Tree structure:
  auth/
    CONTEXT.md
    contract.yaml
    deps/
      token_store/
        contract.yaml          # ← CONTRACT ONLY
      user_repo/
        contract.yaml          # ← CONTRACT ONLY
    src/
      auth.py                  # ← auth's source (EDITABLE)
    tests/
      test_auth.py             # ← auth's tests (EDITABLE)
```

**This is the whole point.** An agent pointed at this tree can read `deps/user_repo/contract.yaml` to learn `user_repo`'s interface, but **user_repo's implementation is physically absent.** The agent's reasoning surface is bounded.

### Step 8: What goes wrong when constraints are violated

#### Cycle detection

Add a dependency cycle between two modules:

```yaml
# cycle_a depends on cycle_b, which depends on cycle_a
```

```bash
$ python agentctl.py validate
  ✗ [DAG] cycle detected: cycle_a → cycle_b → cycle_a

1 error(s) found.
```

#### Budget violation

Set a module's budget to 3 LOC, but write 7 lines of code:

```bash
$ python agentctl.py validate
  ✗ [budget_test] LOC budget exceeded: 7 > 3

1 error(s) found.
```

#### Boundary violation

A module imports a sibling it didn't declare as a dependency:

```python
# In user_repo/src/main.py:
import token_store   # ← not in user_repo's dependencies
```

```bash
$ python agentctl.py validate
  ✗ [user_repo] boundary: src/main.py imports 'token_store'
    which is not in declared dependencies

1 error(s) found.
```

#### Schema validation

Deliberately malformed contract — wrong types, missing fields, bad references:

```bash
$ python agentctl.py validate
  ✗ [bad_module] operation 'do_thing' input 'x': type 'foobar_type'
    is not in v1 type vocabulary
  ✗ [bad_module] duplicate operation name 'do_thing'
  ✗ [bad_module] dependency 'nonexistent' does not reference an existing module
  ✗ [bad_module] behavior property id is required
  ✗ [bad_module] behavior property id is required

5 error(s) found.
```

### Step 9: Run the tests (contract-based, with fakes)

`auth`'s tests use **fakes** — never the real implementations:

```python
class FakeUserRepo:
    """Satisfies the user_repo contract without importing user_repo."""
    def verify_password(self, username, password):
        return {"ok": self._users.get(username) == password}

class FakeTokenStore:
    """Satisfies the token_store contract without importing token_store."""
    def issue(self, subject):
        return {"token": f"fake-{subject}"}

def test_authenticate_success():
    user_repo = FakeUserRepo({"alice": "pass123"})
    token_store = FakeTokenStore()
    result = authenticate("alice", "pass123",
                          user_repo=user_repo, token_store=token_store)
    assert "token" in result
```

```bash
$ PYTHONPATH=modules/auth/src python -m pytest modules/auth/tests/ -v

test_authenticate_success PASSED
test_authenticate_wrong_password PASSED
test_authenticate_unknown_user PASSED
test_authenticate_does_not_import_deps PASSED   # ← verifies D4 physically

4 passed
```

---

## Command Reference

### `agentctl validate`

Runs all checks — schema, DAG, budgets, boundaries — across the workspace. Exits non-zero on any failure.

```
$ agentctl validate
Workspace: /path/to/project
Modules: 3
  ✓ auth  ops=1  deps=[user_repo, token_store]  behavior=2
  ✓ token_store  ops=2  deps=[none]  behavior=2
  ✓ user_repo  ops=1  deps=[none]  behavior=1
All checks passed.
```

### `agentctl context <module> [--out DIR] [--list]`

Materializes the firewall tree for a module. The agent is pointed at this tree — it contains the module's source + tests + contract, plus **only the contract files** of direct dependencies.

- `--list`: Dry-run, prints what would be included without writing.
- `--out DIR`: Custom output directory (default: `.agentctl/workspaces/<module>`).

### `agentctl graph [--dot]`

Prints the dependency DAG as text (default) or Graphviz DOT (with `--dot`).

### `agentctl wrap <path> --name <name>`

Scaffolds a new module around existing code. Copies the source into `modules/<name>/src/`, generates a best-effort `contract.yaml` by inferring function signatures from Python files.

```bash
$ cat sample.py
def hello(name: str) -> str:
    return f"Hello, {name}"

$ agentctl wrap sample.py --name greeter
Wrapped 'sample.py' → module 'greeter'
  Created: modules/greeter/
    src/   — source files copied here
    tests/ — empty (add tests here)
    contract.yaml — skeleton (REVIEW AND FILL IN)
  Inferred 1 operation(s) from function signatures
```

### `agentctl init`

Scaffolds a new workspace in the current directory — writes `agentnative.yaml`, creates `modules/`, writes `.gitignore`.

---

## Module Contracts

Every module has a `contract.yaml` at its root. The contract is the **interface** a module exposes to its dependents — it is the single artifact that crosses the firewall.

```yaml
name: auth                  # Must match directory name
version: 0.1.0

# ── MACHINE-READABLE (fully enforced in v1) ──
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
  - id: token_on_success    # Stable id; v2 fills `formal:` in place
    prose: "On valid credentials, returns a token that token_store.validate accepts."
    formal: null
  - id: reject_invalid
    prose: "On invalid credentials, raises invalid_credentials and issues no token."
    formal: null

# ── BUDGET (optional override) ──
budget:
  max_loc: 2000
  max_files: 25
```

### Type vocabulary (v1)

`string`, `int`, `float`, `bool`, `list<string>`, `list<int>`, `list<float>`, `list<bool>`

### Contract as the v1→v2 seam

The behavioral section is designed for progressive formalization. Each property has a **stable `id`** and a `formal: null` placeholder. v2 fills `formal:` with machine-verifiable specifications one property at a time — an extension, not a rewrite.

---

## Architecture

### Single-file design

The entire harness is `agentctl.py` (~650 lines). It stays under its own default complexity budget.

### Dependency injection by convention

Modules code against their dependency's *interface* (defined in `contract.yaml`) and receive dependency handles as **injected arguments** — never via `import`. This gives "tested through contracts, no dependency implementation needed" for free.

```python
# auth/src/auth.py — never imports user_repo or token_store
def authenticate(username, password, *, user_repo, token_store):
    ...
```

### Token estimation

Token estimation in v1 is approximate: `ceil(len(text) / 4)`. It's a single function so a real tokenizer can replace it later without changing the budget infrastructure.

### Validation pipeline

```
validate:
  1. Schema validation    (M1)  — contract structure, types, ids
  2. DAG + cycle detect   (M2)  — DFS with 3-coloring
  3. Budget checks        (M4)  — LOC, files, estimated tokens
  4. Boundary heuristic   (M5)  — import scanning
```

All run in a single pass. Any failure → non-zero exit.

---

## Repository Layout

```
project/
├── agentnative.yaml          # Workspace config
├── agentctl.py               # The harness (single file)
├── .gitignore                # Ignores .agentctl/
├── modules/                  # All modules live here
│   ├── auth/
│   │   ├── contract.yaml
│   │   ├── src/
│   │   └── tests/
│   ├── token_store/
│   │   ├── contract.yaml
│   │   ├── src/
│   │   └── tests/
│   └── user_repo/
│       ├── contract.yaml
│       ├── src/
│       └── tests/
└── .agentctl/                # Generated, gitignored
    └── workspaces/
        └── auth/             # Materialized firewall for auth
```

---

## Development

```bash
# Run all tests
PYTHONPATH=modules/token_store/src python3 -m pytest modules/token_store/tests/ -v
PYTHONPATH=modules/user_repo/src python3 -m pytest modules/user_repo/tests/ -v
PYTHONPATH=modules/auth/src python3 -m pytest modules/auth/tests/ -v

# Validate everything
python3 agentctl.py validate

# Visualize the dependency graph
python3 agentctl.py graph
python3 agentctl.py graph --dot | dot -Tpng -o dag.png
```

### Build milestones

The project was built in 8 linear milestones, each committed to git:

| Milestone | Commit | What |
|-----------|--------|------|
| M0 | `43eeb2c` | Scaffolding: argparse + dataclasses |
| M1 | `5b43ff1` | Contract parsing, discovery, schema validation |
| M2 | `88be42e` | DAG build, DFS cycle detection, graph command |
| **M3** | `dab7f60` | **Materialized firewall — the keystone** |
| M4 | `67104ea` | Budget checks (LOC, files, tokens) |
| M5 | `73119ec` | Boundary heuristic (import scanning) |
| M6 | `964be52` | `wrap` and `init` commands |
| M7 | `ff06d00` | Example project + acceptance suite |

---

## 3-Phase Program

`agentctl` v1 is part of a larger program. v1 has **standalone value** even if later phases never ship.

| Phase | Scope | Status |
|-------|-------|--------|
| **v1 — Harness** | Filesystem-level firewall, DAG, budgets. This repo. | ✅ Done |
| v2 — Language | Progressively formalizable contract/composition language | Future |
| v3 — Compiler | Verifies contracts, derives deployment topology | Future |

The v1→v2 seam is guarded by the contract format: stable `id` fields + `formal: null` placeholders + hard machine/prose split.

---

## Non-Goals (v1)

- No formal verification, SMT, refinement types — that is v2/v3
- No composition language, conditional routing, fan-out/fan-in
- No deployment compilation or topology selection
- No OS-level sandbox — "source not in the tree" is the v1 firewall
- No real tokenizer, no plugins, no network features, no rich type system

---

> *"The information firewall must be enforced by construction, not by convention."*
