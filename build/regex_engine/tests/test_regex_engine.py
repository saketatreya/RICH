"""Tests for regex_engine."""

from regex_engine import matches


def test_matches_found():
    result = matches(text="hello world", pattern=r"hello")
    assert result["ok"] is True
    assert result["match"] == "hello"


def test_matches_not_found():
    result = matches(text="hello world", pattern=r"xyz")
    assert result["ok"] is False
    assert result["match"] == ""


def test_matches_email():
    result = matches(text="user@gmail.com", pattern=r"@gmail\.")
    assert result["ok"] is True
