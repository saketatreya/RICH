"""Tests for email_checker — integration with shared regex_engine."""

from email_checker import EmailChecker
from regex_engine import matches as real_matches


class RealRegex:
    def matches(self, text, pattern):
        return real_matches(text, pattern)


class FakeFormatChecker:
    def __init__(self, regex):
        self.regex = regex
    def check_format(self, email):
        r = self.regex.matches(email, r"@")
        return {"valid": r["ok"]}


class FakeDomainChecker:
    def __init__(self, regex):
        self.regex = regex
    def is_common(self, email):
        r = self.regex.matches(email, r"@gmail")
        return {"common": r["ok"], "domain": "gmail" if r["ok"] else ""}


def test_valid_gmail():
    regex = RealRegex()
    checker = EmailChecker(regex, FakeFormatChecker(regex), FakeDomainChecker(regex))
    result = checker.check("user@gmail.com")
    assert result["valid_format"] is True
    assert result["common_domain"] is True
