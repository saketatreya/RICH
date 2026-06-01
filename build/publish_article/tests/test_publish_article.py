import importlib
import inspect
import pytest

_mod = importlib.import_module("publish_article")
WiringClass = next(
    c for _n, c in inspect.getmembers(_mod, inspect.isclass)
    if c.__module__ == "publish_article"
)


def _make_deps(
    title="Test Title",
    body="Some body text here.",
    analysis=None,
    published=None,
):
    if analysis is None:
        analysis = {"word_count": 4, "reading_level": "easy", "top_keyword": "some", "has_links": False}
    if published is None:
        published = {"title": title, "body": body, "analysis": analysis, "summary": f"{title} (4 words, easy)"}

    class _FakeParseArticle:
        def parse(self, *a, **k):
            return {"title": title, "body": body}

    class _FakeAnalyzeBody:
        def analyze(self, *a, **k):
            return {"analysis": analysis}

    class _FakeRenderArticle:
        def render(self, *a, **k):
            return {"published": published}

    return _FakeParseArticle(), _FakeAnalyzeBody(), _FakeRenderArticle()


def _make_wiring(title="Test Title", body="Some body text here.", analysis=None, published=None):
    pa, ab, ra = _make_deps(title=title, body=body, analysis=analysis, published=published)
    return WiringClass(parse_article=pa, analyze_body=ab, render_article=ra)


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

def test_publish_returns_dict_with_all_keys():
    w = _make_wiring()
    result = w.publish(raw="# Test Title\nSome body text here.")
    assert isinstance(result, dict)
    assert "title" in result
    assert "body" in result
    assert "analysis" in result
    assert "published" in result


# ---------------------------------------------------------------------------
# Data-flow: parse output surfaces in result
# ---------------------------------------------------------------------------

def test_publish_title_surfaces_from_parse():
    w = _make_wiring(title="My Article")
    result = w.publish(raw="# My Article\nBody.")
    assert result["title"] == "My Article"


def test_publish_body_surfaces_from_parse():
    w = _make_wiring(body="The body content.")
    result = w.publish(raw="# T\nThe body content.")
    assert result["body"] == "The body content."


def test_publish_analysis_surfaces_from_analyze():
    expected_analysis = {"word_count": 10, "reading_level": "hard", "top_keyword": "the", "has_links": True}
    w = _make_wiring(analysis=expected_analysis)
    result = w.publish(raw="# T\nBody.")
    assert result["analysis"] == expected_analysis


def test_publish_published_surfaces_from_render():
    expected_published = {"title": "X", "body": "Y", "analysis": {}, "summary": "X (1 words, easy)"}
    w = _make_wiring(published=expected_published)
    result = w.publish(raw="# X\nY")
    assert result["published"] == expected_published


# ---------------------------------------------------------------------------
# Data-flow: parse feeds analyze (body threading)
# ---------------------------------------------------------------------------

def test_publish_analyze_receives_body_from_parse():
    received = {}

    class _SpyAnalyzeBody:
        def analyze(self, *a, **k):
            received["body"] = k.get("body") or (a[0] if a else None)
            return {"analysis": {"word_count": 1, "reading_level": "easy", "top_keyword": "w", "has_links": False}}

    class _FakeParseArticle:
        def parse(self, *a, **k):
            return {"title": "T", "body": "injected body text"}

    published = {"title": "T", "body": "injected body text", "analysis": {}, "summary": "T (1 words, easy)"}

    class _FakeRenderArticle:
        def render(self, *a, **k):
            return {"published": published}

    w = WiringClass(
        parse_article=_FakeParseArticle(),
        analyze_body=_SpyAnalyzeBody(),
        render_article=_FakeRenderArticle(),
    )
    w.publish(raw="# T\ninjected body text")
    assert received["body"] == "injected body text"


# ---------------------------------------------------------------------------
# Data-flow: render receives title, body, and analysis from prior stages
# ---------------------------------------------------------------------------

def test_publish_render_receives_title_from_parse():
    received = {}

    class _FakeParseArticle:
        def parse(self, *a, **k):
            return {"title": "Routed Title", "body": "b"}

    class _FakeAnalyzeBody:
        def analyze(self, *a, **k):
            return {"analysis": {"word_count": 1, "reading_level": "easy", "top_keyword": "b", "has_links": False}}

    class _SpyRenderArticle:
        def render(self, *a, **k):
            received["title"] = k.get("title") or (a[0] if a else None)
            return {"published": {"title": "Routed Title", "body": "b", "analysis": {}, "summary": "Routed Title (1 words, easy)"}}

    w = WiringClass(
        parse_article=_FakeParseArticle(),
        analyze_body=_FakeAnalyzeBody(),
        render_article=_SpyRenderArticle(),
    )
    w.publish(raw="# Routed Title\nb")
    assert received["title"] == "Routed Title"


