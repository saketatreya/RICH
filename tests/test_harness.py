"""test_harness.py — Live end-to-end test instrumentation for RICH.

§2 of live-test-spec.md. Wraps the three skills at the boundary WITHOUT modifying
build.py, skills.py, llm.py, node.py, or spec.md. Monitors every LLM call,
checks the firewall, collects parse failures, and archives build artifacts.
"""

import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Must be set before importing skills ────────────────────────────
if not os.environ.get("OPENROUTER_API_KEY"):
    print("STOP: OPENROUTER_API_KEY not set. Export it and re-run.", file=sys.stderr)
    sys.exit(1)
os.environ.setdefault("RICH_MODEL", "deepseek/deepseek-chat")

REPO_ROOT = Path(__file__).resolve().parent.parent   # tests/ -> repo root
TESTLOG = REPO_ROOT / "testlog"
BUILD_ARCHIVE = REPO_ROOT / "build_archive"

# ── Phase context ──────────────────────────────────────────────────

_current_phase = "T0"
_call_count = 0

def set_phase(name: str):
    """Set current test phase for logging."""
    global _current_phase
    _current_phase = name
    (TESTLOG / name / "parse_failures").mkdir(parents=True, exist_ok=True)


def phase_dir() -> Path:
    return TESTLOG / _current_phase


# ── Call logger ─────────────────────────────────────────────────────

class CallLog:
    """One logged skill call."""
    def __init__(self, skill: str, node_id: str, depth: int, attempt: int,
                 input_contract: dict, dep_contract_ids: list[str],
                 raw_response: str, parsed_ok: bool, parse_error: str,
                 result_summary: str, firewall_ok: bool, firewall_note: str):
        self.skill = skill
        self.node_id = node_id
        self.depth = depth
        self.attempt = attempt
        self.input_contract = input_contract
        self.dep_contract_ids = dep_contract_ids
        self.raw_response = raw_response
        self.parsed_ok = parsed_ok
        self.parse_error = parse_error
        self.result_summary = result_summary
        self.firewall_ok = firewall_ok
        self.firewall_note = firewall_note
        self.ts = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "phase": _current_phase,
            "skill": self.skill,
            "node_id": self.node_id,
            "depth": self.depth,
            "attempt": self.attempt,
            "input_contract": self.input_contract,
            "dep_contract_ids": self.dep_contract_ids,
            "raw_response_excerpt": self.raw_response[:2000] if self.raw_response else "",
            "parsed_ok": self.parsed_ok,
            "parse_error": self.parse_error,
            "result_summary": self.result_summary,
            "firewall_ok": self.firewall_ok,
            "firewall_note": self.firewall_note,
        }


_logs: list[CallLog] = []


def flush_logs():
    """Write all buffered logs to calls.jsonl."""
    p = phase_dir() / "calls.jsonl"
    if _logs:
        with open(p, "a") as f:
            for log in _logs:
                f.write(json.dumps(log.to_dict(), default=str) + "\n")
    _logs.clear()


def _check_firewall(skill: str, system_prompt: str, user_prompt: str,
                    dep_contract_ids: list[str]) -> tuple[bool, str]:
    """Check D5: no dependency source in IMPLEMENT prompts."""
    if skill != "IMPLEMENT":
        return True, ""
    if not dep_contract_ids:
        return True, ""

    combined = (system_prompt or "") + (user_prompt or "")
    # Heuristic: if any dep id appears in a context suggesting source code,
    # flag it. We look for 'import <dep_id>' or 'from <dep_id>' or the
    # word 'source' near the dep id.
    import re
    for dep_id in dep_contract_ids:
        patterns = [
            rf"import\s+{dep_id}\b",
            rf"from\s+{dep_id}\b",
            rf"{dep_id}\.py\b",
        ]
        for pat in patterns:
            if re.search(pat, combined, re.IGNORECASE):
                return False, f"Firewall breach: dep '{dep_id}' source referenced in IMPLEMENT prompt"
    return True, ""


# ── Monkeypatch the skills ──────────────────────────────────────────

_orig_call_with_retry = None
_depth_tracker = 0
_attempt_tracker = 0
_node_tracker = ""


