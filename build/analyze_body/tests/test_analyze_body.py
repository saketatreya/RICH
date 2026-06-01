import pytest
from analyze_body import analyze


# --- Return structure ---

def test_analyze_returns_analysis_key():
    result = analyze("hello world")
    assert "analysis" in result


def test_analyze_analysis_contains_all_keys():
    analysis = analyze("hello world")["analysis"]
    for key in ("word_count", "reading_level", "top_keyword", "has_links"):
        assert key in analysis


# --- word_count ---

def test_analyze_word_count_multiple_words():
    assert analyze("hello world foo")["analysis"]["word_count"] == 3


def test_analyze_word_count_single_word():
    assert analyze("hello")["analysis"]["word_count"] == 1


def test_analyze_word_count_empty_body():
    assert analyze("")["analysis"]["word_count"] == 0


def test_analyze_word_count_multiple_spaces():
    # whitespace-separated means str.split() semantics
    assert analyze("hello  world")["analysis"]["word_count"] == 2


def test_analyze_word_count_is_int():
    assert isinstance(analyze("hello")["analysis"]["word_count"], int)


# --- reading_level ---

def test_analyze_reading_level_easy_short_sentences():
    # Two 3-word sentences, avg = 3 < 12 -> easy
    assert analyze("a b c. d e f.")["analysis"]["reading_level"] == "easy"


def test_analyze_reading_level_easy_no_period():
    # No period -> one sentence, 3 words, avg = 3 < 12 -> easy
    assert analyze("one two three")["analysis"]["reading_level"] == "easy"


def test_analyze_reading_level_hard_long_sentence():
    # No period -> one sentence with 12 words, avg = 12 >= 12 -> hard
    body = "one two three four five six seven eight nine ten eleven twelve"
    assert analyze(body)["analysis"]["reading_level"] == "hard"


def test_analyze_reading_level_boundary_eleven_words_is_easy():
    # 11 words, no period -> avg = 11 < 12 -> easy
    body = "one two three four five six seven eight nine ten eleven"
    assert analyze(body)["analysis"]["reading_level"] == "easy"


def test_analyze_reading_level_valid_value():
    assert analyze("some text")["analysis"]["reading_level"] in ("easy", "hard")


# --- top_keyword ---

def test_analyze_top_keyword_most_frequent():
    # "the" appears twice, all others once
    assert analyze("the cat sat on the mat")["analysis"]["top_keyword"] == "the"


def test_analyze_top_keyword_tie_breaks_alphabetically():
    # "bat" and "cat" each appear once; "bat" < "cat" alphabetically
    assert analyze("bat cat")["analysis"]["top_keyword"] == "bat"


def test_analyze_top_keyword_alphabetical_tie_with_higher_frequency():
    # "apple" and "zebra" each appear twice; "apple" < "zebra"
    assert analyze("apple zebra apple zebra")["analysis"]["top_keyword"] == "apple"


def test_analyze_top_keyword_case_insensitive():
    # "The" and "the" both lowercase to "the", total freq = 2 > cat's 1
    assert analyze("The the cat")["analysis"]["top_keyword"] == "the"


def test_analyze_top_keyword_single_word():
    assert analyze("hello")["analysis"]["top_keyword"] == "hello"


def test_analyze_top_keyword_is_lowercase():
    # result must be the lowercased form
    assert analyze("HELLO hello HELLO")["analysis"]["top_keyword"] == "hello"


def test_analyze_top_keyword_is_string():
    assert isinstance(analyze("hello world")["analysis"]["top_keyword"], str)


# --- has_links ---

def test_analyze_has_links_true_http():
    assert analyze("visit http://example.com")["analysis"]["has_links"] is True


def test_analyze_has_links_true_https():
    # https contains 'http' as a substring
    assert analyze("see https://secure.example.com")["analysis"]["has_links"] is True


def test_analyze_has_links_false_no_url():
    assert analyze("no links here at all")["analysis"]["has_links"] is False


def test_analyze_has_links_false_empty_body():
    assert analyze("")["analysis"]["has_links"] is False


def test_analyze_has_links_http_as_any_substring():
    # contract says 'http' appears anywhere — substring match, not token match
    assert analyze("httpfoo bar")["analysis"]["has_links"] is True


def test_analyze_has_links_is_bool():
    assert isinstance(analyze("hello")["analysis"]["has_links"], bool)


# --- Independence / integration ---

def test_analyze_word_count_and_has_links_independent():
    body = "hello world http://x.com"
    analysis = analyze(body)["analysis"]
    assert analysis["word_count"] == 3
    assert analysis["has_links"] is True


def test_analyze_no_links_easy_reading_level():
    body = "hello world"
    analysis = analyze(body)["analysis"]
    assert analysis["word_count"] == 2
    assert analysis["has_links"] is False
    assert analysis["reading_level"] == "easy"


def test_analyze_all_four_fields_populated_on_nontrivial_body():
    body = "The quick brown fox jumps. The fox is quick."
    analysis = analyze(body)["analysis"]
    assert isinstance(analysis["word_count"], int) and analysis["word_count"] > 0
    assert analysis["reading_level"] in ("easy", "hard")
    assert isinstance(analysis["top_keyword"], str) and len(analysis["top_keyword"]) > 0
    assert isinstance(analysis["has_links"], bool)


def test_analyze_top_keyword_unaffected_by_presence_of_links():
    # has_links and top_keyword are independent; url token should not skew keyword
    body = "cat cat http://x.com"
    analysis = analyze(body)["analysis"]
    assert analysis["top_keyword"] == "cat"
    assert analysis["has_links"] is True
