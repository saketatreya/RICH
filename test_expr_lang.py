"""Tests for v2.1 — expression language.

TDD: tests for grammar, parser, AST, and type checker.
Covers the four example properties' expressions plus edge cases.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from expr_lang import (
    # AST
    Expr, Literal, Variable, ResultAccess, DepCall,
    UnaryOp, BinaryOp, FuncCall,
    # Parser
    parse_expr, ExprParseError,
    # Type checker
    TypeChecker, ExprTypeError, V1_TYPES,
)


# ── PARSER TESTS ───────────────────────────────────────────────────────────

def test_parse_string_literal():
    expr = parse_expr('"hello"')
    assert isinstance(expr, Literal)
    assert expr.value == "hello"
    assert expr.type == "string"


def test_parse_int_literal():
    expr = parse_expr("42")
    assert isinstance(expr, Literal)
    assert expr.value == 42
    assert expr.type == "int"


def test_parse_float_literal():
    expr = parse_expr("3.14")
    assert isinstance(expr, Literal)
    assert expr.value == 3.14
    assert expr.type == "float"


def test_parse_bool_literals():
    assert parse_expr("true").value is True
    assert parse_expr("false").value is False


def test_parse_variable():
    expr = parse_expr("username")
    assert isinstance(expr, Variable)
    assert expr.name == "username"


def test_parse_result_access():
    expr = parse_expr("result.token")
    assert isinstance(expr, ResultAccess)
    assert expr.field == "token"


def test_parse_result_access_no_field():
    expr = parse_expr("result")
    assert isinstance(expr, ResultAccess)
    assert expr.field is None  # whole result dict


def test_parse_dep_call():
    expr = parse_expr("deps.token_store.issue(subject).token")
    assert isinstance(expr, DepCall)
    assert expr.module == "token_store"
    assert expr.operation == "issue"
    assert len(expr.args) == 1
    assert expr.field == "token"


def test_parse_dep_call_no_args():
    expr = parse_expr("deps.stats.ping()")
    assert isinstance(expr, DepCall)
    assert expr.module == "stats"
    assert expr.operation == "ping"
    assert expr.args == []
    assert expr.field is None


def test_parse_comparison():
    expr = parse_expr("len(result.token) > 0")
    assert isinstance(expr, BinaryOp)
    assert expr.op == ">"
    assert isinstance(expr.left, FuncCall)


def test_parse_boolean_and():
    expr = parse_expr("a == 1 and b == 2")
    assert isinstance(expr, BinaryOp)
    assert expr.op == "and"


def test_parse_boolean_or():
    expr = parse_expr("a == 1 or b == 2")
    assert isinstance(expr, BinaryOp)
    assert expr.op == "or"


def test_parse_not():
    expr = parse_expr("not ok")
    assert isinstance(expr, UnaryOp)
    assert expr.op == "not"


def test_parse_nested_dep_result():
    """The reject_invalid expression: not deps.X.Y(a, b).ok"""
    expr = parse_expr("not deps.user_repo.verify_password(username, password).ok")
    assert isinstance(expr, UnaryOp)
    assert expr.op == "not"
    assert isinstance(expr.operand, DepCall)
    assert expr.operand.module == "user_repo"
    assert expr.operand.operation == "verify_password"
    assert expr.operand.field == "ok"


def test_parse_len_call():
    expr = parse_expr("len(result.token)")
    assert isinstance(expr, FuncCall)
    assert expr.func == "len"
    assert isinstance(expr.arg, ResultAccess)


def test_parse_arithmetic():
    expr = parse_expr("x + y * 2")
    assert isinstance(expr, BinaryOp)
    assert expr.op == "+"


def test_parse_precedence():
    """Multiplication binds tighter than addition."""
    expr = parse_expr("1 + 2 * 3")
    # Should be (1 + (2 * 3)), not ((1 + 2) * 3)
    assert isinstance(expr, BinaryOp)
    assert expr.op == "+"
    assert isinstance(expr.left, Literal)
    assert isinstance(expr.right, BinaryOp)
    assert expr.right.op == "*"


def test_parse_parentheses():
    expr = parse_expr("(1 + 2) * 3")
    assert isinstance(expr, BinaryOp)
    assert expr.op == "*"
    assert isinstance(expr.left, BinaryOp)


def test_parse_error_unclosed_string():
    try:
        parse_expr('"hello')
        assert False
    except ExprParseError:
        pass


def test_parse_error_unknown_token():
    try:
        parse_expr("x @ y")
        assert False
    except ExprParseError:
        pass


# ── TYPE CHECKER TESTS ──────────────────────────────────────────────────────

def make_checker():
    """Create a type checker for auth's authenticate operation."""
    return TypeChecker(
        inputs={"username": "string", "password": "string"},
        outputs={"token": "string"},
        errors=["invalid_credentials"],
        dep_contracts={
            "user_repo": {
                "operations": {
                    "verify_password": {
                        "inputs": {"username": "string", "password": "string"},
                        "outputs": {"ok": "bool"},
                    }
                }
            },
            "token_store": {
                "operations": {
                    "issue": {
                        "inputs": {"subject": "string"},
                        "outputs": {"token": "string"},
                    }
                }
            },
        },
    )


