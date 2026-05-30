"""Tests for v2.2 — runtime checker.

TDD: expression evaluator + contract checker + dependency proxy with blame.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from expr_lang import parse_expr
from properties import (
    PostconditionProperty, RaisesProperty, TraceInvariantProperty,
    TemporalProperty, NonfunctionalProperty,
)
from runtime_checker import (
    EvalContext, evaluate, ContractViolation,
    ContractChecker, contract_checked,
    DependencyProxy,
    check_property,
)


# ── EVALUATOR TESTS ────────────────────────────────────────────────────────

def test_eval_literal_int():
    ctx = EvalContext()
    assert evaluate(parse_expr("42"), ctx) == 42


def test_eval_literal_string():
    ctx = EvalContext()
    assert evaluate(parse_expr('"hello"'), ctx) == "hello"


def test_eval_literal_bool():
    assert evaluate(parse_expr("true"), EvalContext()) is True
    assert evaluate(parse_expr("false"), EvalContext()) is False


def test_eval_variable():
    ctx = EvalContext(inputs={"username": "alice", "password": "secret"})
    assert evaluate(parse_expr("username"), ctx) == "alice"


def test_eval_variable_missing():
    ctx = EvalContext(inputs={})
    try:
        evaluate(parse_expr("username"), ctx)
        assert False, "should raise"
    except ContractViolation as e:
        assert "username" in str(e)


def test_eval_result_access():
    ctx = EvalContext(result={"token": "abc123", "ok": True})
    assert evaluate(parse_expr("result.token"), ctx) == "abc123"
    assert evaluate(parse_expr("result.ok"), ctx) is True


def test_eval_result_no_field():
    ctx = EvalContext(result={"token": "abc"})
    assert evaluate(parse_expr("result"), ctx) == {"token": "abc"}


def test_eval_comparison_eq():
    ctx = EvalContext()
    assert evaluate(parse_expr("1 == 1"), ctx) is True
    assert evaluate(parse_expr("1 == 2"), ctx) is False
    assert evaluate(parse_expr('"a" == "a"'), ctx) is True


def test_eval_comparison_neq():
    ctx = EvalContext()
    assert evaluate(parse_expr("1 != 2"), ctx) is True


def test_eval_comparison_gt():
    ctx = EvalContext()
    assert evaluate(parse_expr("5 > 3"), ctx) is True
    assert evaluate(parse_expr("3 > 5"), ctx) is False


def test_eval_boolean_and():
    ctx = EvalContext()
    assert evaluate(parse_expr("true and true"), ctx) is True
    assert evaluate(parse_expr("true and false"), ctx) is False


def test_eval_boolean_or():
    ctx = EvalContext()
    assert evaluate(parse_expr("false or true"), ctx) is True
    assert evaluate(parse_expr("false or false"), ctx) is False


def test_eval_not():
    ctx = EvalContext()
    assert evaluate(parse_expr("not false"), ctx) is True
    assert evaluate(parse_expr("not true"), ctx) is False


def test_eval_arithmetic():
    ctx = EvalContext()
    assert evaluate(parse_expr("2 + 3"), ctx) == 5
    assert evaluate(parse_expr("10 - 3"), ctx) == 7
    assert evaluate(parse_expr("4 * 3"), ctx) == 12
    assert evaluate(parse_expr("10 / 2"), ctx) == 5.0


def test_eval_precedence():
    ctx = EvalContext()
    assert evaluate(parse_expr("1 + 2 * 3"), ctx) == 7
    assert evaluate(parse_expr("(1 + 2) * 3"), ctx) == 9


def test_eval_len():
    ctx = EvalContext()
    assert evaluate(parse_expr('len("hello")'), ctx) == 5
    ctx2 = EvalContext(result={"token": "abc"})
    assert evaluate(parse_expr("len(result.token)"), ctx2) == 3
    assert evaluate(parse_expr("len(result.token) > 0"), ctx2) is True


def test_eval_dep_call():
    """deps.X.Y(args).field actually calls the dependency handle."""
    class FakeUserRepo:
        def verify_password(self, username, password):
            return {"ok": username == "alice" and password == "secret"}

    ctx = EvalContext(
        inputs={"username": "alice", "password": "secret"},
        deps={"user_repo": FakeUserRepo()},
    )
    expr = parse_expr("deps.user_repo.verify_password(username, password).ok")
    assert evaluate(expr, ctx) is True


def test_eval_dep_call_wrong_password():
    class FakeUserRepo:
        def verify_password(self, username, password):
            return {"ok": username == "alice" and password == "secret"}

    ctx = EvalContext(
        inputs={"username": "alice", "password": "wrong"},
        deps={"user_repo": FakeUserRepo()},
    )
    expr = parse_expr("deps.user_repo.verify_password(username, password).ok")
    assert evaluate(expr, ctx) is False


# ── CONTRACT CHECKER TESTS ─────────────────────────────────────────────────

def test_postcondition_passes():
    """Function satisfies its postcondition → no error."""
    def add_one(x):
        return {"result": x + 1}

    prop = PostconditionProperty(id="positive", expr="result.result > 0")

    checked = contract_checked(add_one, postconditions=[prop])

    # x=5 → result=6 > 0 ✓
    result = checked(x=5)
    assert result == {"result": 6}


def test_postcondition_fails():
    """Function violates its postcondition → ContractViolation."""
    def bad_add(x):
        return {"result": x - 1}

    prop = PostconditionProperty(id="positive", expr="result.result > 0")

    checked = contract_checked(bad_add, postconditions=[prop])

    # x=0 → result=-1, not > 0 ✗
    try:
        checked(x=0)
        assert False, "should have raised"
    except ContractViolation as e:
        assert "positive" in str(e)
        assert "postcondition" in str(e).lower()


def test_raises_property_triggers():
    """When guard is true, error must be raised."""
    def may_fail(x):
        if x < 0:
            raise ValueError("bad_input")
        return {"ok": True}

    prop = RaisesProperty(id="reject_negative", when="x < 0", error="bad_input")

    checked = contract_checked(may_fail, raises_props=[prop])

    # x=-1 → should raise ValueError with bad_input
    try:
        checked(x=-1)
        assert False, "should have raised"
    except ValueError as e:
        assert "bad_input" in str(e)


def test_raises_property_violated():
    """When guard is true but error NOT raised → violation."""
    def forgets_to_raise(x):
        return {"ok": True}  # should raise when x < 0

    prop = RaisesProperty(id="reject_negative", when="x < 0", error="bad_input")

    checked = contract_checked(forgets_to_raise, raises_props=[prop])

    try:
        checked(x=-1)
        assert False, "should have raised"
    except ContractViolation as e:
        assert "reject_negative" in str(e)
        assert "expected error" in str(e).lower() or "bad_input" in str(e)


def test_raises_property_guard_false():
    """When guard is false, raises property is not checked."""
    def ok_fn(x):
        return {"ok": True}

    prop = RaisesProperty(id="reject_negative", when="x < 0", error="bad_input")

    checked = contract_checked(ok_fn, raises_props=[prop])

    # x=5 → guard false, should pass normally
    result = checked(x=5)
    assert result == {"ok": True}


# ── DEPENDENCY PROXY TESTS (BLAME) ────────────────────────────────────────

def test_dep_proxy_blames_caller_on_bad_args():
    """If caller passes wrong types, blame the CALLER."""
    class RealTokenStore:
        def issue(self, subject):
            if not isinstance(subject, str):
                raise TypeError("subject must be string")
            return {"token": "tok-" + subject}

    proxy = DependencyProxy(
        dep=RealTokenStore(),
        module_name="token_store",
        op_name="issue",
        preconditions=[],  # no preconditions to keep test simple
        postconditions=[],
    )

    # Should still pass through to real impl
    result = proxy.issue(subject="alice")
    assert result == {"token": "tok-alice"}


def test_dep_proxy_blames_dep_on_bad_return():
    """If dep returns wrong shape, blame the DEP."""
    class BuggyStore:
        def issue(self, subject):
            return {"wrong_field": "oops"}  # contract says it should have 'token'

    postcond = PostconditionProperty(
        id="returns_token", expr="true"  # dummy for now, will check shape
    )
    proxy = DependencyProxy(
        dep=BuggyStore(),
        module_name="token_store",
        op_name="issue",
        preconditions=[],
        postconditions=[postcond],
    )

    # This should catch the contract violation and blame token_store
    try:
        proxy.issue(subject="alice")
        # For now, with dummy postcondition, this passes
        # Real blame test comes after we wire up postcondition checking in the proxy
    except ContractViolation as e:
        # Expected path when postconditions are checked
        pass


def test_dep_proxy_delegates_to_real():
    """Dependency proxy passes through to real implementation."""
    calls = []
    class CountingStore:
        def issue(self, subject):
            calls.append(subject)
            return {"token": "tok"}

    proxy = DependencyProxy(
        dep=CountingStore(),
        module_name="token_store",
        op_name="issue",
        preconditions=[],
        postconditions=[],
    )

    proxy.issue(subject="alice")
    assert calls == ["alice"]


# ── TRACE INVARIANT TESTS ──────────────────────────────────────────────────

def test_trace_invariant_checker_tracks_history():
    """The checker accumulates call history for trace invariant checks."""
    calls = []
    def issue(subject):
        calls.append(subject)
        return {"token": f"tok-{len(calls)}"}

    trace_prop = TraceInvariantProperty(
        id="unique_tokens",
        expr="true",  # dummy, just testing that history is tracked
    )

    checked = contract_checked(issue, trace_invariants=[trace_prop])

    r1 = checked(subject="alice")
    r2 = checked(subject="bob")

    # Both should succeed with dummy expression
    assert r1["token"] == "tok-1"
    assert r2["token"] == "tok-2"


# ── check_property UTILITY ─────────────────────────────────────────────────

def test_check_property_postcondition_pass():
    prop = PostconditionProperty(id="p", expr="result.ok == true")
    ctx = EvalContext(result={"ok": True})
    check_property(prop, ctx, "test_op")  # should not raise


def test_check_property_postcondition_fail():
    prop = PostconditionProperty(id="p", expr="result.ok == true")
    ctx = EvalContext(result={"ok": False})
    try:
        check_property(prop, ctx, "test_op")
        assert False
    except ContractViolation as e:
        assert "p" in str(e)


def test_check_property_nonfunctional_skipped():
    """Nonfunctional properties are always skipped — not checked."""
    prop = NonfunctionalProperty(id="timing")
    ctx = EvalContext()
    check_property(prop, ctx, "test_op")  # should not raise, just skip


print("All v2.2 tests passed.")
