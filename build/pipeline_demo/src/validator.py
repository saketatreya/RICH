"""Validate a normalized string: non-empty, no special characters."""


def validate(text: str) -> dict:
    """Return {"valid": bool, "reason": str} after validation checks."""
    if not text:
        return {"valid": False, "reason": "String is empty"}

    # Only allow letters, digits, spaces
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789 ")
    for ch in text:
        if ch not in allowed:
            return {"valid": False, "reason": "String contains special characters"}

    return {"valid": True, "reason": "OK"}
