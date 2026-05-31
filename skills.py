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


# ── PLAN (real LLM from M-D, decomposition from M-E) ───────────────

PLAN_SYSTEM_LEAF_ONLY = """You are an architect for a recursive agent build system called RICH.
Your job: given a module CONTRACT, decide if it can be implemented directly as a leaf module.

CRITICAL RESTRICTION: You may ONLY return {"is_leaf": true}. Decomposition is disabled.
Do NOT return children or edges under any circumstances. If you think the module
should be decomposed, return {"is_leaf": true} anyway — this is a leaf-only mode.

Output format: a JSON object with a single key "is_leaf" set to true.
Example: {"is_leaf": true}"""

PLAN_SYSTEM_DECOMPOSE = """You are an architect for a recursive agent build system called RICH.
Your job: given a ROOT CONTRACT, decompose it into child modules if appropriate,
or decide it's simple enough to implement directly as a leaf.

DECISION RULES:
- If the contract is simple (1-2 ops, no complex logic), return leaf.
- If the contract describes a multi-step workflow or pipeline, decompose into children.
- Each child gets its OWN contract that YOU author — the child must be independently implementable.
- Children must form a DAG (no cycles). Edges declare which child depends on which.
- DEPTH LIMIT: max 2 levels (root → children). Children must be leaves.
- Choose clean, descriptive child ids (lowercase, underscore-separated).
- Each child's contract must include: id, description, interface (operations with typed inputs/outputs), dependencies, behavior (prose).

OUTPUT FORMAT (JSON):
For leaf: {"is_leaf": true}
For decompose: {
  "is_leaf": false,
  "children": [
    {
      "id": "<child_id>",
      "description": "<what it does>",
      "interface": {"operations": [{"name": "<op>", "inputs": {...}, "outputs": {...}, "errors": []}]},
      "dependencies": [],
      "behavior": [{"id": "<prop_id>", "prose": "<what must hold>"}]
    }
  ],
  "edges": [{"from": "<child_id>", "to": "<child_id>", "name": "<inject_param_name>"}]
}"""


def plan_canned(contract: dict) -> dict:
    """Always-returns-canned PLAN for the pipeline demo."""
    node_id = contract["id"]
    if node_id == "pipeline_demo":
        return CANNED_PIPELINE_DEMO_DECISION
    if node_id == "email_checker":
        return CANNED_FAN_IN_DECISION
    return {"is_leaf": True}


def plan(contract: dict, allow_decompose: bool = False) -> dict:
    """PLAN(contract) → decision.

    M-A/M-B/M-C: Canned decomposition for pipeline_demo; is_leaf:true for others.
    M-D: Real LLM call restricted to is_leaf:true.
    M-E: allow_decompose=True enables full decomposition (depth 1, pipeline-only).
    """
    node_id = contract["id"]

    # Canned: pipeline_demo always uses canned decomposition
    if node_id == "pipeline_demo":
        return CANNED_PIPELINE_DEMO_DECISION

    # Try real LLM PLAN if available
    if is_available():
        system = PLAN_SYSTEM_DECOMPOSE if allow_decompose else PLAN_SYSTEM_LEAF_ONLY
        contract_yaml = yaml.dump(contract, default_flow_style=False, sort_keys=False)
        user_prompt = f"""CONTRACT:
```yaml
{contract_yaml}
```

Decide: leaf or decompose?"""

        try:
            raw = call_with_retry(
                system_prompt=system,
                user_prompt=user_prompt,
                temperature=0.15 if allow_decompose else 0.05,
                max_tokens=2048 if allow_decompose else 256,
            )
            decision = parse_json_response(raw, context=f"PLAN({node_id})")

            if not allow_decompose:
                return {"is_leaf": True}

            # Validate decomposition
            if not decision.get("is_leaf", True):
                children = decision.get("children", [])
                edges = decision.get("edges", [])
                # Validate DAG (no cycles)
                _validate_dag(children, edges, node_id)
                # Validate child contracts have required fields
                _validate_child_contracts(children, node_id)
                return decision

            return {"is_leaf": True}

        except (LLMNotConfigured, LLMParseError) as e:
            print(f"  [plan] LLM failed for {node_id}, falling back to leaf: {e}")
        except ValueError as e:
            print(f"  [plan] Validation failed for {node_id}: {e}")
            # Fall back to leaf on validation failure
            return {"is_leaf": True}

    # Fallback: everything is a leaf
    return {"is_leaf": True}


