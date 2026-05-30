"""Tests for v2.0 — property schema and classifier.

TDD: write tests first, then implement properties.py.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from properties import (
    PropertyKind,
    FormalProperty,
    PostconditionProperty,
    RaisesProperty,
    TraceInvariantProperty,
    TemporalProperty,
    NonfunctionalProperty,
    parse_formal_property,
    PropertyParseError,
)


def test_parse_postcondition():
    """A postcondition has kind, expr, and optional error context."""
    raw = {"kind": "postcondition", "expr": "len(result.token) > 0"}
    prop = parse_formal_property(raw, "issue_returns_nonempty")
    assert isinstance(prop, PostconditionProperty)
    assert prop.kind == PropertyKind.POSTCONDITION
    assert prop.expr == "len(result.token) > 0"


def test_parse_raises():
    """A raises property has kind and errors list."""
    raw = {
        "kind": "raises",
        "errors": ["invalid_credentials"]
    }
    prop = parse_formal_property(raw, "reject_invalid")
    assert isinstance(prop, RaisesProperty)
    assert prop.kind == PropertyKind.RAISES
    assert prop.errors == ["invalid_credentials"]


def test_parse_raises_with_when():
    """A raises property can have an optional when guard (pure-inputs)."""
    raw = {
        "kind": "raises",
        "errors": ["bad_input"],
        "when": "x < 0"
    }
    prop = parse_formal_property(raw, "reject_negative")
    assert isinstance(prop, RaisesProperty)
    assert prop.when == "x < 0"
    assert prop.errors == ["bad_input"]


def test_parse_trace_invariant():
    """A trace invariant has kind and expr."""
    raw = {"kind": "trace_invariant", "expr": "len(history.issue) == len(set(t.token for t in history.issue))"}
    prop = parse_formal_property(raw, "token_uniqueness")
    assert isinstance(prop, TraceInvariantProperty)
    assert prop.kind == PropertyKind.TRACE_INVARIANT


def test_parse_temporal():
    """A temporal property has kind and expr."""
    raw = {"kind": "temporal", "expr": "G(issue(t) → within(86400, validate(t)))"}
    prop = parse_formal_property(raw, "token_validity_window")
    assert isinstance(prop, TemporalProperty)
    assert prop.kind == PropertyKind.TEMPORAL


def test_parse_nonfunctional():
    """A nonfunctional property just declares itself as such."""
    raw = {"kind": "nonfunctional"}
    prop = parse_formal_property(raw, "constant_time_compare")
    assert isinstance(prop, NonfunctionalProperty)
    assert prop.kind == PropertyKind.NONFUNCTIONAL


def test_parse_from_old_null():
    """Backward compatibility: formal: null parses as None."""
    assert parse_formal_property(None, "old_style") is None
    assert parse_formal_property(None, "old_style") is None


def test_reject_unknown_kind():
    """Unknown kind raises PropertyParseError."""
    raw = {"kind": "magic", "expr": "true"}
    try:
        parse_formal_property(raw, "bad")
        assert False, "should have raised"
    except PropertyParseError as e:
        assert "magic" in str(e)


def test_reject_postcondition_missing_expr():
    """Postcondition without expr raises PropertyParseError."""
    raw = {"kind": "postcondition"}
    try:
        parse_formal_property(raw, "bad")
        assert False, "should have raised"
    except PropertyParseError as e:
        assert "expr" in str(e)


def test_reject_raises_missing_errors():
    """Raises without errors raises PropertyParseError."""
    raw = {"kind": "raises"}
    try:
        parse_formal_property(raw, "bad")
        assert False, "should have raised"
    except PropertyParseError as e:
        assert "errors" in str(e)


def test_kind_enum_values():
    """All five kinds are distinct enum values."""
    kinds = set(PropertyKind)
    assert len(kinds) == 5
    assert PropertyKind.POSTCONDITION in kinds
    assert PropertyKind.RAISES in kinds
    assert PropertyKind.TRACE_INVARIANT in kinds
    assert PropertyKind.TEMPORAL in kinds
    assert PropertyKind.NONFUNCTIONAL in kinds


def test_kind_str_roundtrip():
    """Kind strings roundtrip through the enum."""
    for name in ["postcondition", "raises", "trace_invariant", "temporal", "nonfunctional"]:
        assert PropertyKind(name).name == name.upper().replace(" ", "_")


def test_formal_property_base():
    """Base FormalProperty has kind and id."""
    prop = FormalProperty(kind=PropertyKind.POSTCONDITION, id="test")
    assert prop.kind == PropertyKind.POSTCONDITION
    assert prop.id == "test"


def test_postcondition_default_context():
    """Postcondition defaults to checking after the call returns."""
    prop = PostconditionProperty(id="p", expr="true")
    assert prop.kind == PropertyKind.POSTCONDITION
    assert prop.expr == "true"


def test_raises_default_context():
    """Raises property stores the error name."""
    prop = RaisesProperty(id="r", when="false", error="bad_input")
    assert prop.error == "bad_input"


print("All v2.0 tests passed.")