def test_publish_render_receives_analysis_from_analyze():
    received = {}
    expected_analysis = {"word_count": 7, "reading_level": "hard", "top_keyword": "foo", "has_links": True}

    class _FakeParseArticle:
        def parse(self, *a, **k):
            return {"title": "T", "body": "b"}

    class _FakeAnalyzeBody:
        def analyze(self, *a, **k):
            return {"analysis": expected_analysis}

    class _SpyRenderArticle:
        def render(self, *a, **k):
            received["analysis"] = k.get("analysis") or (a[2] if len(a) > 2 else None)
            return {"published": {"title": "T", "body": "b", "analysis": expected_analysis, "summary": "T (7 words, hard)"}}

    w = WiringClass(
        parse_article=_FakeParseArticle(),
        analyze_body=_FakeAnalyzeBody(),
        render_article=_SpyRenderArticle(),
    )
    w.publish(raw="# T\nb")
    assert received["analysis"] == expected_analysis


# ---------------------------------------------------------------------------
# Data-flow: parse receives the original raw input
# ---------------------------------------------------------------------------

def test_publish_parse_receives_raw_input():
    received = {}

    class _SpyParseArticle:
        def parse(self, *a, **k):
            received["raw"] = k.get("raw") or (a[0] if a else None)
            return {"title": "T", "body": "b"}

    class _FakeAnalyzeBody:
        def analyze(self, *a, **k):
            return {"analysis": {"word_count": 1, "reading_level": "easy", "top_keyword": "b", "has_links": False}}

    class _FakeRenderArticle:
        def render(self, *a, **k):
            return {"published": {"title": "T", "body": "b", "analysis": {}, "summary": "T (1 words, easy)"}}

    w = WiringClass(
        parse_article=_SpyParseArticle(),
        analyze_body=_FakeAnalyzeBody(),
        render_article=_FakeRenderArticle(),
    )
    raw_input = "# T\nb"
    w.publish(raw=raw_input)
    assert received["raw"] == raw_input


# ---------------------------------------------------------------------------
# Stage ordering: parse must run before analyze (body available)
# ---------------------------------------------------------------------------

def test_publish_stages_run_in_order_parse_before_analyze():
    call_log = []

    class _SpyParseArticle:
        def parse(self, *a, **k):
            call_log.append("parse")
            return {"title": "T", "body": "b"}

    class _SpyAnalyzeBody:
        def analyze(self, *a, **k):
            call_log.append("analyze")
            return {"analysis": {"word_count": 1, "reading_level": "easy", "top_keyword": "b", "has_links": False}}

    class _SpyRenderArticle:
        def render(self, *a, **k):
            call_log.append("render")
            return {"published": {"title": "T", "body": "b", "analysis": {}, "summary": "T (1 words, easy)"}}

    w = WiringClass(
        parse_article=_SpyParseArticle(),
        analyze_body=_SpyAnalyzeBody(),
        render_article=_SpyRenderArticle(),
    )
    w.publish(raw="# T\nb")
    assert call_log == ["parse", "analyze", "render"]


# ---------------------------------------------------------------------------
# Varied payloads: different titles/bodies propagate correctly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,body", [
    ("Short", "One sentence."),
    ("A Very Long Title With Many Words", "Multiple sentences. Each one counts. Third."),
    ("Unicode Title \u00e9\u00e0\u00fc", "Body with unicode \u4e2d\u6587 text."),
])
def test_publish_varied_titles_and_bodies_propagate(title, body):
    analysis = {"word_count": 3, "reading_level": "easy", "top_keyword": "a", "has_links": False}
    published = {"title": title, "body": body, "analysis": analysis, "summary": f"{title} (3 words, easy)"}

    class _FakeParseArticle:
        def parse(self, *a, **k):
            return {"title": title, "body": body}

    class _FakeAnalyzeBody:
        def analyze(self, *a, **k):
            return {"analysis": analysis}

    class _FakeRenderArticle:
        def render(self, *a, **k):
            return {"published": published}

    w = WiringClass(
        parse_article=_FakeParseArticle(),
        analyze_body=_FakeAnalyzeBody(),
        render_article=_FakeRenderArticle(),
    )
    result = w.publish(raw=f"# {title}\n{body}")
    assert result["title"] == title
    assert result["body"] == body
    assert result["analysis"] == analysis
    assert result["published"] == published


# ---------------------------------------------------------------------------
# has_links propagates through the pipeline
# ---------------------------------------------------------------------------

def test_publish_analysis_with_links_propagates():
    analysis_with_links = {"word_count": 5, "reading_level": "easy", "top_keyword": "http", "has_links": True}
    published = {"title": "T", "body": "b", "analysis": analysis_with_links, "summary": "T (5 words, easy)"}
    w = _make_wiring(analysis=analysis_with_links, published=published)
    result = w.publish(raw="# T\nb")
    assert result["analysis"]["has_links"] is True


def test_publish_analysis_without_links_propagates():
    analysis_no_links = {"word_count": 2, "reading_level": "easy", "top_keyword": "hi", "has_links": False}
    published = {"title": "T", "body": "b", "analysis": analysis_no_links, "summary": "T (2 words, easy)"}
    w = _make_wiring(analysis=analysis_no_links, published=published)
    result = w.publish(raw="# T\nb")
    assert result["analysis"]["has_links"] is False
