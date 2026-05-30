# RICH — Rishav's Insane Coding Harness

> **The claim:** Software designed for agent cognitive constraints — not human ones — changes the cost profile of agent reasoning. This harness proves the simplest version of that claim works today.

RICH is a runtime harness that sits between an AI agent and its tools, enforcing three constraints on every operation:

1. **Information firewall** — agents cannot read dependency source code, only their contracts
2. **Dependency injection** — agents cannot `import` any other module; dependencies arrive as injected arguments
3. **Complexity budget** — LOC, file count, and token limits enforced per module

These three constraints collapse agent reasoning cost from O(whole codebase) to O(current module). The firewall is enforced by construction, not by convention — operations that cross it are blocked, not warned about.

---

## Table of Contents

1. [Why this exists](#why-this-exists)
2. [Installation](#installation)
3. [The three constraints in detail](#the-three-constraints-in-detail)
4. [Quickstart: using the harness](#quickstart-using-the-harness)
5. [Full test suite walkthrough](#full-test-suite-walkthrough)
6. [CLI reference](#cli-reference)
7. [API reference](#api-reference)
8. [Contract format specification](#contract-format-specification)
9. [Architecture and design decisions](#architecture-and-design-decisions)
10. [Example project: auth system](#example-project-auth-system)
11. [The 3-phase program](#the-3-phase-program)
12. [Development guide](#development-guide)
13. [Pitfalls and edge cases](#pitfalls-and-edge-cases)

---

## Why this exists

### The problem

Current software is optimized for human programmers — unrestricted code access, arbitrary coupling, hidden dependencies, giant context requirements. When you replace a human programmer with an AI agent, you lose all the social coordination technologies that made those structures work (shared mental models, institutional memory, code reviews, hallway conversations) while inheriting every structural assumption those practices produced.

The result: agents must constantly perform long-horizon reasoning — tracing imports across module boundaries, building mental models of the entire codebase, tracking down hidden dependencies. A 2-million-line codebase requires the agent to hold an impossibly large context.

### The inversion

> What if we redesign software itself to fit the cognitive constraints of agents?

A large codebase might contain 2M LOC, 10K files, 300 services, 1000 dependencies. A human developer never actually understands this entire system — they rely on abstractions, ownership boundaries, contracts, architecture diagrams. RICH makes those boundaries **physically enforceable**: an agent working on module `auth` cannot read the source of `token_store`, even if it tries. The filesystem itself enforces the boundary.

### What RICH doesn't do

- It does not make agents smarter
- It does not introduce a new language or compiler
- It does not verify that implementations satisfy their contracts
- It does not generate wiring glue or deployment topology

Those are v2 (contract language) and v3 (compiler). RICH proves that structure alone — enforced at runtime, with existing tooling — already changes what agents can reason about.

---

## Installation

```bash
pip install pyyaml

# Copy the files into your project
# That's it — Python 3.10+, stdlib + PyYAML only
```

Requirements: Python 3.10+, PyYAML. No other dependencies.

---

## The three constraints in detail

### 1. Information Firewall

When an agent works on module `auth`, it has access to exactly:

```
modules/auth/
├── contract.yaml        ← auth's own contract
├── src/auth.py          ← auth's own source (editable)
└── tests/test_auth.py   ← auth's own tests (editable)

modules/token_store/
└── contract.yaml        ← token_store's CONTRACT ONLY

modules/user_repo/
└── contract.yaml        ← user_repo's CONTRACT ONLY
```

What the agent **cannot** access:
- `modules/token_store/src/token_store.py` — dependency implementation
- `modules/user_repo/src/user_repo.py` — sibling implementation
- Any file outside these boundaries

This is not a convention. It is not a code review norm. The harness intercepts every `read_file` call and blocks operations outside the whitelist. The dependency source might as well not exist.

**Why this matters:** An agent's reasoning cost is O(module complexity), not O(codebase complexity). The agent can be maximally capable within its boundary without needing to be capable across the full system.

### 2. Dependency Injection (D4)

Modules code against their dependency's *interface* (defined in `contract.yaml`) and receive dependency handles as **injected arguments** — never via `import`.

```python
# ❌ BLOCKED by the harness
import token_store
from user_repo import UserRepo

# ✅ ALLOWED — dependencies arrive by injection
def authenticate(username, password, *, user_repo, token_store):
    result = user_repo.verify_password(username, password)
    return token_store.issue(username)
```

The harness scans every `write_file` for import statements. If it finds `import <any_module_name>` or `from <any_module_name> import ...`, the write is blocked — **even if the module is a declared dependency**. This is stricter than the static CLI check (which only flags undeclared deps).

**Why this matters:** If a dependency changes its implementation, the agent's code doesn't break — it only depends on the interface. Testing becomes trivial: pass fakes that satisfy the contract, never the real implementation.

### 3. Complexity Budget

Each module has a hard budget:

| Metric | Default | Purpose |
|--------|---------|---------|
| `max_loc` | 5000 | Non-blank source lines |
| `max_files` | 100 | Source files |
| `max_context_tokens` | 100000 | Estimated tokens (chars/4) |

The harness tracks all writes in a session. If an operation would exceed the budget, it's blocked with `BudgetWarning`. Budgets are set per-workspace (in `agentnative.yaml`) and can be overridden per-module (in `contract.yaml`).

**Why this matters:** The budget is calibrated to the target agent's context window. As models improve, budgets expand. As you use cheaper models, budgets tighten. The architecture is explicitly parameterized by agent capability.

---

## Quickstart: using the harness

### Create a session

```python
from harness import Harness, FirewallBlocked, BudgetWarning

h = Harness(".")
session = h.session("auth")  # agent is now working on 'auth'
```

### Read files (allowed)

```python
# ✅ Own source
src = session.read_file("modules/auth/src/auth.py")
# → "def authenticate(username, password, *, user_repo, token_store): ..."

# ✅ Own contract
contract = session.read_file("modules/auth/contract.yaml")
# → "name: auth\ninterface:\n  operations:\n    - name: authenticate\n..."

# ✅ Dependency contract (interface only)
dep_contract = session.read_file("modules/token_store/contract.yaml")
# → "name: token_store\ninterface:\n  operations:\n    - name: issue\n..."
```

### Read files (blocked)

```python
# ❌ Dependency SOURCE
session.read_file("modules/token_store/src/token_store.py")
# → FirewallBlocked: 'modules/token_store/src/token_store.py' is outside
#    module 'auth' boundary. You may only read files within your module
#    and dependency contracts.

# ❌ Sibling module
session.read_file("modules/user_repo/src/user_repo.py")
# → FirewallBlocked

# ❌ Outside workspace
session.read_file("/etc/passwd")
# → FirewallBlocked
```

### Write files (allowed)

```python
# ✅ New file in own source directory
session.write_file("modules/auth/src/helper.py",
    "def format_token(token):\n    return token.upper()\n")
# → written successfully

# ✅ New test file
session.write_file("modules/auth/tests/test_helper.py",
    "def test_format():\n    assert True\n")
# → written successfully

# ✅ Clean imports (stdlib only)
session.write_file("modules/auth/src/utils.py",
    "import hashlib\nimport os\n\ndef hash_it(s):\n    return hashlib.sha256(s).hexdigest()\n")
# → written successfully
```

### Write files (blocked)

```python
# ❌ Import of another module (even a declared dependency)
session.write_file("modules/auth/src/bad.py",
    "import token_store\n\ndef do_thing():\n    pass\n")
# → FirewallBlocked: boundary violation — imports 'token_store'.
#    auth modules receive dependencies by INJECTION, never by import.

# ❌ From-import
session.write_file("modules/auth/src/bad2.py",
    "from user_repo import UserRepo\n\ndef do_thing():\n    pass\n")
# → FirewallBlocked

# ❌ Writing dependency source
session.write_file("modules/token_store/src/sneaky.py", "# backdoor\n")
# → FirewallBlocked

# ❌ Writing outside workspace
session.write_file("/tmp/evil.py", "# nope\n")
# → FirewallBlocked
```

### Search files

```python
# ✅ Search own source
results = session.search_files("def authenticate", "modules/auth/src")
# → ['modules/auth/src/auth.py:8: def authenticate(...']

# ✅ Search own tests
results = session.search_files("test_authenticate", "modules/auth/tests")
# → ['modules/auth/tests/test_auth.py:12: def test_authenticate_success():']

# ✅ Search dependency contracts
results = session.search_files("name:", "modules/token_store")
# → ['modules/token_store/contract.yaml:1: name: token_store']

# ❌ Search dependency source
results = session.search_files("def issue", "modules/token_store/src")
# → FirewallBlocked: 'modules/token_store/src' is outside module boundary
```

### Session information

```python
session.context()
# → Full markdown context document for the agent's system prompt

session.boundary_summary()
# → Module: auth
#   Dependencies (contracts only, no source):
#     - token_store → /path/to/token_store/contract.yaml
#     - user_repo → /path/to/user_repo/contract.yaml
#   Budget: 5000 LOC / 100 files / 100000 tokens
#   You may read:
#     ✓ modules/auth/contract.yaml
#     ✓ modules/auth/src
#     ✓ modules/auth/tests
#     ✓ modules/token_store/contract.yaml
#     ✓ modules/user_repo/contract.yaml
#   You may write:
#     ✎ modules/auth/src
#     ✎ modules/auth/tests

session.budget_status()
# → "Budget: LOC 9/5000  files 3/100  tokens 43/100000"

session.stats_summary()
# → "Session: 10 operations (7 allowed, 3 blocked)"
```

---

## Full test suite walkthrough

The test suite (`test_harness.py`) exercises every constraint on the example `auth` module. Here's the full output with annotations:

```
$ python3 test_harness.py
========================================================================
HARNESS TEST SUITE
========================================================================

Module: auth
Dependencies: ['token_store', 'user_repo']
Budget: 5000 LOC / 100 files / 100000 tokens

─── FIREWALL: Read Enforcement ───
  ✅ allow read: own source file           # session.read_file("modules/auth/src/auth.py")
  ✅ allow read: own contract              # session.read_file("modules/auth/contract.yaml")
  ✅ allow read: own test file             # session.read_file("modules/auth/tests/test_auth.py")
  ✅ allow read: dependency contract        # session.read_file("modules/token_store/contract.yaml")
  ✅ allow read: other dependency contract  # session.read_file("modules/user_repo/contract.yaml")
  ✅ block read: dependency SOURCE          # session.read_file("modules/token_store/src/...") BLOCKED
  ✅ block read: sibling module source      # session.read_file("modules/user_repo/src/...") BLOCKED
  ✅ block read: outside workspace entirely # session.read_file("/etc/passwd") BLOCKED

─── FIREWALL: Write Enforcement ───
  ✅ allow write: new file in own src/     # write to modules/auth/src/ — within boundary
  ✅ allow write: new file in own tests/   # write to modules/auth/tests/ — within boundary
  ✅ block write: dependency source         # write to modules/token_store/src/ — BLOCKED
  ✅ block write: outside workspace         # write to /tmp/evil.py — BLOCKED

─── IMPORT BOUNDARY: Dependency Injection Enforcement ───
  ✅ block write: import undeclared dependency   # "import token_store" — BLOCKED
  ✅ block write: from-import undeclared dependency # "from user_repo import UserRepo" — BLOCKED
  ✅ allow write: code without illegal imports    # "import hashlib\nimport os" — ALLOWED

─── SEARCH: Scoped to Module Boundary ───
  ✅ allow search: own module source       # search "def authenticate" in modules/auth/src
  ✅ allow search: own tests               # search in modules/auth/tests
  ✅ allow search: dependency contracts    # search "name:" in modules/token_store
  ✅ block search: dependency source directory  # search "def issue" in token_store/src — BLOCKED

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

Each `✅` is a test that passed — either an allowed operation succeeded, or a blocked operation was correctly blocked. The `block` tests use `should_pass=False`, meaning they pass when `FirewallBlocked` is raised.

---

## CLI reference

The `rich.py` CLI provides static analysis and scaffolding:

### `rich init`

Scaffolds a new workspace:

```bash
$ rich init
Workspace scaffolded at /home/user/project
  agentnative.yaml  — workspace config
  modules/          — module directory
  .gitignore        — ignores .agentctl/

Next: create a module with `rich wrap <file> --name <name>`
```

Creates `agentnative.yaml` (budget defaults), `modules/` directory, and `.gitignore`.

### `rich validate`

Runs all checks — schema, DAG, budgets, boundary scanning:

```bash
$ rich validate
Workspace: /home/zaphod/dev/rich
Modules: 3
  ✓ auth  ops=1  deps=[user_repo, token_store]  behavior=2
  ✓ token_store  ops=2  deps=[none]  behavior=2
  ✓ user_repo  ops=1  deps=[none]  behavior=1
All checks passed.
```

On failure, prints specific errors and exits non-zero:

```bash
$ rich validate
  ✗ [auth] dependency 'nonexistent' does not reference an existing module
  ✗ [DAG] cycle detected: module_a → module_b → module_a
  ✗ [btest] LOC budget exceeded: 7 > 3

3 error(s) found.
```

Checks performed:
1. **Schema validation** — contract structure, type vocabulary, operation uniqueness, behavior property ids, dependency references
2. **DAG + cycle detection** — DFS with 3-coloring, prints cycle paths
3. **Budget checks** — non-blank LOC, file count, estimated tokens vs. limits
4. **Boundary heuristic** — scans source files for imports of sibling modules not in declared dependencies

### `rich context <module> [--out DIR] [--list]`

Materializes a firewall tree for a module — copies the module's own source + tests + contract, plus each direct dependency's `contract.yaml` only:

```bash
$ rich context auth
Materialized context for 'auth' → .agentctl/workspaces/auth

  Tree structure:
  auth/
    CONTEXT.md
    contract.yaml
    deps/
      token_store/
        contract.yaml
      user_repo/
        contract.yaml
    src/
      auth.py
    tests/
      test_auth.py
```

`--list` does a dry-run: shows what would be included without writing. `--out` specifies a custom output directory.

This is the mechanism for Option 2 deployment: chroot the agent into the materialized tree for physical filesystem enforcement.

### `rich graph [--dot]`

Prints the dependency DAG:

```bash
$ rich graph
Dependency DAG:
  [token_store]  ←  deps: none
  [user_repo]    ←  deps: none
  [auth]         ←  deps: user_repo, token_store
```

```bash
$ rich graph --dot
digraph rich {
  rankdir=LR;
  node [shape=box, style=rounded];
  "user_repo" -> "auth";
  "token_store" -> "auth";
  "token_store";
  "user_repo";
}
```

### `rich wrap <path> --name <name>`

Scaffolds a module around existing code. Copies source into `modules/<name>/src/`, generates a best-effort `contract.yaml` by inferring function signatures from Python files:

```bash
$ cat sample.py
def hello(name: str) -> str:
    return f"Hello, {name}"

def count_things(items: list) -> int:
    return len(items)

$ rich wrap sample.py --name greeter
Wrapped 'sample.py' → module 'greeter'
  Created: modules/greeter/
    src/   — source files copied here
    tests/ — empty (add tests here)
    contract.yaml — skeleton (REVIEW AND FILL IN)
  Inferred 2 operation(s) from function signatures
```

The generated contract passes `rich validate` and is ready after a human reviews and fills in behavioral properties and actual dependencies.

---

## API reference

### `harness.Harness`

Factory for `ModuleSession` instances.

```python
class Harness:
    def __init__(self, workspace_root: str = "."):
        """Load workspace from agentnative.yaml. Validates on load."""

    def session(self, module_name: str) -> ModuleSession:
        """Create a harness session for an agent working on the given module."""

    def module_names(self) -> list[str]:
        """List available module names in the workspace."""

    def validate(self) -> bool:
        """Run static validation (schema, DAG, budgets, boundaries)."""
```

### `harness.ModuleSession`

The core of the harness — mediates every agent tool call.

```python
class ModuleSession:
    def __init__(self, workspace_root: str, module_name: str):
        """Creates session for one agent working on one module.
        Builds read/write whitelists from contract declarations.
        Raises FirewallBlocked if module not found."""

    # Tool mediation
    def read_file(self, path: str) -> str:
        """Read a file within the module boundary.
        Raises FirewallBlocked if path is outside whitelist."""

    def write_file(self, path: str, content: str) -> None:
        """Write to a file within the module boundary.
        Raises FirewallBlocked if path outside whitelist or content has illegal imports.
        Raises BudgetWarning if operation exceeds budget."""

    def search_files(self, pattern: str, path: Optional[str] = None,
                     target: str = "content") -> list[str]:
        """Search within the module boundary.
        Raises FirewallBlocked if search path is outside whitelist."""

    def terminal(self, command: str) -> tuple[int, str]:
        """Heuristic terminal mediation. Scans command for out-of-bounds paths.
        Caller is responsible for actual execution."""

    # Information
    def context(self) -> str:
        """Full context markdown for the agent's system prompt."""

    def boundary_summary(self) -> str:
        """Human-readable summary of what the agent may/may not access."""

    def budget_status(self) -> str:
        """Current budget consumption vs limits."""

    def stats_summary(self) -> str:
        """Session operations: allowed vs blocked count."""

    # Properties
    module: Module              # The module the agent is working on
    deps: list[Module]          # Direct dependency modules
    budget: Budget              # Effective budget for this module
    graph: dict[str, list[str]] # Dependency adjacency list
    whitelist_read: set[str]    # Absolute paths agent may read
    whitelist_write: set[str]   # Absolute paths agent may write to
    stats: SessionStats         # Budget tracking
```

### Exceptions

```python
class FirewallBlocked(Exception):
    """Raised when agent attempts operation outside module boundary."""
    operation: str    # e.g. "read_file", "write_file"
    detail: str       # Human-readable explanation

class BudgetWarning(Exception):
    """Raised when agent approaches or exceeds complexity budget."""
    metric: str       # "LOC", "files", or "tokens"
    used: int         # Current usage
    limit: int        # Budget limit
```

### `rich.py` data models

These are the dataclasses shared between the CLI and the harness:

```python
@dataclass
class Budget:
    max_loc: int = 5000
    max_files: int = 100
    max_context_tokens: int = 100000

@dataclass
class Operation:
    name: str
    inputs: dict[str, str]      # param_name → v1_type
    outputs: dict[str, str]     # param_name → v1_type
    errors: list[str]           # error names

@dataclass
class BehaviorProperty:
    id: str                     # Stable id for progressive formalization
    prose: str = ""             # Human-readable description
    formal: Optional[str] = None  # null in v1, filled by v2

@dataclass
class Interface:
    operations: list[Operation]

@dataclass
class Contract:
    name: str                   # Must match directory name
    version: str = "0.1.0"
    interface: Interface
    dependencies: list[str]     # Direct deps by name
    behavior: list[BehaviorProperty]
    budget: Optional[Budget]    # Per-module override

@dataclass
class Module:
    name: str
    path: str                   # Filesystem path
    contract: Contract
    contract_path: str          # Path to contract.yaml

@dataclass
class Workspace:
    root: str                   # Absolute path to workspace root
    config: dict                # Raw agentnative.yaml contents
    modules: list[Module]
```

### Key functions in `rich.py`

```python
def load_workspace(root: str = ".") -> Workspace:
    """Load agentnative.yaml, discover modules, parse all contracts."""

def parse_module(mod_dir: str, contract_path: str) -> Module:
    """Parse a single module directory + its contract.yaml."""

def validate_workspace(ws: Workspace) -> list[str]:
    """Schema validation: names, types, ids, dependency references."""

def build_dep_graph(modules: list[Module]) -> dict[str, list[str]]:
    """Build adjacency list from module dependencies."""

def detect_cycles(modules: list[Module]) -> list[list[str]]:
    """DFS with 3-coloring. Returns list of cycle paths."""

def get_effective_budget(mod: Module, ws: Workspace) -> Budget:
    """Module budget override falling back to workspace defaults."""

def check_module_budget(mod: Module, ws: Workspace) -> list[str]:
    """Count LOC, files, tokens in module src/. Returns violation messages."""

def check_module_boundaries(mod: Module, ws: Workspace) -> list[str]:
    """Scan source files for imports of sibling modules not in deps."""

def estimate_tokens(text: str) -> int:
    """Approximate token count: ceil(len(text)/4). Replace with real tokenizer later."""
```

---

## Contract format specification

Every module has a `contract.yaml` at its root. The contract is the **interface** a module exposes to its dependents — it is the single artifact that crosses the firewall.

### Complete schema

```yaml
# Required: module identity
name: <string>              # Must match directory name. Unique across workspace.
version: <string>           # Semantic version

# Machine-readable interface (enforced by the harness today)
interface:
  operations:               # List of operations this module exposes
    - name: <string>        # Unique within this module
      inputs:               # Dict of param_name → v1_type
        <param>: <type>
      outputs:              # Dict of param_name → v1_type
        <param>: <type>
      errors:               # List of error names this operation can raise
        - <error_name>

# Direct dependencies — defines the firewall boundary
dependencies:               # List of module name strings
  - <module_name>           # Must reference an existing module

# Behavioral properties (progressively formalizable)
behavior:                   # List of properties this module guarantees
  - id: <string>            # STABLE id — v2 fills formal: in place
    prose: <string>         # Human-readable description
    formal: null            # null in v1, formal spec in v2

# Optional per-module budget override
budget:                     # Falls back to workspace defaults when omitted
  max_loc: <int>
  max_files: <int>
  max_context_tokens: <int>
```

### Type vocabulary (v1)

| Type | Description |
|------|-------------|
| `string` | UTF-8 text |
| `int` | Integer |
| `float` | Floating point |
| `bool` | Boolean |
| `list<string>` | List of strings |
| `list<int>` | List of ints |
| `list<float>` | List of floats |
| `list<bool>` | List of bools |

Richer types (nested lists, dicts, unions, optionals) are v2. The current vocabulary is intentionally minimal.

### Validation rules

The harness and CLI enforce:

1. `name` must match the directory name exactly
2. `name` must not be empty
3. `version` must be present
4. All operation `inputs` and `outputs` types must be in the v1 vocabulary
5. Operation names must be unique within a module
6. All `dependencies` must reference existing modules
7. Behavior property `id` fields must be present and unique within a module
8. Module `budget` values override workspace defaults; omitted values inherit from workspace

### The v1→v2 seam

The contract format is designed so v2 is an **extension, not a replacement**. Two design choices make this possible:

1. **Stable `id` fields** — every behavior property has a unique, stable `id`. v2 formalizes properties one at a time by filling in `formal:` without changing the `id` or `prose`.

2. **Hard machine/prose split** — the `interface` section is fully machine-readable and enforceable today. The `behavior` section is prose now, formal later. They share a schema but have different enforcement timelines.

The one failure mode to avoid: a behavior section that cannot be formalized without a rewrite. The `id` + `formal: null` pattern is the mitigation.

---

## Architecture and design decisions

### Two layers

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

rich.py             ← ADMIN CLI: static validation, scaffolding, DAG visualization
    ├── init              — scaffold new workspace
    ├── validate          — schema, DAG, budgets, boundaries (one-shot)
    ├── context           — materialize workspace tree for a module
    ├── graph             — print dependency DAG (text or DOT)
    └── wrap              — scaffold module around existing code
```

**Key difference:** `rich.py` validates at a point in time. `harness.py` enforces on every operation.

### Why not a single file?

The original spec (D5) called for a single `agentctl.py`. That was for the CLI. The harness is a separate concern — it wraps agent tool calls, not human CLI invocations. The library functions are shared; the consumers are different.

### Dependency injection vs imports (the D4 gap)

The static boundary check in `rich.py` (`check_module_boundaries`) flags imports of modules **not** in declared dependencies. The harness (`_scan_imports`) blocks imports of **any** module, including declared dependencies. This asymmetry is intentional:

- **Static check:** catches accidental dependencies. Useful for pre-flight validation.
- **Runtime enforcement:** enforces D4 fully. Even declared deps must arrive by injection. The import is the violation, regardless of what the contract says.

### Token estimation

`estimate_tokens(text) = ceil(len(text) / 4)`. A single function so a real tokenizer (tiktoken, etc.) can replace it later without changing the budget infrastructure.

### Enforcement mechanisms

| Level | Mechanism | Strength |
|-------|-----------|----------|
| `harness.py` | Tool call mediation | In-process — blocks every operation |
| `rich context` + `chroot` | Filesystem isolation | Physical — files don't exist in the tree |
| `rich validate` | Static analysis | One-shot — catches violations at a point in time |

For production use, combine the harness (for in-process enforcement) with `chroot`/`bwrap` (for filesystem isolation). The harness catches import violations and tracks budgets; the chroot prevents escape through raw filesystem access.

---

## Example project: auth system

The repo includes a 3-module example that demonstrates every constraint:

### Module graph

```
token_store (no deps) ────────────┐
                                  ├──→ auth
user_repo (no deps) ──────────────┘
```

### `token_store`

In-memory token store with 24-hour expiry. Two operations: `issue(subject) → {token}` and `validate(token) → {subject}`. Errors: `invalid_token`, `expired_token`. No dependencies.

### `user_repo`

Password verification with constant-time comparison over stored hashes. One operation: `verify_password(username, password) → {ok: bool}`. Error: `unknown_user`. No dependencies.

### `auth`

Authentication service that depends on both `token_store` and `user_repo`. One operation: `authenticate(username, password) → {token}`. Error: `invalid_credentials`.

**Key design:** `auth` receives its dependencies by injection — the function signature is `authenticate(username, password, *, user_repo, token_store)`. It never imports `user_repo` or `token_store`. Its tests pass **fakes** that satisfy the dependency contracts — never the real implementations.

### Running the example

```bash
# Run all module tests
PYTHONPATH=modules/token_store/src python3 -m pytest modules/token_store/tests/ -v
PYTHONPATH=modules/user_repo/src python3 -m pytest modules/user_repo/tests/ -v
PYTHONPATH=modules/auth/src python3 -m pytest modules/auth/tests/ -v

# Run the harness test suite
python3 test_harness.py

# Static validation
python3 rich.py validate
```

---

## The 3-phase program

RICH v1 is part of a larger program. v1 has **standalone value** even if later phases never ship.

| Phase | Scope | Status |
|-------|-------|--------|
| **v1 — Harness** | Filesystem-level firewall, DAG, budgets. This repo. | ✅ Done |
| v2 — Language | Progressively formalizable contract/composition language | Future |
| v3 — Compiler | Contract verification + deployment compilation | Future |

The v1→v2 seam is the contract format. The v2→v3 seam is the formality of the composition language. Each phase builds on the previous one; none requires the next.

---

## Development guide

### Running everything

```bash
# Full test suite
python3 test_harness.py

# Module unit tests
PYTHONPATH=modules/token_store/src python3 -m pytest modules/token_store/tests/ -v
PYTHONPATH=modules/user_repo/src python3 -m pytest modules/user_repo/tests/ -v
PYTHONPATH=modules/auth/src python3 -m pytest modules/auth/tests/ -v

# Static validation
python3 rich.py validate

# Visualize the DAG
python3 rich.py graph
python3 rich.py graph --dot | dot -Tpng -o dag.png

# Materialize a module context
python3 rich.py context auth --list
python3 rich.py context auth
```

### Project structure

```
.
├── harness.py          ← RUNTIME: ModuleSession mediates every agent tool call
├── rich.py             ← ADMIN CLI: static validate, context, graph, wrap, init
├── test_harness.py     ← 23/23 test suite proving every constraint
├── agentnative.yaml    ← Workspace config (module_root, budgets)
├── .gitignore
├── README.md
├── SKILL.md            ← Hermes Agent skill (loaded automatically)
├── modules/
│   ├── auth/
│   │   ├── contract.yaml
│   │   ├── src/auth.py
│   │   └── tests/test_auth.py
│   ├── token_store/
│   │   ├── contract.yaml
│   │   ├── src/token_store.py
│   │   └── tests/test_token_store.py
│   └── user_repo/
│       ├── contract.yaml
│       ├── src/user_repo.py
│       └── tests/test_user_repo.py
└── .agentctl/          ← Generated workspace trees (gitignored)
```

### Adding a new module

```bash
# Option 1: Scaffold from existing code
python3 rich.py wrap existing_module.py --name new_module

# Option 2: Manual
mkdir -p modules/new_module/{src,tests}
# Write modules/new_module/contract.yaml
# Write modules/new_module/src/new_module.py
# Write modules/new_module/tests/test_new_module.py

# Validate
python3 rich.py validate
```

### Modifying the harness

When editing `harness.py`:

- `_build_whitelists()` constructs readable and writable paths from module + dep contracts
- `_is_under_whitelist()` checks if an absolute path is under any whitelisted directory
- `_scan_imports()` blocks ALL module imports per D4
- `_IMPORT_RE` catches both `import X` and `from X import Y`
- `FirewallBlocked` and `BudgetWarning` are the only blocking exceptions
- `_generate_context_str()` generates CONTEXT.md without filesystem writes (unlike `_generate_context_md` in rich.py)

### Modifying the CLI

When editing `rich.py`:

- Single file, Python 3.10+, stdlib + PyYAML only
- `load_workspace` discovers modules from `agentnative.yaml`
- `validate_workspace` runs schema checks in one pass
- `detect_cycles` uses DFS with 3-coloring (WHITE/GREY/BLACK)
- `check_module_budget` counts non-blank LOC across all source files
- `check_module_boundaries` scans for imports of sibling modules
- `estimate_tokens` is a single function — replace with a real tokenizer without changing downstream code

---

## Pitfalls and edge cases

### Harness vs CLI boundary checking

The static check (`rich.py`) only flags imports of modules not in declared dependencies. The harness (`harness.py`) blocks ALL module imports per D4. If you're only running `rich validate` and not using the harness, you'll miss import violations against declared dependencies.

### Terminal mediation is heuristic

`session.terminal()` does basic path scanning in the command string — it's not a sandbox. For strong terminal enforcement, materialize the workspace with `rich context` and chroot into it.

### search_files scoping

`session.search_files()` restricts results to whitelisted directories. If the search path is outside the whitelist, `FirewallBlocked` is raised (not an empty result set). This is intentional: an empty result could mean "no matches" or "outside boundary" — the ambiguity is worse than a hard block.

### Empty src/ and tests/ directories

The harness whitelists `modules/<name>/src/` and `modules/<name>/tests/` even when empty. If these directories don't exist, `write_file` creates them. But `read_file` on files within them won't find anything — the whitelist is about the *boundary*, not the *contents*.

### Budget is session-scoped

Budget tracking (`SessionStats`) is per-session, not cumulative across sessions. If an agent exceeds the budget in one session, a new session starts fresh. For cumulative enforcement, track externally.

### Unicode and binary files

`check_module_budget` and `check_module_boundaries` skip files that can't be read as UTF-8. Binary files are neither counted against budgets nor scanned for imports. This is a known gap — a module could theoretically hide code in binary files, but the harness's `read_file` would expose it (the file would be unreadable).

### Transitive dependencies are invisible

D2 says the information horizon is direct-only. An agent working on `auth` sees `token_store`'s contract but not `token_store`'s dependencies. If `token_store` depends on `crypto`, `auth`'s agent doesn't know `crypto` exists. This is by design — transitive concerns are encapsulated behind the direct dependency's contract.

---

## Phase 2 — Formal Contract Language

v2 makes the `formal: null` placeholder real. It adds a small predicate language with a runtime checker that turns every contract into a blame-assigning oracle, and a type checker that validates expressions against declared I/O types.

### Property kinds

| Kind | Checked | Example |
|------|---------|---------|
| `postcondition` | After each successful call | `len(result.token) > 0` |
| `raises` | Before call (guard) + after (error match) | `not deps.user_repo.verify_password(...).ok → invalid_credentials` |
| `trace_invariant` | After each call, against history | `∀ i≠j: token_i ≠ token_j` |
| `temporal` | Deferred to v2.4 (cross-call properties) | `G(issue(t) → within(86400, validate(t)))` |
| `nonfunctional` | Declared out-of-scope | Constant-time comparison, latency, etc. |

### The four example properties

```yaml
# token_on_success — postcondition: token must be non-empty
- id: token_on_success
  formal:
    kind: postcondition
    expr: "len(result.token) > 0"

# reject_invalid — raises: bad credentials must trigger the error
- id: reject_invalid
  formal:
    kind: raises
    when: "not deps.user_repo.verify_password(username, password).ok"
    error: "invalid_credentials"

# token_uniqueness — trace invariant: tokens are unique
- id: token_uniqueness
  formal:
    kind: trace_invariant
    expr: "true"

# constant_time_compare — nonfunctional: declared, not checked
- id: constant_time_compare
  formal:
    kind: nonfunctional
```

### Runtime checker

```python
from runtime_checker import contract_checked, ContractViolation
from properties import PostconditionProperty, RaisesProperty

# Wrap a function with contract checking
checked_fn = contract_checked(
    authenticate,
    postconditions=[PostconditionProperty(id="token_on_success",
                    expr="len(result.token) > 0")],
    raises_props=[RaisesProperty(id="reject_invalid",
                   when="not deps.user_repo.verify_password(username, password).ok",
                   error="invalid_credentials")],
)

# Correct password → passes postcondition
result = checked_fn(username="admin", password="secret",
                    user_repo=UserRepo(), token_store=TokenStore())

# Wrong password → raises property triggers, checks error matches
checked_fn(username="admin", password="wrong", ...)
# → AuthError("invalid_credentials") — satisfies the raises property
```

### Dependency proxy with blame

```python
from runtime_checker import DependencyProxy

# Wrap a dependency handle — checks contracts at the injection boundary
proxy = DependencyProxy(
    dep=RealTokenStore(),
    module_name="token_store",
    op_name="issue",
    postconditions=[PostconditionProperty(id="returns_token",
                     expr="len(result.token) > 0")],
)

proxy(subject="alice")
# If token_store returns empty token → ContractViolation blaming "token_store"
# If caller passes wrong types → ContractViolation blaming "caller"
```

### Expression language

A small, total, side-effect-free predicate language with two planned backends:

```python
from expr_lang import parse_expr

# Comparisons and boolean logic
parse_expr("len(result.token) > 0")
parse_expr("not deps.user_repo.verify_password(username, password).ok")

# Arithmetic, precedence, parentheses
parse_expr("(x + y) * 2 > threshold")

# Dep references (uninterpreted function calls for assume-guarantee)
parse_expr("deps.token_store.issue(subject).token")
```

The same AST supports runtime evaluation today and SMT compilation in v2.3. The type checker validates every expression against declared operation I/O types and dependency contracts.

### v2 test suite

```
test_properties.py        — 15 tests: kind classifier, parse/validate formal properties
test_expr_lang.py         — 40 tests: parser, AST, type checker, precedence
test_runtime_checker.py   — 28 tests: evaluator, contract checker, dependency proxy
test_v2_integration.py    — 18 tests: all 4 properties against real modules, blame
```

All 101 v2 tests pass alongside the 23 harness tests and 11 module tests — 135 total.

### Architecture (v2 files)

```
properties.py           ← v2.0: PropertyKind enum, FormalProperty subclasses, parser
expr_lang.py            ← v2.1: Tokenizer, recursive-descent parser, AST, type checker
runtime_checker.py      ← v2.2: Expression evaluator, ContractChecker, DependencyProxy
```

---

> *"The information firewall must be enforced by construction, not by convention."*
