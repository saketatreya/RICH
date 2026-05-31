"""Normalize a string: strip whitespace, lowercase."""


def normalize(text: str) -> dict:
    """Return {"normalized": <string>} after stripping whitespace and lowercasing."""
    return {"normalized": text.strip().lower()}
