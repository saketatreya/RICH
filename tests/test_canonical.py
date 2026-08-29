"""The canonical JSON form is identity, so it gets pinned rather than assumed."""

import json

import pytest

from richbuild.canonical import canonical_json_bytes, canonical_json_text
import richbuild.coding as coding
import richbuild.scheduler as scheduler
import richbuild.store as store


def test_every_producer_shares_one_encoding():
    """Four modules once defined this independently; a digest is only an identity
    while they all agree byte for byte."""

    document = {"b": 1, "a": {"d": [3, 2], "c": "é"}}

    assert coding._canonical_json(document) == canonical_json_text(document)
    assert store._canonical_json(document) == canonical_json_text(document)
    assert scheduler._canonical_json(document) == canonical_json_bytes(document)


def test_the_form_is_sorted_tight_unescaped_and_newline_terminated():
    document = {"b": 1, "a": "é"}

    text = canonical_json_text(document)

    assert text == '{"a":"é","b":1}', "sorted keys, no spaces, no \\u escaping"
    assert canonical_json_bytes(document) == text.encode("utf-8") + b"\n"
    assert json.loads(text) == document


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_refused_rather_than_written_as_invalid_json(value):
    """json.dumps would emit bare NaN/Infinity, which is not JSON and which no
    strict parser reads back. The store's encoder used to allow exactly this."""

    with pytest.raises(ValueError):
        canonical_json_text({"amount": value})
    with pytest.raises(ValueError):
        canonical_json_bytes({"amount": value})


def test_bytes_are_the_text_plus_exactly_one_newline():
    """Adding or removing the trailing newline silently changes every digest."""

    for document in ({}, {"a": 1}, [1, 2], "text", 7):
        assert canonical_json_bytes(document) == (
            canonical_json_text(document).encode("utf-8") + b"\n"
        )
