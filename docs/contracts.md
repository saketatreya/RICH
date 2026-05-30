# Contract Format Reference

Every module has a `contract.yaml` at its root. The contract is the **interface** a module exposes to its dependents — it is the single artifact that crosses the information firewall.

---

## Complete Schema

```yaml
# ── Identity ──
name: <string>              # Required. Must match directory name. Unique across workspace.
version: <string>           # Required. Semantic version.

# ── Machine-Readable Interface (v1 — enforced now) ──
interface:
  operations:               # List of operations this module exposes
    - name: <string>        # Required. Unique within this module.
      inputs:               # Dict of param_name → v1_type
        <param>: <type>
      outputs:              # Dict of param_name → v1_type
        <param>: <type>
      errors:               # List of error names this operation can raise
        - <error_name>

# ── Dependencies (v1 — defines the firewall boundary) ──
dependencies:               # List of module name strings. Direct deps only (D2).
  - <module_name>           # Must reference an existing module.

# ── Behavioral Properties (v1 prose, v2 formal) ──
behavior:                   # List of behavioral guarantees.
  - id: <string>            # Required. STABLE id — v2 fills `formal:` in place.
    prose: <string>         # Required. Human-readable description.
    formal: <formal_spec>   # null in v1, structured object in v2.

# ── Budget Override (v1 — optional) ──
budget:                     # Per-module override. Falls back to workspace defaults.
  max_loc: <int>            # Non-blank source lines (default: 5000)
  max_files: <int>          # Source files (default: 100)
  max_context_tokens: <int> # Estimated tokens, chars/4 (default: 100000)
```

---

## Type Vocabulary (v1)

| Type | Description | Example values |
|------|-------------|---------------|
| `string` | UTF-8 text | `"hello"`, `"abc123"` |
| `int` | Integer | `42`, `-7`, `0` |
| `float` | Floating point | `3.14`, `-0.5`, `2.0` |
| `bool` | Boolean | `true`, `false` |
| `list<string>` | List of strings | `["a", "b"]` |
| `list<int>` | List of ints | `[1, 2, 3]` |
| `list<float>` | List of floats | `[1.1, 2.2]` |
| `list<bool>` | List of bools | `[true, false]` |

Richer types (nested lists, dicts, unions, optionals) are deferred to future phases. The current vocabulary is intentionally minimal — it covers the most common cases without introducing a full type system.

---

## Formal Property Schema (v2)

The `formal` field in each behavior property widens from `null` to a structured object:

```yaml
formal:
  kind: <property_kind>     # Required. One of: postcondition, raises, trace_invariant, temporal, nonfunctional
  # ... kind-specific fields below
```

### `kind: postcondition`

Checked after each successful call. Predicate over inputs and result.

```yaml
formal:
  kind: postcondition
  expr: <expression>        # Required. Evaluated after call returns. Must be true.
```

Example:
```yaml
- id: token_on_success
  prose: "Returns a non-empty token."
  formal:
    kind: postcondition
    expr: "len(result.token) > 0"
```

### `kind: raises`

Guard checked before call, error matched on failure.

```yaml
formal:
  kind: raises
  when: <expression>        # Required. Evaluated BEFORE call. If true, error must be raised.
  error: <error_name>       # Required. Must match the error raised.
```

Example:
```yaml
- id: reject_invalid
  prose: "Bad credentials raise invalid_credentials."
  formal:
    kind: raises
    when: "not deps.user_repo.verify_password(username, password).ok"
    error: "invalid_credentials"
```

### `kind: trace_invariant`

Checked after each call against accumulated call history.

```yaml
formal:
  kind: trace_invariant
  expr: <expression>        # Required. Evaluated against history after each call.
```

Example:
```yaml
- id: token_uniqueness
  prose: "Each call produces a unique token."
  formal:
    kind: trace_invariant
    expr: "true"            # Placeholder — real expression uses trace predicates
```

### `kind: temporal`

Cross-call temporal logic. Deferred to v2.4.

```yaml
formal:
  kind: temporal
  expr: <temporal_formula>  # Required. Temporal logic expression with controllable clock.
```

Example:
```yaml
- id: token_validity_window
  prose: "Tokens validate for 24h from issue."
  formal:
    kind: temporal
    expr: "G(issue(t) → within(86400, validate(t)))"
```

### `kind: nonfunctional`

Declared out-of-scope for contract-based checking.

```yaml
formal:
  kind: nonfunctional       # No additional fields required.
```

Example:
```yaml
- id: constant_time_compare
  prose: "Password comparison is constant-time over stored hash."
  formal:
    kind: nonfunctional
```

---

## Validation Rules (v1 + v2)

### v1 validation (enforced by `rich validate`)

1. `name` must match the directory name exactly
2. `name` must not be empty
3. `version` must be present
4. All operation `inputs` and `outputs` types must be in the v1 vocabulary
5. Operation names must be unique within a module
6. All `dependencies` must reference existing modules
7. Behavior property `id` fields must be present and unique within a module
8. Module `budget` values override workspace defaults; omitted values inherit

### v2 validation (enforced by `parse_formal_property`)

9. `formal` is `null` → backward-compatible, no property parsed
10. `formal` is a dict with `kind` field → parsed as structured property
11. Unknown `kind` → `PropertyParseError`
12. Missing required fields per kind → `PropertyParseError`

### Expression language validation (enforced by `TypeChecker`)

13. Variables must reference declared operation inputs
14. `result.field` must reference declared operation outputs
15. `deps.X.Y(args...).field` — `X` must be a declared dependency, `Y` must be a declared operation, `field` must be a declared output of `Y`
16. Argument types to dep calls must match declared input types
17. `len()` requires `string` or `list<string>`
18. Comparison operators require compatible types
19. Boolean operators require `bool` operands
20. Arithmetic operators require numeric operands

---

## Workspace Config (`agentnative.yaml`)

```yaml
module_root: modules         # Where module directories live (default: "modules")
source_dir: src              # Per-module source subdir name (default: "src")
tests_dir: tests             # Per-module tests subdir name (default: "tests")
budget:
  max_loc: 5000              # Default non-blank source lines per module
  max_files: 100             # Default source files per module
  max_context_tokens: 100000 # Default estimated token budget
```

---

## Example: Complete `auth` Contract (v2)

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
    formal:
      kind: postcondition
      expr: "len(result.token) > 0"

  - id: reject_invalid
    prose: "On invalid credentials, raises invalid_credentials and issues no token."
    formal:
      kind: raises
      when: "not deps.user_repo.verify_password(username, password).ok"
      error: "invalid_credentials"
```

---

## The v1→v2 Seam

The contract format is designed so v2 is an **extension, not a replacement**. Two choices:

1. **Stable `id` fields.** Every behavior property has a unique, stable `id`. v2 formalizes properties by filling in `formal:` without changing the `id` or `prose`.

2. **Hard machine/prose split.** The `interface` section is fully machine-readable and enforceable today. The `behavior` section is prose in v1, structured in v2. They share a schema but have different enforcement timelines.

The one failure mode to avoid: a behavior section that cannot be formalized without a rewrite. The `id` + `formal: null` pattern is the mitigation — v2 fills `formal:` in place, property by property, without changing anything else in the contract.
