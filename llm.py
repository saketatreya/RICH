"""llm.py — OpenRouter LLM client for M-C onward.

Each skill (PLAN, IMPLEMENT, DERIVE_TESTS) is one OpenRouter call using the
chat completions API. Model is configurable via RICH_MODEL env var with a
sensible default. Retry with backoff. On parse failure, dump raw response
(do not crash silently).
"""

import json
import os
import re
import time
import urllib.request
import urllib.error

# ── Config ─────────────────────────────────────────────────────────
DEFAULT_MODEL = "google/gemini-2.0-flash-001"
RICH_MODEL = os.environ.get("RICH_MODEL", DEFAULT_MODEL)
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds, exponential


# ── Core call ──────────────────────────────────────────────────────

def call_llm(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    json_mode: bool = True,
) -> str:
    """Make one OpenRouter chat completions call.

    Returns the raw response text. Raises LLMError on failure.
    """
    if not API_KEY:
        raise LLMError("OPENROUTER_API_KEY not set — cannot make real LLM calls")

    model = model or RICH_MODEL

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if json_mode:
        body["response_format"] = {"type": "json_object"}

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/saket-atreya/rich",
            "X-Title": "RICH Build System",
        },
    )

    raw = None
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            result = json.loads(raw)
            return result["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise LLMError(f"HTTP {e.code}: {body_text[:500]}")
    except urllib.error.URLError as e:
        raise LLMError(f"Network error: {e}")
    except (KeyError, IndexError) as e:
        preview = raw[:500] if raw else "N/A"
        raise LLMError(f"Response parse error: {e}\nRaw preview: {preview}")


def call_with_retry(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    json_mode: bool = True,
    max_retries: int = MAX_RETRIES,
) -> str:
    """Call LLM with exponential backoff retry."""
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )
        except LLMError as e:
            last_error = e
            if attempt < max_retries:
                delay = RETRY_BACKOFF_BASE ** attempt
                print(f"  [llm] attempt {attempt}/{max_retries} failed: {e}")
                print(f"  [llm] retrying in {delay}s...")
                time.sleep(delay)
    raise LLMError(f"All {max_retries} attempts failed. Last error: {last_error}")


# ── Parse defense ──────────────────────────────────────────────────

def parse_json_response(raw: str, context: str = "") -> dict:
    """Defensively parse LLM output as JSON.

    Strips markdown fences, handles common LLM formatting quirks.
    On failure, dumps raw response to stderr and raises.
    """
    # Strip markdown fences
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    # Fix common LLM JSON escaping bugs: backslashes before non-escape chars
    # Valid JSON escapes: \" \\ \/ \b \f \n \r \t \u
    text = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)

    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError as e:
        # Dump raw response — do NOT crash silently (§5)
        dump_path = f"/tmp/rich_parse_failure_{int(time.time())}.txt"
        with open(dump_path, "w") as f:
            f.write(f"Context: {context}\n")
            f.write(f"Error: {e}\n")
            f.write("=" * 60 + "\n")
            f.write("RAW RESPONSE:\n")
            f.write("=" * 60 + "\n")
            f.write(raw)
        print(f"  [llm] PARSE FAILURE — raw response dumped to {dump_path}")
        raise LLMParseError(f"Failed to parse JSON from {context}: {e}", dump_path)


# ── Error types ────────────────────────────────────────────────────

class LLMError(Exception):
    """Base error for LLM calls."""
    pass


class LLMParseError(LLMError):
    """JSON parse failure — raw response saved to disk."""
    def __init__(self, message: str, dump_path: str):
        super().__init__(message)
        self.dump_path = dump_path


class LLMNotConfigured(LLMError):
    """No API key available — real LLM calls impossible."""
    pass


# ── Convenience ────────────────────────────────────────────────────

def is_available() -> bool:
    """Check if real LLM calls are possible."""
    return bool(API_KEY)


def check_available():
    """Raise LLMNotConfigured if no API key."""
    if not API_KEY:
        raise LLMNotConfigured("OPENROUTER_API_KEY not set — set it or use canned mode")