def test_type_check_literal():
    checker = make_checker()
    expr = parse_expr("42")
    assert checker.check(expr) == "int"


def test_type_check_input_variable():
    checker = make_checker()
    expr = parse_expr("username")
    assert checker.check(expr) == "string"


def test_type_check_unknown_variable():
    checker = make_checker()
    expr = parse_expr("nonexistent")
    try:
        checker.check(expr)
        assert False
    except ExprTypeError as e:
        assert "nonexistent" in str(e)


def test_type_check_result_access():
    checker = make_checker()
    expr = parse_expr("result.token")
    assert checker.check(expr) == "string"


def test_type_check_result_invalid_field():
    checker = make_checker()
    expr = parse_expr("result.nonexistent")
    try:
        checker.check(expr)
        assert False
    except ExprTypeError as e:
        assert "nonexistent" in str(e)


def test_type_check_dep_call():
    checker = make_checker()
    expr = parse_expr("deps.user_repo.verify_password(username, password).ok")
    assert checker.check(expr) == "bool"


def test_type_check_dep_call_wrong_arg_type():
    checker = make_checker()
    expr = parse_expr("deps.user_repo.verify_password(42, password).ok")
    try:
        checker.check(expr)
        assert False
    except ExprTypeError as e:
        assert "int" in str(e).lower() or "42" in str(e)


def test_type_check_comparison():
    checker = make_checker()
    expr = parse_expr("len(result.token) > 0")
    assert checker.check(expr) == "bool"


def test_type_check_boolean_op():
    checker = make_checker()
    expr = parse_expr("true and false")
    assert checker.check(expr) == "bool"


def test_type_check_not():
    checker = make_checker()
    expr = parse_expr("not deps.user_repo.verify_password(username, password).ok")
    assert checker.check(expr) == "bool"


def test_type_check_len_requires_string():
    checker = make_checker()
    expr = parse_expr("len(42)")
    try:
        checker.check(expr)
        assert False
    except ExprTypeError:
        pass


def test_type_check_comparison_type_mismatch():
    checker = make_checker()
    expr = parse_expr('"hello" > 42')
    try:
        checker.check(expr)
        assert False
    except ExprTypeError:
        pass


def test_type_check_dep_module_unknown():
    checker = make_checker()
    expr = parse_expr("deps.nonexistent.foo().x")
    try:
        checker.check(expr)
        assert False
    except ExprTypeError as e:
        assert "nonexistent" in str(e)


# ── PROPERTY-SPECIFIC EXPRESSIONS ──────────────────────────────────────────

def test_issue_returns_nonempty_parses():
    """len(result.token) > 0"""
    expr = parse_expr("len(result.token) > 0")
    assert isinstance(expr, BinaryOp)

    # Check against token_store.issue types
    checker = TypeChecker(
        inputs={"subject": "string"},
        outputs={"token": "string"},
        errors=[],
        dep_contracts={},
    )
    assert checker.check(expr) == "bool"


def test_reject_invalid_parses():
    """not deps.user_repo.verify_password(username, password).ok"""
    expr = parse_expr("not deps.user_repo.verify_password(username, password).ok")
    assert isinstance(expr, UnaryOp)

    checker = make_checker()
    assert checker.check(expr) == "bool"


print("All v2.1 tests passed.")
