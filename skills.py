"""LLM skills — PLAN, IMPLEMENT, DERIVE_TESTS.

In M-A, all skills return hardcoded (canned) results for ONE chosen pipeline goal:
"normalize then validate a string" — a pipeline of normalizer → validator.

M-A acceptance: build(root_contract) creates full tree on disk, runs the (real)
canned tests, marks nodes verified. Zero LLM calls.
"""

import json
from node import Node


def plan(contract: dict) -> dict:
    """PLAN(contract) → decision.

    M-A: Hardcoded decomposition for the pipeline_demo root.
    Returns {"is_leaf": true} for any leaf contract.
    Returns {"is_leaf": false, ...} with hardcoded children for the root.
    """
    node_id = contract["id"]

    # Root: pipeline_demo — decompose into normalizer + validator
    if node_id == "pipeline_demo":
        return {
            "is_leaf": False,
            "children": [
                {
                    "id": "normalizer",
                    "description": "Normalize a string: strip whitespace, lowercase",
                    "interface": {
                        "operations": [
                            {
                                "name": "normalize",
                                "inputs": {"text": "string"},
                                "outputs": {"normalized": "string"},
                                "errors": [],
                            }
                        ]
                    },
                    "dependencies": [],
                    "behavior": [
                        {
                            "id": "strip_and_lower",
                            "prose": "Normalized text must have no leading/trailing whitespace and be lowercase",
                        }
                    ],
                },
                {
                    "id": "validator",
                    "description": "Validate a normalized string: non-empty, no special characters",
                    "interface": {
                        "operations": [
                            {
                                "name": "validate",
                                "inputs": {"text": "string"},
                                "outputs": {"valid": "bool", "reason": "string"},
                                "errors": [],
                            }
                        ]
                    },
                    "dependencies": [],
                    "behavior": [
                        {
                            "id": "non_empty",
                            "prose": "Empty strings are invalid",
                        },
                        {
                            "id": "no_special_chars",
                            "prose": "Strings with special characters other than letters/digits/spaces are invalid",
                        },
                    ],
                },
            ],
            "edges": [
                {
                    "from": "normalizer",
                    "to": "validator",
                    "name": "normalized",
                }
            ],
        }

    # Any other contract: leaf
    return {"is_leaf": True}


def implement(contract: dict, dep_contracts: dict | None = None, pipeline: bool = False) -> str:
    """IMPLEMENT(contract, dep_contracts, pipeline) → source code.

    M-A: Hardcoded implementations for normalizer, validator, and pipeline_demo.
    """
    dep_contracts = dep_contracts or {}
    node_id = contract["id"]

    if node_id == "normalizer":
        return '''"""Normalize a string: strip whitespace, lowercase."""


def normalize(text: str) -> dict:
    """Return {"normalized": <string>} after stripping whitespace and lowercasing."""
    return {"normalized": text.strip().lower()}
'''

    elif node_id == "validator":
        return '''"""Validate a normalized string: non-empty, no special characters."""


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
'''

    elif node_id == "pipeline_demo":
        return '''"""Pipeline demo: normalize → validate.

Receives normalizer + validator as injected dependencies.
"""


class PipelineDemo:
    def __init__(self, normalizer, validator):
        self.normalizer = normalizer
        self.validator = validator

    def run(self, text: str) -> dict:
        """Run the pipeline: normalize then validate."""
        norm_result = self.normalizer.normalize(text)
        val_result = self.validator.validate(norm_result["normalized"])
        return {
            "original": text,
            "normalized": norm_result["normalized"],
            "valid": val_result["valid"],
            "reason": val_result["reason"],
        }
'''

    return ""


def derive_tests(contract: dict) -> str:
    """DERIVE_TESTS(contract) → pytest source.

    M-A: Hardcoded test files for normalizer, validator, and pipeline_demo.
    """
    node_id = contract["id"]

    if node_id == "normalizer":
        return '''"""Tests for normalizer — derived from contract."""

from normalizer import normalize


def test_normalize_strips_whitespace():
    result = normalize("  hello  ")
    assert result["normalized"] == "hello"


def test_normalize_lowercases():
    result = normalize("HELLO")
    assert result["normalized"] == "hello"


def test_normalize_empty_string():
    result = normalize("")
    assert result["normalized"] == ""


def test_normalize_already_clean():
    result = normalize("hello world")
    assert result["normalized"] == "hello world"
'''

    elif node_id == "validator":
        return '''"""Tests for validator — derived from contract."""

from validator import validate


def test_validate_normal_string():
    result = validate("hello world")
    assert result["valid"] is True
    assert result["reason"] == "OK"


def test_validate_empty_string():
    result = validate("")
    assert result["valid"] is False
    assert result["reason"] == "String is empty"


def test_validate_special_chars():
    result = validate("hello@world")
    assert result["valid"] is False
    assert result["reason"] == "String contains special characters"


def test_validate_numeric():
    result = validate("hello 123")
    assert result["valid"] is True
'''

    elif node_id == "pipeline_demo":
        return '''"""Tests for pipeline_demo — integration-level, derived from root contract."""

from pipeline_demo import PipelineDemo


def test_pipeline_happy_path():
    demo = PipelineDemo(normalizer=None, validator=None)
    # We test the wiring logic by mocking deps inline
    class FakeNormalizer:
        def normalize(self, text):
            return {"normalized": text.strip().lower()}

    class FakeValidator:
        def validate(self, text):
            if not text:
                return {"valid": False, "reason": "empty"}
            return {"valid": True, "reason": "OK"}

    demo.normalizer = FakeNormalizer()
    demo.validator = FakeValidator()
    result = demo.run("  Hello World  ")
    assert result["original"] == "  Hello World  "
    assert result["normalized"] == "hello world"
    assert result["valid"] is True
'''

    return ""
