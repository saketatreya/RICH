"""v2.1: Expression language — grammar, parser, AST, type checker.

A small, total, side-effect-free predicate language for contract expressions.
The same AST supports both runtime evaluation and (future) SMT compilation.

Grammar:
  expr     → or_expr
  or_expr  → and_expr ("or" and_expr)*
  and_expr → not_expr ("and" not_expr)*
  not_expr → "not" not_expr | comparison
  comparison → term (CMP_OP term)?
  term     → factor (ADD_OP factor)*
  factor   → unary (MUL_OP unary)*
  unary    → "-" unary | primary
  primary  → BOOL_LIT | INT_LIT | FLOAT_LIT | STRING_LIT
           | "(" expr ")"
           | "result" ("." IDENT)?
           | "len" "(" expr ")"
           | "deps." IDENT "." IDENT ("(" args ")")? ("." IDENT)?
           | IDENT ("(" args ")")? ("." IDENT)*
"""

import re
from typing import Optional

V1_TYPES = {"string", "int", "float", "bool", "list<string>", "list<int>",
            "list<float>", "list<bool>"}

# ── AST Nodes ──────────────────────────────────────────────────────────────────

class Expr:
    """Base class for all expression AST nodes."""
    type: Optional[str] = None  # filled in by type checker

class Literal(Expr):
    def __init__(self, value):
        self.value = value
        if isinstance(value, bool):
            self.type = "bool"
        elif isinstance(value, int):
            self.type = "int"
        elif isinstance(value, float):
            self.type = "float"
        elif isinstance(value, str):
            self.type = "string"
    def __repr__(self):
        return f"Literal({self.value!r})"

class Variable(Expr):
    def __init__(self, name: str):
        self.name = name
    def __repr__(self):
        return f"Variable({self.name!r})"

class ResultAccess(Expr):
    def __init__(self, field: Optional[str] = None):
        self.field = field  # None means the whole result dict
    def __repr__(self):
        return f"ResultAccess({self.field!r})"

class DepCall(Expr):
    def __init__(self, module: str, operation: str, args: list[Expr],
                 field: Optional[str] = None):
        self.module = module
        self.operation = operation
        self.args = args
        self.field = field  # None means whole return dict
    def __repr__(self):
        return f"DepCall({self.module}.{self.operation}(...){'.' + self.field if self.field else ''})"

class UnaryOp(Expr):
    def __init__(self, op: str, operand: Expr):
        self.op = op
        self.operand = operand
    def __repr__(self):
        return f"UnaryOp({self.op!r}, {self.operand!r})"

class BinaryOp(Expr):
    def __init__(self, op: str, left: Expr, right: Expr):
        self.op = op
        self.left = left
        self.right = right
    def __repr__(self):
        return f"BinaryOp({self.op!r}, {self.left!r}, {self.right!r})"

class HistoryAccess(Expr):
    def __init__(self, field: Optional[str] = None):
        self.field = field  # None means whole history list
    def __repr__(self):
        return f"HistoryAccess({self.field!r})"


class AggregateCall(Expr):
    def __init__(self, agg: str, expr: Expr):
        self.agg = agg     # "distinct", "count", "all", "any"
        self.expr = expr   # expression to aggregate over history entries
    def __repr__(self):
        return f"AggregateCall({self.agg!r}, {self.expr!r})"


class FuncCall(Expr):
    def __init__(self, func: str, arg: Expr):
        self.func = func
        self.arg = arg
    def __repr__(self):
        return f"FuncCall({self.func!r}, {self.arg!r})"


# ── Lexer ──────────────────────────────────────────────────────────────────────

TOKEN_RE = re.compile(r"""
    \s*(?:
        # Operators and delimiters (longer first)
        (<=|>=|==|!=|[<>]=?)
      | (and|or|not)\b
      | (true|false)\b
      | (result)\b
      | (history)\b
      | (len)\b
      | (deps)\b
      | (distinct|count|all|any)\b
      | ([+\-*/])
      | ([(),.])
      # Literals
      | ("(?:[^"\\]|\\.)*")
      | (\d+\.\d+)
      | (\d+)
      # Identifiers (after keywords)
      | ([a-zA-Z_]\w*)
    )\s*
""", re.VERBOSE)

TOKEN_CMP = 1
TOKEN_LOGIC = 2
TOKEN_BOOL_LIT = 3
TOKEN_RESULT = 4
TOKEN_HISTORY = 5
TOKEN_FUNC = 6
TOKEN_DEPS = 7
TOKEN_AGGREGATE = 8
TOKEN_ADDOP = 9
TOKEN_DELIM = 10
TOKEN_STRING = 11
TOKEN_FLOAT = 12
TOKEN_INT = 13
TOKEN_IDENT = 14


