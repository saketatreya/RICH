"""Regex engine: pattern matching via Python's re module."""

import re


def matches(text: str, pattern: str) -> dict:
    """Check if text matches pattern. Returns ok=True and the match if found."""
    m = re.search(pattern, text)
    if m:
        return {"ok": True, "match": m.group()}
    return {"ok": False, "match": ""}
