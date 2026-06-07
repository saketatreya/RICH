"""subagent_skill.py — Claude Code subagent backend for RICH's three skills.

A drop-in replacement for the OpenRouter model (llm.py). It exposes a
`call_with_retry()` with the SAME signature skills.py expects from the model,
implemented by spawning `claude -p` (headless Claude Code) as a bounded,
tool-less worker that takes a (system, user) prompt and returns raw text.

`install()` monkeypatches skills.py's model seam — call_with_retry, is_available,
parse_json_response — WITHOUT touching build.py, node.py, the recursion, or the
deterministic engine. This is the same boundary-swap pattern test_harness.py
uses, so RICH's mechanical spine is unchanged.

Firewall (RICH D5, hardened): the worker is spawned with `--tools ""` (ZERO
built-in tools) PLUS an explicit `--disallowedTools` denylist, so it is a pure
text generator with no tool affordance at all. The dependency *contracts* arrive
in the prompt; the worker physically cannot read dependency *source* — and with
no tools in context it cannot emit a tool_use block, so a large generation call
cannot trip `--max-turns` (Phase 11 Fix 1).

Parse defense: reuses llm.parse_json_response (fence stripping, escape repair,
raw-dump-on-failure) and HARDENS it with a balanced-brace extractor, since a
subagent is chattier than a raw completions call.

Usage:
    import subagent_skill
    subagent_skill.install()          # swap the model backend
    # ... then run build.py's build()/test_* as normal ...
    subagent_skill.print_telemetry()  # cost/latency summary
"""

import json
import os
import re
import shutil
import subprocess
import time

from llm import (
    parse_json_response as _rich_parse,
    LLMError,
    LLMParseError,
)


# ── Config ─────────────────────────────────────────────────────────
# Quality skills (PLAN/IMPLEMENT) benefit from a stronger model; override via env.
SUBAGENT_MODEL = os.environ.get("RICH_SUBAGENT_MODEL", "sonnet")
CLAUDE_BIN = os.environ.get("RICH_CLAUDE_BIN", "claude")
TIMEOUT_S = int(os.environ.get("RICH_SUBAGENT_TIMEOUT", "240"))
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2

# The firewall: a pure text generator cannot touch the filesystem or spawn agents.
DISALLOWED_TOOLS = [
    "Read", "Bash", "Glob", "Grep", "Edit", "Write", "NotebookEdit",
    "WebFetch", "WebSearch", "Task",
]


# ── Telemetry ──────────────────────────────────────────────────────
# Phase 11 (efficiency/logging audit): the `claude -p` envelope returns a full `usage`
# block (input/output/cache tokens) and a stop_reason per call. The old _record threw all
# of that away — keeping only cost/wall/turns — so per-call token accounting and cost
# attribution were impossible, and prompts/responses were never logged at all. We now
# (a) keep the full usage in the in-memory ledger AND (b) append one self-describing JSONL
# line per call to RICH_CALL_LOG (default build/llm_calls.jsonl), so every live call is
# auditable after the fact: which model, what it cost, how many input/output/cached tokens,
# how big the prompts were, and whether it errored. Purely additive — no build path changes.
_calls: list[dict] = []

CALL_LOG = os.environ.get("RICH_CALL_LOG", "build/llm_calls.jsonl")


