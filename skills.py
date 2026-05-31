"""LLM skills — PLAN, IMPLEMENT, DERIVE_TESTS.

M-A/M-B: All skills return hardcoded (canned) results.
M-C onward: IMPLEMENT and DERIVE_TESTS become real LLM calls.
PLAN stays stubbed (only is_leaf:true) until M-D.
"""

import json
import yaml
from llm import (
    call_with_retry,
    parse_json_response,
    is_available,
    LLMNotConfigured,
    LLMParseError,
)


# ── PLAN (real LLM from M-D, leaf-only) ────────────────────────────

PLAN_SYSTEM_LEAF_ONLY = """You are an architect for a recursive agent build system called RICH.
Your job: given a module CONTRACT, decide if it can be implemented directly as a leaf module.

CRITICAL RESTRICTION: You may ONLY return {"is_leaf": true}. Decomposition is disabled.
Do NOT return children or edges under any circumstances. If you think the module
should be decomposed, return {"is_leaf": true} anyway — this is a leaf-only mode.

Output format: a JSON object with a single key "is_leaf" set to true.
Example: {"is_leaf": true}"""


def plan(contract: dict) -> dict:
    """PLAN(contract) → decision.

    M-A/M-B/M-C: Canned decomposition for pipeline_demo; is_leaf:true for others.
    M-D: Real LLM call restricted to is_leaf:true.
    M-E: Full decomposition enabled.
    """
    node_id = contract["id"]

    # Canned: pipeline_demo decomposes into normalizer + validator
    if node_id == "pipeline_demo":
        return CANNED_PIPELINE_DEMO_DECISION

    # Try real LLM PLAN if available
    if is_available():
        contract_yaml = yaml.dump(contract, default_flow_style=False, sort_keys=False)
        user_prompt = f"""CONTRACT:
```yaml
{contract_yaml}
```

Can this module be implemented directly as a leaf?"""

        try:
            raw = call_with_retry(
                system_prompt=PLAN_SYSTEM_LEAF_ONLY,
                user_prompt=user_prompt,
                temperature=0.05,
                max_tokens=256,
            )
            decision = parse_json_response(raw, context=f"PLAN({node_id})")
            # Force leaf-only regardless of what LLM returns
            return {"is_leaf": True}
        except (LLMNotConfigured, LLMParseError) as e:
            print(f"  [plan] LLM failed for {node_id}, falling back to leaf: {e}")

    # Fallback: everything is a leaf
    return {"is_leaf": True}


# ── IMPLEMENT (real LLM from M-C) ──────────────────────────────────

IMPLEMENT_SYSTEM = """You are a code generator for a recursive agent build system called RICH.
Your job: given a module CONTRACT, produce the Python source code that satisfies it.

RULES:
1. Return ONLY valid Python source code. No markdown fences, no prose.
2. The module must expose every operation named in contract.interface.operations with matching signatures.
3. Each operation returns a dict matching its declared outputs.
4. Dependencies arrive as injected constructor/factory parameters — NEVER import them.
5. If dep_contracts is non-empty, receive each dependency as a named parameter.
6. For pipeline nodes (pipeline=True), compose the dependencies as a sequential pipeline.
7. If you receive failure output from a prior attempt, fix the bugs.

Output format: a JSON object with a single key "source" containing the Python code as a string."""


def implement(contract: dict, dep_contracts: dict | None = None,
              pipeline: bool = False, prior_failures: list[str] | None = None) -> str:
    """IMPLEMENT(contract, dep_contracts, pipeline) → source code.

    M-A/M-B: Canned implementations for the pipeline demo.
    M-C onward: Real LLM call via OpenRouter. Falls back to canned if no API key.
    """
    dep_contracts = dep_contracts or {}
    node_id = contract["id"]

    # Fall back to canned for the pipeline demo
    if not is_available() and node_id in CANNED_IMPLS:
        return CANNED_IMPLS[node_id]

    if not is_available():
        raise LLMNotConfigured(
            "No API key available for IMPLEMENT. "
            "Set OPENROUTER_API_KEY or provide canned implementations."
        )

    # Build the prompt
    contract_yaml = yaml.dump(contract, default_flow_style=False, sort_keys=False)
    dep_yaml = ""
    if dep_contracts:
        dep_yaml = yaml.dump(dep_contracts, default_flow_style=False, sort_keys=False)

    user_prompt = f"""CONTRACT:
```yaml
{contract_yaml}
```

DEPENDENCY CONTRACTS (interfaces only, never source):
```yaml
{dep_yaml if dep_yaml else "(none — this is a leaf module with no dependencies)"}
```

PIPELINE MODE: {pipeline}
"""

    if prior_failures:
        user_prompt += f"""
PRIOR ATTEMPT FAILURES (fix these):
{chr(10).join(prior_failures)}
"""

    try:
        raw = call_with_retry(
            system_prompt=IMPLEMENT_SYSTEM,
            user_prompt=user_prompt,
            temperature=0.1,
            max_tokens=4096,
        )
        result = parse_json_response(raw, context=f"IMPLEMENT({node_id})")
        return result["source"]
    except (LLMNotConfigured, LLMParseError) as e:
        # If LLM fails and we have a canned fallback, use it
        if node_id in CANNED_IMPLS:
            print(f"  [implement] LLM failed for {node_id}, using canned fallback: {e}")
            return CANNED_IMPLS[node_id]
        raise


