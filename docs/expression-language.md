# Expression Language Reference

A small, total, side-effect-free predicate language for formal contract properties. The same AST supports runtime evaluation today and SMT compilation in v2.3.

---

## Grammar

```
expr       → or_expr
or_expr    → and_expr ("or" and_expr)*
and_expr   → not_expr ("and" not_expr)*
not_expr   → "not" not_expr | comparison
comparison → term (CMP_OP term)?
term       → factor (ADD_OP factor)*
factor     → unary (MUL_OP unary)*
unary      → "-" unary | primary

primary    → "true" | "false"
           | INT_LIT | FLOAT_LIT | STRING_LIT
           | "(" expr ")"
           | "result" ("." IDENT)?
           | "len" "(" expr ")"
           | "deps." IDENT "." IDENT ("(" args ")")? ("." IDENT)?
           | IDENT

CMP_OP     → "==" | "!=" | "<" | ">" | "<=" | ">="
ADD_OP     → "+" | "-"  
MUL_OP     → "*" | "/"
```

**Precedence** (lowest to highest): `or` < `and` < `not` < comparison < `+` `-` < `*` `/` < unary `-` < primary

---

## Literals

| Type | Syntax | Example |
|------|--------|---------|
| Boolean | `true`, `false` | `true` |
| Integer | Decimal digits | `42`, `-7`, `0` |
| Float | Decimal with `.` | `3.14`, `-0.5` |
| String | Double-quoted, escaped | `"hello"`, `"abc\"def"` |

String escape sequences: `\"`, `\\`.

---

## Operators

### Comparison

| Op | Types | Example |
|----|-------|---------|
| `==` | any (same types) | `x == 5`, `name == "alice"` |
| `!=` | any (same types) | `x != 0` |
| `<` | int, float, string | `len(s) < 10` |
| `>` | int, float, string | `count > 0` |
| `<=` | int, float, string | `x <= 100` |
| `>=` | int, float, string | `x >= 0` |

Mixed int/float comparison is allowed (`3 < 3.5` → `true`).

### Boolean

| Op | Types | Example |
|----|-------|---------|
| `and` | bool, bool | `x > 0 and y < 10` |
| `or` | bool, bool | `a or b` |
| `not` | bool | `not ok` |

Short-circuit evaluation: `false and expr` doesn't evaluate `expr`.

### Arithmetic

| Op | Types | Result | Example |
|----|-------|--------|---------|
| `+` | int/float, int/float | int if both int, float otherwise | `x + 1` |
| `-` | int/float, int/float | int if both int, float otherwise | `total - fee` |
| `*` | int/float, int/float | int if both int, float otherwise | `price * qty` |
| `/` | int/float, int/float | always float | `count / 2` |
| `-x` | int, float | same as operand | `-delta` |

---

## Special Forms

### `result` — Return Value Access

```
result            → whole return dict
result.field      → value of field in return dict
```

Only valid in `kind: postcondition` context. The field must be a declared output of the operation.

```yaml
# Operation outputs: {token: string}
expr: "len(result.token) > 0"
expr: "result.token != ''"
```

### `deps.<module>.<op>(args...).field` — Dependency Call

```
deps.IDENT.IDENT "(" args ")" "." IDENT
deps.IDENT.IDENT "(" args ")"             → whole return dict
deps.IDENT.IDENT "." IDENT                → field access (sugar for no-arg call)
```

At **runtime**, this actually calls the dependency handle. At **static check time** (v2.3), this is an uninterpreted function constrained by the dependency's postconditions — assume-guarantee reasoning.

```yaml
# auth's reject_invalid property:
when: "not deps.user_repo.verify_password(username, password).ok"

# Evaluates to: not user_repo.verify_password(username="alice", password="wrong")["ok"]
```

Arguments are positional, matched to the dependency operation's declared parameter order. Argument expressions are evaluated in the current context (resolving input variables).

### `len(expr)` — Length

```
len "(" expr ")"
```

Returns the length of a string or list. Result type is `int`.

```yaml
expr: "len(result.token) > 0"
expr: "len(username) >= 3"
```

### Variables

```
IDENT → [a-zA-Z_][a-zA-Z0-9_]*
```

Variables resolve to declared operation inputs. Unknown variables raise `ExprTypeError` at type-check time and `ContractViolation` at runtime.

```yaml
# Operation inputs: {username: string, password: string}
expr: "username == 'admin'"
expr: "len(password) >= 8"
```

