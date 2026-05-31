"""Tests for domain_checker — uses fake regex engine."""

from domain_checker import DomainChecker


class FakeRegex:
    def matches(self, text, pattern):
        if "gmail" in text:
            return {"ok": True, "match": "@gmail."}
        return {"ok": False, "match": ""}


def test_common_domain():
    checker = DomainChecker(FakeRegex())
    result = checker.is_common("user@gmail.com")
    assert result["common"] is True
    assert result["domain"] == "gmail"


def test_uncommon_domain():
    checker = DomainChecker(FakeRegex())
    result = checker.is_common("user@company.com")
    assert result["common"] is False