# ── DERIVE_TESTS (real LLM from M-C) ────────────────────────────────

DERIVE_TESTS_SYSTEM = """You are a test generator for a recursive agent build system called RICH.
Your job: given a module CONTRACT, produce a pytest test file that verifies the implementation.

RULES:
1. Return ONLY valid Python pytest source. No markdown fences, no prose.
2. Import the module by name (from <module_id> import <op_name>).
3. Test every operation in contract.interface.operations.
4. For each operation: test normal inputs, edge cases, and declared error conditions.
5. Tests are consumer-driven — they encode what the consumer needs from this module.
6. Use descriptive test names: test_<op>_<scenario>.

Output format: a JSON object with a single key "tests" containing the pytest code as a string."""


def derive_tests(contract: dict) -> str:
    """DERIVE_TESTS(contract) → pytest source.

    M-A/M-B: Canned test files for the pipeline demo.
    M-C onward: Real LLM call via OpenRouter. Falls back to canned if no API key.
    """
    node_id = contract["id"]

    if not is_available() and node_id in CANNED_TESTS:
        return CANNED_TESTS[node_id]

    if not is_available():
        raise LLMNotConfigured(
            "No API key available for DERIVE_TESTS. "
            "Set OPENROUTER_API_KEY or provide canned tests."
        )

    contract_yaml = yaml.dump(contract, default_flow_style=False, sort_keys=False)

    user_prompt = f"""CONTRACT:
```yaml
{contract_yaml}
```

Generate a pytest file that imports from '{node_id}' and tests all operations."""

    try:
        raw = call_with_retry(
            system_prompt=DERIVE_TESTS_SYSTEM,
            user_prompt=user_prompt,
            temperature=0.1,
            max_tokens=4096,
        )
        result = parse_json_response(raw, context=f"DERIVE_TESTS({node_id})")
        return result["tests"]
    except (LLMNotConfigured, LLMParseError) as e:
        if node_id in CANNED_TESTS:
            print(f"  [derive_tests] LLM failed for {node_id}, using canned fallback: {e}")
            return CANNED_TESTS[node_id]
        raise


# ═════════════════════════════════════════════════════════════════════
# Canned data (M-A/M-B fallback for pipeline demo)
# ═════════════════════════════════════════════════════════════════════

CANNED_PIPELINE_DEMO_DECISION = {
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
                {"id": "non_empty", "prose": "Empty strings are invalid"},
                {"id": "no_special_chars", "prose": "Strings with special characters other than letters/digits/spaces are invalid"},
            ],
        },
    ],
    "edges": [{"from": "normalizer", "to": "validator", "name": "normalized"}],
}

CANNED_IMPLS = {
    "normalizer": '''"""Normalize a string: strip whitespace, lowercase."""


def normalize(text: str) -> dict:
    """Return {"normalized": <string>} after stripping whitespace and lowercasing."""
    return {"normalized": text.strip().lower()}
''',
    "validator": '''"""Validate a normalized string: non-empty, no special characters."""


def validate(text: str) -> dict:
    """Return {"valid": bool, "reason": str} after validation checks."""
    if not text:
        return {"valid": False, "reason": "String is empty"}
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789 ")
    for ch in text:
        if ch not in allowed:
            return {"valid": False, "reason": "String contains special characters"}
    return {"valid": True, "reason": "OK"}
''',
    "pipeline_demo": '''"""Pipeline demo: normalize → validate.

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
''',
}

CANNED_TESTS = {
    "normalizer": '''"""Tests for normalizer — derived from contract."""

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
''',
    "validator": '''"""Tests for validator — derived from contract."""

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
''',
    "pipeline_demo": '''"""Tests for pipeline_demo — integration-level."""

from pipeline_demo import PipelineDemo


class FakeNormalizer:
    def normalize(self, text):
        return {"normalized": text.strip().lower()}


class FakeValidator:
    def validate(self, text):
        if not text:
            return {"valid": False, "reason": "empty"}
        return {"valid": True, "reason": "OK"}


def test_pipeline_happy_path():
    demo = PipelineDemo(FakeNormalizer(), FakeValidator())
    result = demo.run("  Hello World  ")
    assert result["original"] == "  Hello World  "
    assert result["normalized"] == "hello world"
    assert result["valid"] is True
''',
}