class Token:
    def __init__(self, kind: int, value: str, pos: int):
        self.kind = kind
        self.value = value
        self.pos = pos
    def __repr__(self):
        return f"Token({self.kind}, {self.value!r})"


def tokenize(text: str) -> list[Token]:
    tokens = []
    for m in TOKEN_RE.finditer(text):
        for i in range(1, 15):
            val = m.group(i)
            if val is not None:
                tokens.append(Token(i, val, m.start()))
                break
    return tokens


# ── Parser ─────────────────────────────────────────────────────────────────────

class ExprParseError(Exception):
    def __init__(self, msg: str, pos: int = 0):
        self.pos = pos
        super().__init__(f"Parse error at position {pos}: {msg}")


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Optional[Token]:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def advance(self) -> Token:
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def expect(self, kind: int, value: str = None) -> Token:
        t = self.peek()
        if t is None:
            raise ExprParseError("unexpected end of expression")
        if t.kind != kind or (value is not None and t.value != value):
            raise ExprParseError(
                f"expected {value or kind}, got {t.value!r}", t.pos
            )
        return self.advance()

    # ── Grammar ──

    def parse(self) -> Expr:
        expr = self.parse_or()
        if self.peek() is not None:
            raise ExprParseError(
                f"unexpected token: {self.peek().value!r}", self.peek().pos
            )
        return expr

    def parse_or(self) -> Expr:
        left = self.parse_and()
        while self.peek() and self.peek().value == "or":
            self.advance()
            right = self.parse_and()
            left = BinaryOp("or", left, right)
        return left

    def parse_and(self) -> Expr:
        left = self.parse_not()
        while self.peek() and self.peek().value == "and":
            self.advance()
            right = self.parse_not()
            left = BinaryOp("and", left, right)
        return left

    def parse_not(self) -> Expr:
        if self.peek() and self.peek().value == "not":
            self.advance()
            operand = self.parse_not()
            return UnaryOp("not", operand)
        return self.parse_comparison()

    def parse_comparison(self) -> Expr:
        left = self.parse_term()
        if self.peek() and self.peek().kind == TOKEN_CMP:
            op = self.advance().value
            right = self.parse_term()
            return BinaryOp(op, left, right)
        return left

    def parse_term(self) -> Expr:
        left = self.parse_factor()
        while self.peek() and self.peek().kind == TOKEN_ADDOP and \
              self.peek().value in ("+", "-"):
            op = self.advance().value
            right = self.parse_factor()
            left = BinaryOp(op, left, right)
        return left

    def parse_factor(self) -> Expr:
        left = self.parse_unary()
        while self.peek() and self.peek().kind == TOKEN_ADDOP and \
              self.peek().value in ("*", "/"):
            op = self.advance().value
            right = self.parse_unary()
            left = BinaryOp(op, left, right)
        return left

    def parse_unary(self) -> Expr:
        if self.peek() and self.peek().kind == TOKEN_ADDOP and \
           self.peek().value == "-":
            self.advance()
            operand = self.parse_unary()
            return UnaryOp("-", operand)
        return self.parse_primary()

    def parse_primary(self) -> Expr:
        t = self.peek()
        if t is None:
            raise ExprParseError("unexpected end of expression")

        # Boolean literals
        if t.kind == TOKEN_BOOL_LIT:
            self.advance()
            return Literal(t.value == "true")

        # Numeric literals
        if t.kind == TOKEN_INT:
            self.advance()
            return Literal(int(t.value))
        if t.kind == TOKEN_FLOAT:
            self.advance()
            return Literal(float(t.value))

        # String literals
        if t.kind == TOKEN_STRING:
            self.advance()
            s = t.value[1:-1]  # strip quotes
            s = s.replace('\\"', '"').replace('\\\\', '\\')
            return Literal(s)

        # Parenthesized expression
        if t.kind == TOKEN_DELIM and t.value == "(":
            self.advance()
            expr = self.parse_or()
            self.expect(TOKEN_DELIM, ")")
            return expr

        # "result" keyword
        if t.kind == TOKEN_RESULT:
            self.advance()
            field = self._parse_field()
            return ResultAccess(field)

        # "history" keyword
        if t.kind == TOKEN_HISTORY:
            self.advance()
            field = self._parse_field()
            return HistoryAccess(field)

        # "deps" keyword
        if t.kind == TOKEN_DEPS:
            self.advance()
            self.expect(TOKEN_DELIM, ".")
            mod_tok = self.expect(TOKEN_IDENT)
            module = mod_tok.value
            self.expect(TOKEN_DELIM, ".")
            op_tok = self.expect(TOKEN_IDENT)
            operation = op_tok.value

            args = []
            if self.peek() and self.peek().kind == TOKEN_DELIM and \
               self.peek().value == "(":
                self.advance()
                args = self.parse_args()
                self.expect(TOKEN_DELIM, ")")

            field = self._parse_field()
            return DepCall(module, operation, args, field)

        # Aggregate functions: distinct(expr), count(expr), all(expr), any(expr)
        if t.kind == TOKEN_AGGREGATE:
            agg = self.advance().value
            self.expect(TOKEN_DELIM, "(")
            arg = self.parse_or()
            self.expect(TOKEN_DELIM, ")")
            return AggregateCall(agg, arg)

        # "len" function call
        if t.kind == TOKEN_FUNC and t.value == "len":
            self.advance()
            self.expect(TOKEN_DELIM, "(")
            arg = self.parse_or()
            self.expect(TOKEN_DELIM, ")")
            return FuncCall("len", arg)

        # Identifier
        if t.kind == TOKEN_IDENT:
            self.advance()
            name = t.value
            return Variable(name)

        raise ExprParseError(f"unexpected token: {t.value!r}", t.pos)

    def _parse_field(self) -> Optional[str]:
        """Parse optional .field access after result/history/deps. Handles keyword collision."""
        if self.peek() and self.peek().kind == TOKEN_DELIM and \
           self.peek().value == ".":
            self.advance()
            # After a dot, ANY token that looks like an identifier is a field name
            t = self.peek()
            if t is None:
                raise ExprParseError("expected field name after '.'")
            # Accept identifiers AND keywords (they become field names after dot)
            if t.kind in (TOKEN_IDENT, TOKEN_RESULT, TOKEN_HISTORY, TOKEN_DEPS,
                         TOKEN_FUNC, TOKEN_AGGREGATE, TOKEN_BOOL_LIT):
                self.advance()
                return t.value
            raise ExprParseError(f"expected field name after '.', got {t.value!r}", t.pos)
        return None

    def parse_args(self) -> list[Expr]:
        args = []
        if self.peek() and (self.peek().kind != TOKEN_DELIM or
                            self.peek().value != ")"):
            args.append(self.parse_or())
            while self.peek() and self.peek().kind == TOKEN_DELIM and \
                  self.peek().value == ",":
                self.advance()
                args.append(self.parse_or())
        return args