def _record(env: dict, model: str, wall_s: float,
            sys_chars: int = 0, user_chars: int = 0):
    usage = env.get("usage") or {}
    entry = {
        "ts": round(time.time(), 3),
        "model": model,
        "wall_s": round(wall_s, 2),
        "cost_usd": env.get("total_cost_usd"),
        "duration_ms": env.get("duration_ms"),
        "num_turns": env.get("num_turns"),
        "stop_reason": env.get("stop_reason"),
        "subtype": env.get("subtype"),
        "is_error": bool(env.get("is_error")),
        # token accounting (previously discarded)
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        # prompt sizes (chars) so payload growth is visible without storing the prompt
        "sys_chars": sys_chars,
        "user_chars": user_chars,
        "session_id": env.get("session_id"),
    }
    _calls.append(entry)
    try:
        with open(CALL_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def get_telemetry() -> dict:
    total_cost = sum((c["cost_usd"] or 0) for c in _calls)
    total_wall = sum(c["wall_s"] for c in _calls)
    total_in = sum((c.get("input_tokens") or 0) for c in _calls)
    total_out = sum((c.get("output_tokens") or 0) for c in _calls)
    total_cache_r = sum((c.get("cache_read_tokens") or 0) for c in _calls)
    return {
        "n_calls": len(_calls),
        "total_cost_usd": round(total_cost, 4),
        "total_wall_s": round(total_wall, 1),
        "avg_wall_s": round(total_wall / len(_calls), 1) if _calls else 0,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cache_read_tokens": total_cache_r,
        "calls": _calls,
    }


def print_telemetry():
    t = get_telemetry()
    print(f"  [subagent telemetry] {t['n_calls']} calls, "
          f"${t['total_cost_usd']} total, {t['total_wall_s']}s wall "
          f"(avg {t['avg_wall_s']}s/call)")
    print(f"  [subagent tokens] in={t['total_input_tokens']} "
          f"out={t['total_output_tokens']} cache_read={t['total_cache_read_tokens']} "
          f"(per-call ledger: {CALL_LOG})")


def reset_telemetry():
    _calls.clear()


# ── Core invocation ────────────────────────────────────────────────

def is_available() -> bool:
    """A subagent backend is available iff the claude CLI is on PATH."""
    return shutil.which(CLAUDE_BIN) is not None


def _invoke(system_prompt: str, user_prompt: str, *, model: str,
            max_turns: int = 6, timeout: int = TIMEOUT_S) -> str:
    """One `claude -p` call. Returns the model's raw text (envelope `.result`)."""
    cmd = [
        CLAUDE_BIN, "-p",
        "--model", model,
        "--output-format", "json",
        "--append-system-prompt", system_prompt,
        # Phase 11 Fix 1 (the max_turns / tool_use wall) — THREE layers, because a single
        # one proved insufficient:
        #  (1) `--tools ""` removes ALL built-in tools (Read/Bash/… verified gone), so the
        #      subagent cannot touch the filesystem — the SOURCE-FIREWALL is intact (in fact
        #      tightened: with no built-ins there is nothing to read). --disallowedTools is
        #      KEPT below as belt-and-suspenders (Phase 11 §3 invariant: do not relax it).
        #  (2) BUT the Anthropic API SERVER tools (web_search/web_fetch) are NOT governed by
        #      --tools/--disallowedTools, and sonnet on a large render reached for one →
        #      error_max_turns/stop_reason:tool_use (the P8/P10 wall). So the IMPLEMENT/
        #      DERIVE_TESTS system prompts now explicitly forbid ALL tool use (the Phase-8
        #      rewording), telling the model to emit only the JSON.
        #  (3) max_turns is raised from 1 (see _invoke default) so that even if the model
        #      still emits a stray server tool_use, it gets turns to RECOVER to final text
        #      instead of dying at turn 1. A server tool cannot read dependency source, so
        #      the source-firewall holds regardless of the turn budget.
        "--tools", "",
        "--disallowedTools", *DISALLOWED_TOOLS,
        "--max-turns", str(max_turns),
        user_prompt,
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LLMError(f"claude -p timed out after {timeout}s")
    wall = time.time() - t0

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "")[:400]
        raise LLMError(f"claude -p exit {proc.returncode}: {detail}")

    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"could not parse claude envelope: {e}; "
                       f"stdout head: {proc.stdout[:200]!r}")

    if env.get("is_error"):
        raise LLMError(f"claude reported error: {str(env.get('result',''))[:300]}")

    _record(env, model, wall, sys_chars=len(system_prompt), user_chars=len(user_prompt))
    return env.get("result", "") or ""


