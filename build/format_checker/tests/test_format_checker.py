"""Tests for format_checker — uses fake regex engine."""

from format_checker import FormatChecker


class FakeRegex:
    def matches(self, text, pattern):
        if "@" in text and "." in text.split("@")[-1]:
            return {"ok": True, "match": text}
        return {"ok": False, "match": ""}


def test_valid_email():
    checker = FormatChecker(FakeRegex())
    result = checker.check_format("user@gmail.com")
    assert result["valid"] is True


def test_invalid_email_no_at():
    checker = FormatChecker(FakeRegex())
    result = checker.check_format("usergmail.com")
    assert result["valid"] is False