def parse_expr(text: str) -> Expr:
    """Parse an expression string into an AST."""
    tokens = tokenize(text)
    if not tokens:
        raise ExprParseError("empty expression")
    parser = Parser(tokens)
    return parser.parse()


# ── Type Checker ───────────────────────────────────────────────────────────────

class ExprTypeError(Exception):
    def __init__(self, msg: str, node: Expr = None):
        self.node = node
        super().__init__(msg)


class TypeChecker:
    """Validate expression types against declared operation I/O types."""

    def __init__(self, inputs: dict[str, str], outputs: dict[str, str],
                 errors: list[str], dep_contracts: dict):
        """
        Args:
            inputs: param_name → v1_type for this operation
            outputs: param_name → v1_type for this operation's return
            errors: list of error names this operation can raise
            dep_contracts: {module_name: {"operations": {op_name: {
                "inputs": {param: type}, "outputs": {param: type}}}}}
        """
        self.inputs = inputs
        self.outputs = outputs
        self.errors = set(errors)
        self.dep_contracts = dep_contracts
        self._result_type = "dict"  # result is always a dict in v1
        self._result_fields = outputs

    def check(self, node: Expr) -> str:
        """Type-check an expression and return its inferred type."""
        return self._check(node)

    def _check(self, node: Expr) -> str:
        if isinstance(node, Literal):
            return node.type  # already set in __init__

        elif isinstance(node, Variable):
            if node.name not in self.inputs:
                raise ExprTypeError(
                    f"unknown input variable '{node.name}'. "
                    f"Known inputs: {sorted(self.inputs)}", node
                )
            node.type = self.inputs[node.name]
            return node.type

        elif isinstance(node, ResultAccess):
            if node.field is None:
                node.type = "dict"  # whole result
                return "dict"
            if node.field not in self._result_fields:
                raise ExprTypeError(
                    f"unknown result field '{node.field}'. "
                    f"Known outputs: {sorted(self._result_fields)}", node
                )
            node.type = self._result_fields[node.field]
            return node.type

        elif isinstance(node, DepCall):
            return self._check_dep_call(node)

        elif isinstance(node, UnaryOp):
            inner = self._check(node.operand)
            if node.op == "not":
                if inner != "bool":
                    raise ExprTypeError(
                        f"'not' requires bool operand, got {inner}", node
                    )
                node.type = "bool"
                return "bool"
            elif node.op == "-":
                if inner not in ("int", "float"):
                    raise ExprTypeError(
                        f"unary '-' requires int or float, got {inner}", node
                    )
                node.type = inner
                return inner

        elif isinstance(node, BinaryOp):
            return self._check_binary(node)

        elif isinstance(node, FuncCall):
            return self._check_func_call(node)

        raise ExprTypeError(f"unknown expression node: {type(node).__name__}", node)

    def _check_dep_call(self, node: DepCall) -> str:
        if node.module not in self.dep_contracts:
            raise ExprTypeError(
                f"unknown dependency module '{node.module}'. "
                f"Known deps: {sorted(self.dep_contracts)}", node
            )

        dep = self.dep_contracts[node.module]
        ops = dep.get("operations", {})
        if node.operation not in ops:
            raise ExprTypeError(
                f"unknown operation '{node.operation}' on '{node.module}'. "
                f"Known ops: {sorted(ops)}", node
            )

        op_spec = ops[node.operation]
        op_inputs = op_spec.get("inputs", {})
        op_outputs = op_spec.get("outputs", {})

        # Check argument count
        if len(node.args) != len(op_inputs):
            raise ExprTypeError(
                f"'{node.module}.{node.operation}' expects "
                f"{len(op_inputs)} args, got {len(node.args)}", node
            )

        # Check argument types (positional by declaration order)
        for arg, (pname, ptype) in zip(node.args, op_inputs.items()):
            arg_type = self._check(arg)
            if arg_type != ptype:
                raise ExprTypeError(
                    f"'{node.module}.{node.operation}' arg '{pname}' expects "
                    f"{ptype}, got {arg_type}", arg
                )

        # Resolve return field
        if node.field is None:
            node.type = "dict"
            return "dict"
        if node.field not in op_outputs:
            raise ExprTypeError(
                f"unknown output field '{node.field}' on "
                f"'{node.module}.{node.operation}'. "
                f"Known outputs: {sorted(op_outputs)}", node
            )
        node.type = op_outputs[node.field]
        return node.type

    def _check_binary(self, node: BinaryOp) -> str:
        left_type = self._check(node.left)
        right_type = self._check(node.right)

        # Boolean operators
        if node.op in ("and", "or"):
            if left_type != "bool" or right_type != "bool":
                raise ExprTypeError(
                    f"'{node.op}' requires bool operands, got {left_type} and {right_type}",
                    node
                )
            node.type = "bool"
            return "bool"

        # Comparison operators
        if node.op in ("==", "!="):
            if left_type != right_type:
                raise ExprTypeError(
                    f"'{node.op}' requires same types, got {left_type} and {right_type}",
                    node
                )
            node.type = "bool"
            return "bool"

        if node.op in ("<", ">", "<=", ">="):
            if left_type not in ("int", "float", "string") or \
               right_type not in ("int", "float", "string"):
                raise ExprTypeError(
                    f"'{node.op}' requires int/float/string, got {left_type} and {right_type}",
                    node
                )
            # Allow mixed int/float comparison
            if left_type == "string" and right_type != "string":
                raise ExprTypeError(
                    f"'{node.op}' string comparison requires string operand, got {right_type}",
                    node
                )
            if right_type == "string" and left_type != "string":
                raise ExprTypeError(
                    f"'{node.op}' string comparison requires string operand, got {left_type}",
                    node
                )
            node.type = "bool"
            return "bool"

        # Arithmetic
        if node.op in ("+", "-", "*", "/"):
            if left_type not in ("int", "float") or right_type not in ("int", "float"):
                raise ExprTypeError(
                    f"'{node.op}' requires numeric operands, got {left_type} and {right_type}",
                    node
                )
            # Result is float if either operand is float, int otherwise
            result_type = "float" if ("float" in (left_type, right_type)) else "int"
            node.type = result_type
            return result_type

        raise ExprTypeError(f"unknown binary operator: {node.op}", node)

    def _check_func_call(self, node: FuncCall) -> str:
        if node.func == "len":
            arg_type = self._check(node.arg)
            if arg_type not in ("string", "list<string>"):
                raise ExprTypeError(
                    f"'len' requires string or list, got {arg_type}", node
                )
            node.type = "int"
            return "int"

        raise ExprTypeError(f"unknown function: {node.func}", node)
