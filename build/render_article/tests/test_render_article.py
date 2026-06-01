import pytest
from render_article import render


def test_render_returns_published_key():
    result = render(title="Hello", body="Some text.", analysis={"word_count": 2, "reading_level": "easy"})
    assert "published" in result


def test_render_published_contains_title():
    result = render(title="My Article", body="Body text.", analysis={"word_count": 2, "reading_level": "grade 5"})
    assert result["published"]["title"] == "My Article"


def test_render_published_contains_body():
    result = render(title="My Article", body="Body text.", analysis={"word_count": 2, "reading_level": "grade 5"})
    assert result["published"]["body"] == "Body text."


def test_render_published_contains_analysis():
    analysis = {"word_count": 42, "reading_level": "college"}
    result = render(title="Deep Thoughts", body="...", analysis=analysis)
    assert result["published"]["analysis"] == analysis


def test_render_summary_format():
    result = render(
        title="Science Today",
        body="Content here.",
        analysis={"word_count": 150, "reading_level": "grade 8"},
    )
    assert result["published"]["summary"] == "Science Today (150 words, grade 8)"


def test_render_summary_uses_analysis_word_count():
    result = render(
        title="Short Piece",
        body="Irrelevant body.",
        analysis={"word_count": 7, "reading_level": "beginner"},
    )
    assert "7 words" in result["published"]["summary"]


def test_render_summary_uses_analysis_reading_level():
    result = render(
        title="Short Piece",
        body="Irrelevant body.",
        analysis={"word_count": 7, "reading_level": "advanced"},
    )
    assert "advanced" in result["published"]["summary"]


def test_render_summary_contains_title():
    result = render(
        title="Unique Title XYZ",
        body=".",
        analysis={"word_count": 1, "reading_level": "easy"},
    )
    assert result["published"]["summary"].startswith("Unique Title XYZ")


def test_render_published_has_exactly_four_keys():
    result = render(
        title="Article",
        body="Body.",
        analysis={"word_count": 5, "reading_level": "grade 3"},
    )
    assert set(result["published"].keys()) == {"title", "body", "analysis", "summary"}


def test_render_analysis_passthrough_unchanged():
    analysis = {"word_count": 100, "reading_level": "intermediate", "extra_field": "preserved"}
    result = render(title="T", body="B", analysis=analysis)
    assert result["published"]["analysis"] is analysis


def test_render_empty_title():
    result = render(title="", body="Some body.", analysis={"word_count": 2, "reading_level": "easy"})
    assert result["published"]["title"] == ""
    assert result["published"]["summary"] == " (2 words, easy)"


def test_render_empty_body():
    result = render(title="Empty Body Article", body="", analysis={"word_count": 0, "reading_level": "n/a"})
    assert result["published"]["body"] == ""


def test_render_zero_word_count():
    result = render(
        title="Empty",
        body="",
        analysis={"word_count": 0, "reading_level": "n/a"},
    )
    assert "0 words" in result["published"]["summary"]


def test_render_analysis_with_extra_keys_does_not_affect_summary():
    analysis = {"word_count": 20, "reading_level": "grade 6", "sentiment": "positive"}
    result = render(title="Extra Keys", body="Body.", analysis=analysis)
    assert result["published"]["summary"] == "Extra Keys (20 words, grade 6)"


def test_render_large_word_count():
    result = render(
        title="Epic Novel",
        body="Long content.",
        analysis={"word_count": 120000, "reading_level": "literary"},
    )
    assert result["published"]["summary"] == "Epic Novel (120000 words, literary)"
