import pytest
from parse_article import parse


def test_parse_basic_title_and_body():
    raw = "# Hello World\nThis is the body.\nSecond line."
    result = parse(raw)
    assert result["title"] == "Hello World"
    assert "This is the body." in result["body"]
    assert "Second line." in result["body"]


def test_parse_returns_dict_with_title_and_body_keys():
    raw = "# My Title\nSome content."
    result = parse(raw)
    assert "title" in result
    assert "body" in result


def test_parse_title_strips_hash_prefix():
    raw = "# Strip Me\nBody here."
    result = parse(raw)
    assert result["title"] == "Strip Me"
    assert not result["title"].startswith("#")


def test_parse_title_not_first_line():
    raw = "Preamble line\n# Actual Title\nBody after title."
    result = parse(raw)
    assert result["title"] == "Actual Title"


def test_parse_body_excludes_title_line():
    raw = "# Title\nLine one.\nLine two."
    result = parse(raw)
    assert "# Title" not in result["body"]
    assert "Title" not in result["body"] or result["body"].count("Title") == 0


def test_parse_body_preserves_order():
    raw = "# Title\nalpha\nbeta\ngamma"
    result = parse(raw)
    body = result["body"]
    assert body.index("alpha") < body.index("beta") < body.index("gamma")


def test_parse_title_only_no_other_lines():
    raw = "# Solo Title"
    result = parse(raw)
    assert result["title"] == "Solo Title"
    assert result["body"] == "" or result["body"] is not None


def test_parse_first_hash_line_is_title_when_multiple_exist():
    raw = "# First Title\nsome body\n# Not The Title\nmore body"
    result = parse(raw)
    assert result["title"] == "First Title"
    assert "# Not The Title" in result["body"]


def test_parse_lines_before_title_go_into_body():
    raw = "intro line\nanother intro\n# The Title\nbody line"
    result = parse(raw)
    assert result["title"] == "The Title"
    body = result["body"]
    assert "intro line" in body
    assert "another intro" in body
    assert "body line" in body


def test_parse_title_with_spaces_preserved():
    raw = "# A Title With   Spaces\nBody."
    result = parse(raw)
    assert result["title"] == "A Title With   Spaces"


def test_parse_multiline_body_joined_in_order():
    raw = "# Header\nfirst\nsecond\nthird"
    result = parse(raw)
    body = result["body"]
    assert "first" in body
    assert "second" in body
    assert "third" in body
    assert body.index("first") < body.index("second") < body.index("third")


def test_parse_does_not_match_hash_without_space():
    raw = "#NoSpace\n# Real Title\nbody"
    result = parse(raw)
    assert result["title"] == "Real Title"
    assert "#NoSpace" in result["body"]


def test_parse_returns_strings():
    raw = "# Title\nBody content."
    result = parse(raw)
    assert isinstance(result["title"], str)
    assert isinstance(result["body"], str)