def call_with_retry(system_prompt: str, user_prompt: str, *,
                    model: str | None = None, temperature: float = 0.1,
                    max_tokens: int = 4096, json_mode: bool = True,
                    max_retries: int = MAX_RETRIES) -> str:
    """Drop-in for llm.call_with_retry. temperature/max_tokens/json_mode are
    accepted for signature-compatibility (claude -p manages these itself)."""
    model = model or SUBAGENT_MODEL
    last = None
    for attempt in range(1, max_retries + 1):
        try:
            return _invoke(system_prompt, user_prompt, model=model)
        except LLMError as e:
            last = e
            if attempt < max_retries:
                delay = RETRY_BACKOFF_BASE ** attempt
                print(f"  [subagent] attempt {attempt}/{max_retries} failed: {e}")
                print(f"  [subagent] retrying in {delay}s...")
                time.sleep(delay)
    raise LLMError(f"subagent: all {max_retries} attempts failed. Last: {last}")


# ── Hardened parse defense ─────────────────────────────────────────
#
# A subagent is chattier and far more likely than a raw json-mode completion to
# (a) wrap JSON in prose/fences and (b) embed real code (regexes, paths) whose
# backslashes must survive JSON decoding. RICH's base parser (llm.py) handles
# fences, but its escape-"repair" regex CORRUPTS already-valid `\\` pairs
# (it turns a valid JSON `\\s` into an invalid `\\\s`), which breaks every
# IMPLEMENT whose source contains a regex. So we parse robustly here and only
# fall back to RICH's parser to get its dump-on-failure behavior.

def _extract_json_block(text: str) -> str | None:
    """Find the first balanced {...} object (skips leading prose / trailing chatter)."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _fix_escapes(s: str) -> str:
    """Correctly double ONLY lone (invalid) backslashes, preserving valid escape
    pairs (\\\\, \\", \\n, \\uXXXX, ...). Unlike RICH's regex, this respects
    already-escaped backslashes by consuming them in pairs."""
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            nxt = s[i + 1] if i + 1 < n else ""
            if nxt in '"\\/bfnrtu':      # valid JSON escape — keep the pair intact
                out.append(c)
                out.append(nxt)
                i += 2
                continue
            out.append("\\\\")           # lone backslash — escape it
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def parse_json_response(raw: str, context: str = "") -> dict:
    """Robustly parse a subagent's (possibly chatty) JSON.

    Order matters: try the CLEAN text first (most subagent JSON is already valid);
    only then attempt escape-repair. Falls back to RICH's parser solely so genuine
    failures are still dumped to disk via the existing machinery."""
    cleaned = _strip_fences(raw)
    block = _extract_json_block(cleaned) or cleaned
    for candidate in (block, _fix_escapes(block)):
        try:
            return json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            continue
    return _rich_parse(raw, context)   # dumps raw + raises LLMParseError


# ── Install / uninstall ────────────────────────────────────────────

_orig = {}


def install():
    """Swap skills.py's model seam to the subagent backend. Idempotent."""
    import skills
    if not _orig:
        _orig["call_with_retry"] = skills.call_with_retry
        _orig["is_available"] = skills.is_available
        _orig["parse_json_response"] = skills.parse_json_response
    skills.call_with_retry = call_with_retry
    skills.is_available = is_available
    skills.parse_json_response = parse_json_response
    print(f"  [subagent] Installed. Backend: claude -p, model={SUBAGENT_MODEL}, "
          f"tools disabled={len(DISALLOWED_TOOLS)}")


def uninstall():
    import skills
    if _orig:
        skills.call_with_retry = _orig["call_with_retry"]
        skills.is_available = _orig["is_available"]
        skills.parse_json_response = _orig["parse_json_response"]