def _validate_dag(children: list[dict], edges: list[dict], parent_id: str):
    """Validate that children+edges form a DAG (no cycles)."""
    child_ids = {c["id"] for c in children}
    # Build adjacency
    dep_of = {cid: set() for cid in child_ids}
    for edge in edges:
        frm = edge.get("from", "")
        to = edge.get("to", "")
        if frm not in child_ids:
            raise ValueError(f"Edge references unknown child '{frm}' in {parent_id}")
        if to not in child_ids:
            raise ValueError(f"Edge references unknown child '{to}' in {parent_id}")
        dep_of[to].add(frm)

    # Detect cycles via DFS
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {cid: WHITE for cid in child_ids}

    def dfs(cid):
        color[cid] = GRAY
        for dep in dep_of.get(cid, set()):
            if color[dep] == GRAY:
                raise ValueError(f"Cycle detected: {cid} → {dep} in decomposition of {parent_id}")
            if color[dep] == WHITE:
                dfs(dep)
        color[cid] = BLACK

    for cid in child_ids:
        if color[cid] == WHITE:
            dfs(cid)


def _validate_child_contracts(children: list[dict], parent_id: str):
    """Validate each child contract has required fields."""
    required = ["id", "description", "interface"]
    for child in children:
        for field in required:
            if field not in child:
                raise ValueError(f"Child contract missing '{field}' in decomposition of {parent_id}")
        iface = child.get("interface", {})
        ops = iface.get("operations", [])
        if not ops:
            raise ValueError(f"Child '{child['id']}' has no operations in decomposition of {parent_id}")


# ── IMPLEMENT (real LLM from M-C) ──────────────────────────────────

IMPLEMENT_SYSTEM = """You are a code generator for a recursive agent build system called RICH.
Your job: given a module CONTRACT, produce the Python source code that satisfies it.

RULES:
1. Return ONLY valid Python source code. No markdown fences, no prose.
2. CRITICAL: Export each operation as a TOP-LEVEL FUNCTION matching the operation name exactly.
   Do NOT wrap operations in classes. The test harness imports functions directly.
   Example: def normalize(text: str) -> dict:
                return {"normalized": text.strip().lower()}
3. Each operation returns a dict matching its declared outputs.
4. Dependencies arrive as injected constructor/factory parameters — NEVER import them.
   PIPELINE MODE ONLY (not leaf): use a class with __init__ receiving deps.
   Example: class MyPipeline:
                def __init__(self, dep_a, dep_b):
                    self.dep_a = dep_a
                    self.dep_b = dep_b
                def run(self, text):
                    r1 = self.dep_a.op(text)
                    r2 = self.dep_b.op(r1["output_key"])
                    return {"result": r2["output_key"]}
5. PIPELINE MODE: You MUST compose the injected dependencies sequentially.
   Call dep1's operation, pass its output dict to dep2, etc. Do NOT reimplement.
6. LEAF MODE: Export plain functions. No classes needed.
7. If you receive failure output from a prior attempt, fix the bugs.

Output format: a JSON object with a single key "source" containing the Python code as a string."""


def implement_canned(contract: dict, dep_contracts=None, pipeline=False, prior_failures=None) -> str:
    """Always-returns-canned IMPLEMENT."""
    node_id = contract["id"]
    if node_id in CANNED_IMPLS:
        return CANNED_IMPLS[node_id]
    return ""


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
4. CRITICAL: Operations return a DICT with keys matching the declared outputs. Extract the right key before asserting. Example: result = op(args); assert result["output_key"] == expected
5. For each operation: test normal inputs, edge cases, and declared error conditions.
6. Tests are consumer-driven — they encode what the consumer needs from this module.
7. Use descriptive test names: test_<op>_<scenario>.