---

## Type System

### Type Rules

| Expression | Required operand types | Result type |
|-----------|----------------------|-------------|
| `true` / `false` | — | `bool` |
| `42` | — | `int` |
| `3.14` | — | `float` |
| `"hello"` | — | `string` |
| `x` (variable) | — | declared input type |
| `result.field` | field in outputs | declared output type |
| `deps.X.Y(...).field` | field in Y's outputs | declared output type |
| `not e` | `e: bool` | `bool` |
| `e1 and e2` | `e1, e2: bool` | `bool` |
| `e1 or e2` | `e1, e2: bool` | `bool` |
| `e1 == e2`, `e1 != e2` | `typeof(e1) == typeof(e2)` | `bool` |
| `e1 < e2`, etc. | `int/float/string` (compatible) | `bool` |
| `e1 + e2`, `e1 - e2` | `int/float` | `int` (both int) or `float` |
| `e1 * e2` | `int/float` | `int` (both int) or `float` |
| `e1 / e2` | `int/float` | `float` |
| `-e` | `int/float` | same as `e` |
| `len(e)` | `string` or `list<string>` | `int` |

### Dep Call Type Checking

When type-checking `deps.token_store.issue(subject).token`:

1. `token_store` must be a declared dependency of the module
2. `issue` must be a declared operation on `token_store`
3. Argument count must match `issue`'s declared input count
4. Each argument type must match the corresponding declared input type
5. `token` must be a declared output field of `issue`

This is checked by `TypeChecker.check()` before any expression is evaluated.

---

## Examples From Real Contracts

### Postcondition: `token_on_success`

```yaml
# auth.authenticate → {token: string}
expr: "len(result.token) > 0"
```

AST: `BinaryOp(">", FuncCall("len", ResultAccess("token")), Literal(0))`

Evaluated after `authenticate` returns. If `result.token` is empty → `ContractViolation`.

### Raises Guard: `reject_invalid`

```yaml
# auth.authenticate(username, password) → raises invalid_credentials when user_repo rejects
when: "not deps.user_repo.verify_password(username, password).ok"
error: "invalid_credentials"
```

AST: `UnaryOp("not", DepCall("user_repo", "verify_password", [Variable("username"), Variable("password")], "ok"))`

Evaluated **before** `authenticate` is called. If `true`, the call must raise an error containing `"invalid_credentials"`.

Note: `deps.user_repo.verify_password(username, password).ok` actually calls the real `user_repo` handle at runtime. At static check time (v2.3), `verify_password` is an uninterpreted function constrained by `user_repo`'s own postconditions — this is assume-guarantee reasoning.

---

## Error Handling

### Parse Errors (`ExprParseError`)

```python
parse_expr('"unclosed')          # → ExprParseError: unclosed string
parse_expr("x @ y")              # → ExprParseError: unexpected token '@'
parse_expr("deps.x")             # → ExprParseError: expected '.' after deps
```

### Type Errors (`ExprTypeError`)

```python
checker.check(parse_expr("nonexistent"))     # → ExprTypeError: unknown input variable
checker.check(parse_expr("result.bad"))      # → ExprTypeError: unknown result field
checker.check(parse_expr('"a" + "b"'))       # → ExprTypeError: arithmetic requires numeric
checker.check(parse_expr("len(42)"))         # → ExprTypeError: len requires string or list
```

### Runtime Errors (`ContractViolation`)

```python
evaluate(parse_expr("result.token"), ctx)     # → ContractViolation if result is None
evaluate(parse_expr("deps.foo.bar()"), ctx)   # → ContractViolation if dep not in context
```

---

## Two Backends, One AST

The expression language is deliberately designed with a single AST that supports two interpretation paths:

| Backend | Status | What it does |
|---------|--------|-------------|
| Runtime evaluator (`evaluate()`) | ✅ v2.2 | Walks AST against `EvalContext`, returns Python values |
| SMT compiler | v2.3 | Compiles decidable-core expressions to SMT-LIB for Z3 verification |

The **shared-AST constraint** is the v2 analog of v1's "materialize, don't print": the runtime evaluator and the SMT compiler are two interpreters of one grammar. A runtime feature is never added without either adding its SMT translation or explicitly marking expressions that use it as runtime-only.

This prevents the trap where the runtime path outgrows what the static path can compile — exactly the same shape of mistake v1 avoided by making `context` materialize rather than print.
