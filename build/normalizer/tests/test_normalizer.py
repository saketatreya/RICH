"""Tests for normalizer — derived from contract."""

from normalizer import normalize


def test_normalize_strips_whitespace():
    result = normalize("  hello  ")
    assert result["normalized"] == "hello"


def test_normalize_lowercases():
    result = normalize("HELLO")
    assert result["normalized"] == "hello"


def test_normalize_empty_string():
    result = normalize("")
    assert result["normalized"] == ""


def test_normalize_already_clean():
    result = normalize("hello world")
    assert result["normalized"] == "hello world"