Output format: a JSON object with a single key "tests" containing the pytest code as a string."""


def derive_tests_canned(contract: dict) -> str:
    """Always-returns-canned DERIVE_TESTS."""
    node_id = contract["id"]
    if node_id in CANNED_TESTS:
        return CANNED_TESTS[node_id]
    return ""


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


# M-F: Fan-in demo — two children share one dependency
CANNED_FAN_IN_DECISION = {
    "is_leaf": False,
    "children": [
        {
            "id": "regex_engine",
            "description": "Provide regex pattern matching: check if a string matches a given pattern",
            "interface": {
                "operations": [
                    {
                        "name": "matches",
                        "inputs": {"text": "string", "pattern": "string"},
                        "outputs": {"ok": "bool", "match": "string"},
                        "errors": [],
                    }
                ]
            },
            "dependencies": [],
            "behavior": [
                {"id": "match_or_not", "prose": "Returns ok=true and the matched text if pattern matches, ok=false and empty match otherwise"},
            ],
        },
        {
            "id": "format_checker",
            "description": "Check if an email has valid format (contains @, has domain part)",
            "interface": {
                "operations": [
                    {
                        "name": "check_format",
                        "inputs": {"email": "string"},
                        "outputs": {"valid": "bool"},
                        "errors": [],
                    }
                ]
            },
            "dependencies": [{"name": "regex", "id": "regex_engine"}],
            "behavior": [
                {"id": "has_at", "prose": "Email must contain exactly one @ sign"},
                {"id": "has_domain", "prose": "Domain part after @ must be non-empty"},
            ],
        },
        {
            "id": "domain_checker",
            "description": "Check if email domain is a common provider (gmail, yahoo, outlook)",
            "interface": {
                "operations": [
                    {
                        "name": "is_common",
                        "inputs": {"email": "string"},
                        "outputs": {"common": "bool", "domain": "string"},
                        "errors": [],
                    }
                ]
            },
            "dependencies": [{"name": "regex", "id": "regex_engine"}],
            "behavior": [
                {"id": "common_providers", "prose": "Returns common=true for gmail.com, yahoo.com, outlook.com domains"},
            ],
        },
    ],
    "edges": [
        {"from": "regex_engine", "to": "format_checker", "name": "regex"},
        {"from": "regex_engine", "to": "domain_checker", "name": "regex"},
    ],
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
    # M-F: Fan-in canned implementations
    "regex_engine": '''"""Regex engine: pattern matching via Python's re module."""

import re


def matches(text: str, pattern: str) -> dict:
    """Check if text matches pattern. Returns ok=True and the match if found."""
    m = re.search(pattern, text)
    if m:
        return {"ok": True, "match": m.group()}
    return {"ok": False, "match": ""}
''',
    "format_checker": '''"""Format checker: validate email format using injected regex engine."""


class FormatChecker:
    def __init__(self, regex):
        self.regex = regex

    def check_format(self, email: str) -> dict:
        """Check email has valid format: contains @, has domain part."""
        result = self.regex.matches(email, r"^[^@]+@[^@]+\\.[^@]+$")
        return {"valid": result["ok"]}
''',
    "domain_checker": '''"""Domain checker: check if email domain is common using injected regex engine."""


class DomainChecker:
    def __init__(self, regex):
        self.regex = regex

    def is_common(self, email: str) -> dict:
        """Check if email domain is gmail/yahoo/outlook."""
        result = self.regex.matches(email, r"@(gmail|yahoo|outlook)\\.")
        if result["ok"]:
            return {"common": True, "domain": result["match"].lstrip("@").rstrip(".")}
        return {"common": False, "domain": ""}
''',
    "email_checker": '''"""Email checker: compose format_checker and domain_checker over shared regex_engine."""


class EmailChecker:
    def __init__(self, regex_engine, format_checker, domain_checker):
        self.regex_engine = regex_engine
        self.format_checker = format_checker
        self.domain_checker = domain_checker

    def check(self, email: str) -> dict:
        """Check email format and domain. Both checkers share the same regex engine."""
        fmt = self.format_checker.check_format(email)
        dom = self.domain_checker.is_common(email)
        return {
            "email": email,
            "valid_format": fmt["valid"],
            "common_domain": dom["common"],
            "domain": dom["domain"],
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
    "regex_engine": '''"""Tests for regex_engine."""

from regex_engine import matches


def test_matches_found():
    result = matches(text="hello world", pattern=r"hello")
    assert result["ok"] is True
    assert result["match"] == "hello"


def test_matches_not_found():
    result = matches(text="hello world", pattern=r"xyz")
    assert result["ok"] is False
    assert result["match"] == ""


def test_matches_email():
    result = matches(text="user@gmail.com", pattern=r"@gmail\\.")
    assert result["ok"] is True
''',
    "format_checker": '''"""Tests for format_checker — uses fake regex engine."""

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
''',
    "domain_checker": '''"""Tests for domain_checker — uses fake regex engine."""

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
''',
    "email_checker": '''"""Tests for email_checker — integration with shared regex_engine."""

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
''',
}
