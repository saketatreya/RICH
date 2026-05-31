"""Tests for validator — derived from contract."""

from validator import validate


def test_validate_normal_string():
    result = validate("hello world")
    assert result["valid"] is True
    assert result["reason"] == "OK"


def test_validate_empty_string():
    result = validate("")
    assert result["valid"] is False
    assert result["reason"] == "String is empty"


def test_validate_special_chars():
    result = validate("hello@world")
    assert result["valid"] is False
    assert result["reason"] == "String contains special characters"


def test_validate_numeric():
    result = validate("hello 123")
    assert result["valid"] is True
