"""codex_skill.py — Codex CLI backend for RICH's three skills.

This is a drop-in backend for ``skills.py``. It treats Codex as a bounded text
generator: one prompt in, one schema-validated JSON object out. RICH's engine
remains responsible for writing files, running tests, and assembling modules.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from llm import LLMError, parse_json_response as _rich_parse


CODEX_BIN = os.environ.get("RICH_CODEX_BIN", "codex")
CODEX_MODEL = os.environ.get("RICH_CODEX_MODEL") or None
TIMEOUT_S = int(os.environ.get("RICH_CODEX_TIMEOUT", "300"))
MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 2
CALL_LOG = os.environ.get("RICH_CALL_LOG", "build/llm_calls.jsonl")

_calls: list[dict] = []


_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "required": ["is_leaf"],
    "properties": {
        "is_leaf": {"type": "boolean"},
        "children": {"type": "array", "items": {"type": "object"}},
        "edges": {"type": "array", "items": {"type": "object"}},
    },
}

_SOURCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["source"],
    "properties": {"source": {"type": "string"}},
}

_TESTS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tests"],
    "properties": {"tests": {"type": "string"}},
}


def _schema_for(system_prompt: str) -> dict:
    lower = (system_prompt or "").lower()
    if "code generator" in lower:
        return _SOURCE_SCHEMA
    if "test generator" in lower:
        return _TESTS_SCHEMA
    if "architect" in lower or "decompose" in lower:
        return _PLAN_SCHEMA
    return {"type": "object", "additionalProperties": True}


def _record(model: str | None, wall_s: float, sys_chars: int, user_chars: int,
            returncode: int, stderr: str = ""):
    entry = {
        "ts": round(time.time(), 3),
        "backend": "codex",
        "model": model,
        "wall_s": round(wall_s, 2),
        "returncode": returncode,
        "sys_chars": sys_chars,
        "user_chars": user_chars,
        "stderr_head": stderr[:300] if stderr else "",
    }
    _calls.append(entry)
    try:
        Path(CALL_LOG).parent.mkdir(parents=True, exist_ok=True)
        with open(CALL_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def get_telemetry() -> dict:
    total_wall = sum(c["wall_s"] for c in _calls)
    return {
        "n_calls": len(_calls),
        "total_wall_s": round(total_wall, 1),
        "avg_wall_s": round(total_wall / len(_calls), 1) if _calls else 0,
        "calls": _calls,
    }


def print_telemetry():
    t = get_telemetry()
    print(f"  [codex telemetry] {t['n_calls']} calls, "
          f"{t['total_wall_s']}s wall (avg {t['avg_wall_s']}s/call; "
          f"per-call ledger: {CALL_LOG})")


def reset_telemetry():
    _calls.clear()


def is_available() -> bool:
    return shutil.which(CODEX_BIN) is not None


def _prompt(system_prompt: str, user_prompt: str) -> str:
    return f"""You are serving as a bounded text-generation backend for RICH.

Return exactly one JSON object matching the provided output schema. Do not inspect files,
do not run commands, do not edit anything, and do not include prose or Markdown fences.

SYSTEM PROMPT:
{system_prompt}

USER PROMPT:
{user_prompt}
"""


def _invoke(system_prompt: str, user_prompt: str, *, model: str | None,
            timeout: int = TIMEOUT_S) -> str:
    if not is_available():
        raise LLMError(f"{CODEX_BIN!r} not found on PATH")

    with tempfile.TemporaryDirectory(prefix="rich_codex_") as tmp:
        tmp_path = Path(tmp)
        schema_path = tmp_path / "schema.json"
        output_path = tmp_path / "last_message.txt"
        schema_path.write_text(json.dumps(_schema_for(system_prompt), indent=2))

        cmd = [
            CODEX_BIN, "exec",
            "--ephemeral",
            "--ignore-rules",
            "--sandbox", "read-only",
            "--ask-for-approval", "never",
            "--skip-git-repo-check",
            "-C", str(tmp_path),
            "--output-schema", str(schema_path),
            "--output-last-message", str(output_path),
        ]
        if model:
            cmd.extend(["--model", model])
        cmd.append("-")

        t0 = time.time()
        proc = subprocess.run(
            cmd,
            input=_prompt(system_prompt, user_prompt),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        wall = time.time() - t0
        _record(model, wall, len(system_prompt), len(user_prompt),
                proc.returncode, proc.stderr)

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "")[:600]
            raise LLMError(f"codex exec exit {proc.returncode}: {detail}")

        if output_path.exists():
            raw = output_path.read_text().strip()
            if raw:
                return raw

        # Fallback for older CLI behavior: take the last apparent JSON object.
        text = (proc.stdout or "").strip()
        matches = re.findall(r"\{.*\}", text, flags=re.DOTALL)
        if matches:
            return matches[-1]
        return text


def call_with_retry(system_prompt: str, user_prompt: str, *,
                    model: str | None = None, temperature: float = 0.1,
                    max_tokens: int = 4096, json_mode: bool = True,
                    max_retries: int = MAX_RETRIES) -> str:
    model = model or CODEX_MODEL
    last = None
    for attempt in range(1, max_retries + 1):
        try:
            return _invoke(system_prompt, user_prompt, model=model)
        except LLMError as e:
            last = e
            if attempt < max_retries:
                delay = RETRY_BACKOFF_BASE ** attempt
                print(f"  [codex] attempt {attempt}/{max_retries} failed: {e}")
                print(f"  [codex] retrying in {delay}s...")
                time.sleep(delay)
    raise LLMError(f"codex: all {max_retries} attempts failed. Last: {last}")


def parse_json_response(raw: str, context: str = "") -> dict:
    return _rich_parse(raw, context)


_orig = {}


def install():
    import skills
    if not _orig:
        _orig["call_with_retry"] = skills.call_with_retry
        _orig["is_available"] = skills.is_available
        _orig["parse_json_response"] = skills.parse_json_response
    skills.call_with_retry = call_with_retry
    skills.is_available = is_available
    skills.parse_json_response = parse_json_response
    label = CODEX_MODEL or "codex-default"
    print(f"  [codex] Installed. Backend: codex exec, model={label}, sandbox=read-only")


def uninstall():
    import skills
    if _orig:
        skills.call_with_retry = _orig["call_with_retry"]
        skills.is_available = _orig["is_available"]
        skills.parse_json_response = _orig["parse_json_response"]