def _wrapped_call_with_retry(system_prompt, user_prompt, *,
                              model=None, temperature=0.1, max_tokens=4096,
                              json_mode=True, max_retries=3):
    """Intercept every LLM call for logging + firewall check."""
    global _call_count
    _call_count += 1

    # Determine which skill this is from the system prompt
    skill = "UNKNOWN"
    if "architect" in (system_prompt or "").lower() or "decompose" in (system_prompt or "").lower():
        skill = "PLAN"
    elif "code generator" in (system_prompt or "").lower():
        skill = "IMPLEMENT"
    elif "test generator" in (system_prompt or "").lower():
        skill = "DERIVE_TESTS"

    # Extract node_id from user prompt
    node_id = "unknown"
    import re
    m = re.search(r"id:\s*(\S+)", user_prompt or "")
    if m:
        node_id = m.group(1)

    # Extract dep_contract_ids from user prompt for firewall check
    dep_ids = []
    if "DEPENDENCY CONTRACTS" in (user_prompt or ""):
        dep_matches = re.findall(r"name:\s*(\S+)", user_prompt or "")
        dep_ids = [d for d in dep_matches if d not in ("none", "(none")]

    # Make the actual call
    raw_response = ""
    parsed_ok = False
    parse_error = ""
    result_summary = ""

    try:
        raw_response = _orig_call_with_retry(
            system_prompt=system_prompt, user_prompt=user_prompt,
            model=model, temperature=temperature, max_tokens=max_tokens,
            json_mode=json_mode, max_retries=max_retries,
        )
        # Try to parse for summary
        try:
            from llm import parse_json_response
            parsed = parse_json_response(raw_response, context="<logged>")
            parsed_ok = True
            if "is_leaf" in parsed:
                if parsed.get("is_leaf"):
                    result_summary = "is_leaf=true"
                else:
                    children = parsed.get("children", [])
                    result_summary = f"is_leaf=false, {len(children)} children: {[c.get('id','?') for c in children]}"
            elif "source" in parsed:
                result_summary = f"source {len(parsed['source'])} chars"
            elif "tests" in parsed:
                result_summary = f"tests {len(parsed['tests'])} chars"
            else:
                result_summary = f"keys: {list(parsed.keys())}"
        except Exception as e:
            parse_error = str(e)[:200]
            result_summary = f"parse failed: {parse_error[:80]}"
    except Exception as e:
        raw_response = str(e)[:2000]
        parse_error = str(e)[:200]
        result_summary = f"call failed: {str(e)[:80]}"

    # Firewall check
    firewall_ok, firewall_note = _check_firewall(skill, system_prompt, user_prompt, dep_ids)

    # Extract contract from user prompt
    input_contract = {}
    try:
        import yaml
        m2 = re.search(r"```yaml\n(.*?)```", user_prompt or "", re.DOTALL)
        if m2:
            input_contract = yaml.safe_load(m2.group(1)) or {}
    except Exception:
        pass

    # Log
    _logs.append(CallLog(
        skill=skill, node_id=node_id, depth=_depth_tracker,
        attempt=_attempt_tracker, input_contract=input_contract,
        dep_contract_ids=dep_ids, raw_response=raw_response,
        parsed_ok=parsed_ok, parse_error=parse_error,
        result_summary=result_summary, firewall_ok=firewall_ok,
        firewall_note=firewall_note,
    ))

    if not firewall_ok:
        print(f"  🔴 FIREWALL BREACH: {firewall_note}")

    return raw_response


_installed = False


def install():
    """Install the test harness — monkeypatches skills.py's LLM boundary."""
    global _orig_call_with_retry, _installed
    if _installed:
        return  # Already installed — don't re-wrap
    _installed = True

    import skills, llm

    # Save original
    _orig_call_with_retry = llm.call_with_retry

    # Patch BOTH modules — skills.py imports call_with_retry at module level
    wrapped = _wrapped_call_with_retry
    llm.call_with_retry = wrapped
    skills.call_with_retry = wrapped

    # Also patch parse_json_response to collect failures
    _orig_parse = llm.parse_json_response

    def _wrapped_parse(raw, context=""):
        try:
            return _orig_parse(raw, context)
        except llm.LLMParseError as e:
            src = Path(e.dump_path)
            if src.exists():
                dest = phase_dir() / "parse_failures" / src.name
                shutil.copy2(src, dest)
            raise

    llm.parse_json_response = _wrapped_parse
    skills.parse_json_response = _wrapped_parse

    print(f"  [harness] Installed. Phase: {_current_phase}, Model: {llm.RICH_MODEL}")


def uninstall():
    """Restore original functions."""
    import llm, skills
    if _orig_call_with_retry:
        llm.call_with_retry = _orig_call_with_retry


def archive_build():
    """Move build/ to build_archive/<phase>/ and create fresh build/."""
    src = REPO_ROOT / "build"
    if src.exists():
        dest = BUILD_ARCHIVE / _current_phase / "build"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(src), str(dest))
    (REPO_ROOT / "build").mkdir(exist_ok=True)


def get_stats() -> dict:
    """Return call stats for current phase."""
    return {
        "phase": _current_phase,
        "total_calls": _call_count,
        "parse_failures": sum(1 for l in _logs if not l.parsed_ok and l.parse_error),
        "firewall_breaches": sum(1 for l in _logs if not l.firewall_ok),
    }


def reset_stats():
    """Reset call counter for new phase."""
    global _call_count
    _call_count = 0
    _logs.clear()


# ── Phase runner ────────────────────────────────────────────────────

def run_phase(name: str, fn, *args, **kwargs):
    """Run a test phase with proper setup/teardown."""
    global _current_phase, _call_count
    _current_phase = name
    _call_count = 0
    _logs.clear()

    set_phase(name)
    archive_build()
    flush_logs()

    print(f"\n{'='*60}")
    print(f"{name}: Starting")
    print(f"{'='*60}")

    t0 = time.time()
    try:
        result = fn(*args, **kwargs)
        elapsed = time.time() - t0
        flush_logs()

        if isinstance(result, dict) and result.get("status") == "PASS":
            print(f"\n  ✓ {name} PASSED ({elapsed:.1f}s, {_call_count} LLM calls)")
        elif isinstance(result, dict):
            print(f"\n  {result.get('status', '?')} {name} ({elapsed:.1f}s, {_call_count} LLM calls)")
        else:
            print(f"\n  ✓ {name} complete ({elapsed:.1f}s, {_call_count} LLM calls)")
        return result
    except Exception as e:
        elapsed = time.time() - t0
        flush_logs()
        print(f"\n  ✗ {name} FAILED with exception ({elapsed:.1f}s, {_call_count} LLM calls)")
        print(f"  {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "FAIL", "exception": str(e), "elapsed": elapsed, "calls": _call_count}


def finish_phase(name: str):
    """Flush logs for current phase."""
    flush_logs()
    print(f"  [harness] {name}: logs flushed to {phase_dir()}")
