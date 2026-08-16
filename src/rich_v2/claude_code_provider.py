"""Reach the pinned model through the Claude Code CLI instead of the HTTP API.

This is a second *route* to the same model policy, not a second policy. The
Messages API route needs an ``ANTHROPIC_API_KEY``; this one runs ``claude -p``
against an existing Claude Code login, so a subscription can pay for a run. It
is selected explicitly and is never a fallback: if it is not chosen, nothing
here executes.

Reaching a model through an agent harness costs something, and the price is
stated rather than hidden:

* **The worker is stripped to a text generator.** ``--tools ""`` leaves no tool
  affordance at all, the working directory is a fresh empty one, and ``HOME``
  is a throwaway containing only a symlink to the credential. That last part is
  not paranoia: with the real ``HOME``, a probe of this exact command reported
  back the operator's own ``CLAUDE.md`` memory. Ambient context the control
  plane never approved is precisely what the information firewall exists to
  keep out of a task prompt.
* **A small residue remains.** Even isolated, the harness supplies the account
  email and the current date. Both are recorded here rather than papered over.
* **Output cannot be bounded before the fact.** The CLI exposes no
  maximum-output-tokens control, so this route can only detect an overage after
  it happens. ``ModelGateway`` then charges it exactly and fails the attempt
  closed, which is the same treatment a provider-reported overage gets on the
  API route -- but the reservation is advisory here, not enforced.
* **The harness makes its own auxiliary calls** with a smaller model. Their
  cost is real and is included in what this charges, and the pinned model is
  verified to have done the substantive work.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any, Mapping

from .budget import Usage
from .providers import ModelRequest, ModelResponse, ProviderFailure


CLAUDE_CODE_PROVIDER = "anthropic-claude-code"
# Linux caps a single argv element at 128 KiB. RICH prompts are far smaller, but
# the failure mode is a confusing E2BIG from the kernel, so bound it here.
MAX_ARGUMENT_BYTES = 96 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
DEFAULT_EFFORT = "high"
# Context the harness injects that RICH did not author. Measured, not assumed:
# with an isolated HOME this is the complete list.
RESIDUAL_AMBIENT_CONTEXT = ("userEmail", "currentDate")
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n\s*```\s*$", re.S)


@dataclass(frozen=True, slots=True)
class ClaudeCodeResult:
    """One completed CLI invocation, as bytes and an exit status."""

    returncode: int
    stdout: bytes
    timed_out: bool = False


class ClaudeCodeCliProvider:
    """Tool-free ``ModelProvider`` backed by one ``claude -p`` subprocess."""

    name = CLAUDE_CODE_PROVIDER

    def __init__(
        self,
        *,
        executable: str = "claude",
        credential_path: str | os.PathLike[str] | None = None,
        effort: str = DEFAULT_EFFORT,
        runner: Any | None = None,
    ) -> None:
        if effort not in EFFORT_LEVELS:
            raise ValueError("effort must be one of the published effort levels")
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError("executable cannot be empty")
        self._executable = executable.strip()
        self._effort = effort
        self._credential_path = (
            Path(credential_path)
            if credential_path is not None
            else Path.home() / ".claude" / ".credentials.json"
        )
        # Injectable so the argv and envelope handling are testable without a
        # login; production leaves it None and runs the real subprocess.
        self._runner = runner

    def __repr__(self) -> str:
        return f"{type(self).__name__}(executable={self._executable!r})"

    def generate(self, request: ModelRequest) -> ModelResponse:
        started = monotonic()
        self._preflight(request)
        argv = self.command(request)

        with tempfile.TemporaryDirectory(prefix="rich-claude-") as sandbox:
            root = Path(sandbox)
            home = self._isolated_home(root)
            workdir = root / "cwd"
            workdir.mkdir()
            result = self._invoke(
                argv,
                prompt=request.user_prompt,
                cwd=workdir,
                home=home,
                timeout_seconds=request.timeout_seconds,
            )

        elapsed = min(max(monotonic() - started, 0.0), request.timeout_seconds)
        if result.timed_out:
            raise ProviderFailure(
                "Claude Code invocation timed out",
                retryable=True,
                request_was_sent=True,
            )
        envelope = self._decode(result, request)
        usage = self._usage(envelope, request, elapsed)
        self._raise_for_failure(envelope, usage)
        self._require_pinned_model(envelope, request, usage)
        text = _structured_text(envelope.get("result"), usage)
        try:
            parsed = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            raise ProviderFailure(
                "Claude Code returned invalid structured output",
                retryable=True,
                usage=usage,
                request_was_sent=True,
            ) from None

        return ModelResponse(
            text=text,
            provider=self.name,
            model=request.model,
            usage=usage,
            parsed=parsed,
            provider_request_id=_safe_session_id(envelope.get("session_id")),
        )

    # -- request construction ------------------------------------------------

    def command(self, request: ModelRequest) -> list[str]:
        """Return the exact argv, so a reviewer can read the whole firewall."""

        return [
            self._executable,
            "--print",
            "--output-format",
            "json",
            "--model",
            request.model,
            # No tool affordance whatsoever. A generator that cannot act cannot
            # read a dependency's source, edit a workspace, or claim a test
            # passed -- and with nothing to call it cannot stall in a tool loop.
            "--tools",
            "",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--effort",
            self._effort,
            # Replaces the harness's own agent system prompt rather than
            # appending to it: the worker must be bounded by RICH's instructions
            # alone, not by a coding agent's defaults layered underneath.
            "--system-prompt",
            request.system_prompt,
        ]

    def _isolated_home(self, root: Path) -> Path:
        """Build a HOME holding nothing but a link to the credential.

        Discovery of ``CLAUDE.md``, user memory, settings and project history
        all key off ``HOME``. Pointing it at an empty directory is what keeps a
        task prompt equal to the prompt RICH built. The credential is linked
        rather than copied so the secret is never duplicated at rest, and so a
        token refresh still lands in the real file.
        """

        home = root / "home"
        (home / ".claude").mkdir(parents=True)
        if not self._credential_path.exists():
            raise ProviderFailure(
                "Claude Code credential is missing; run `claude` once to log in",
                retryable=False,
                request_was_sent=False,
            )
        (home / ".claude" / ".credentials.json").symlink_to(
            self._credential_path.resolve()
        )
        return home

    def _preflight(self, request: ModelRequest) -> None:
        if request.provider != self.name:
            raise ProviderFailure(
                "model request was routed to the wrong provider",
                retryable=False,
                request_was_sent=False,
            )
        if (
            not isinstance(request.response_schema, Mapping)
            or not request.response_schema
        ):
            raise ProviderFailure(
                "Claude Code structured generation requires a response schema",
                retryable=False,
                request_was_sent=False,
            )
        for label, value in (
            ("system prompt", request.system_prompt),
            ("model", request.model),
        ):
            if len(value.encode("utf-8")) > MAX_ARGUMENT_BYTES:
                raise ProviderFailure(
                    f"Claude Code {label} exceeds the maximum argument size",
                    retryable=False,
                    request_was_sent=False,
                )
        if request.prompt_bytes > request.max_input_tokens:
            raise ProviderFailure(
                "Claude Code prompt exceeds its input token reservation",
                retryable=False,
                request_was_sent=False,
            )

    # -- subprocess boundary -------------------------------------------------

    def _invoke(
        self,
        argv: list[str],
        *,
        prompt: str,
        cwd: Path,
        home: Path,
        timeout_seconds: float,
    ) -> ClaudeCodeResult:
        if self._runner is not None:
            return self._runner(argv, prompt=prompt, cwd=cwd, home=home)
        if shutil.which(argv[0]) is None:
            raise ProviderFailure(
                f"Claude Code executable {argv[0]!r} was not found on PATH",
                retryable=False,
                request_was_sent=False,
            )
        environment = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            # This route exists to spend a subscription. An ambient API key
            # would silently bill somewhere else, so it is not inherited: an
            # expired login fails closed rather than quietly changing payer.
            "CLAUDE_CODE_ENTRYPOINT": "rich-v2",
        }
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            stdout, _ = process.communicate(
                prompt.encode("utf-8"), timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired:
            _reap(process)
            return ClaudeCodeResult(returncode=-1, stdout=b"", timed_out=True)
        except Exception:
            _reap(process)
            # Exception text from the process boundary is discarded: it can
            # carry environment and path detail that must not reach a caller.
            raise ProviderFailure(
                "Claude Code invocation failed at the process boundary",
                retryable=True,
                request_was_sent=True,
            ) from None
        return ClaudeCodeResult(returncode=process.returncode, stdout=stdout)

    # -- envelope handling ---------------------------------------------------

    @staticmethod
    def _decode(
        result: ClaudeCodeResult, request: ModelRequest
    ) -> Mapping[str, Any]:
        if len(result.stdout) > MAX_RESULT_BYTES:
            raise ProviderFailure(
                "Claude Code response exceeded the maximum accepted size",
                retryable=True,
                request_was_sent=True,
            )
        lines = [
            line for line in result.stdout.decode("utf-8", "replace").splitlines() if line.strip()
        ]
        if not lines:
            raise ProviderFailure(
                f"Claude Code produced no result envelope (exit {result.returncode})",
                retryable=result.returncode != 0,
                request_was_sent=True,
            )
        try:
            # parse_float=Decimal so the reported cost is the exact literal the
            # harness wrote. Money never becomes a float, not even in transit.
            document = json.loads(lines[-1], parse_float=Decimal)
        except json.JSONDecodeError:
            raise ProviderFailure(
                "Claude Code returned an unparsable result envelope",
                retryable=True,
                request_was_sent=True,
            ) from None
        if not isinstance(document, Mapping) or document.get("type") != "result":
            raise ProviderFailure(
                "Claude Code returned an unrecognized result envelope",
                retryable=True,
                request_was_sent=True,
            )
        del request
        return document

    def _usage(
        self,
        envelope: Mapping[str, Any],
        request: ModelRequest,
        elapsed: float,
    ) -> Usage:
        reservation = Usage(
            model_attempts=1,
            input_tokens=request.max_input_tokens,
            output_tokens=request.max_output_tokens,
            cost_usd=request.max_cost_usd,
            execution_seconds=elapsed,
        )
        per_model = envelope.get("modelUsage")
        cost = envelope.get("total_cost_usd")
        # An empty breakdown alongside a reported cost would charge the money
        # and none of the tokens. Anything less than a usable report settles
        # the reservation instead.
        if (
            not isinstance(per_model, Mapping)
            or not per_model
            or not isinstance(cost, Decimal)
        ):
            return reservation
        try:
            # Every model the harness used, not only the pinned one. Claude Code
            # spends a small model on its own auxiliary work, and that spend is
            # as real as the rest; counting only the pinned model would
            # under-report the budget by whatever the harness did on the side.
            input_tokens = 0
            output_tokens = 0
            for entry in per_model.values():
                if not isinstance(entry, Mapping):
                    raise ValueError
                input_tokens += (
                    _non_negative_int(entry.get("inputTokens"))
                    + _non_negative_int(entry.get("cacheReadInputTokens") or 0)
                    + _non_negative_int(entry.get("cacheCreationInputTokens") or 0)
                )
                output_tokens += _non_negative_int(entry.get("outputTokens"))
            if not cost.is_finite() or cost < 0:
                raise ValueError
        except (TypeError, ValueError):
            return reservation
        return Usage(
            model_attempts=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            execution_seconds=elapsed,
        )

    @staticmethod
    def _raise_for_failure(envelope: Mapping[str, Any], usage: Usage) -> None:
        denials = envelope.get("permission_denials")
        if denials:
            # The worker was launched with no tools. An attempted tool use means
            # the firewall assumption no longer holds, whatever the output says.
            raise ProviderFailure(
                "Claude Code reported a tool permission denial from a "
                "tool-free worker",
                retryable=False,
                usage=usage,
                request_was_sent=True,
            )
        status = envelope.get("api_error_status")
        if envelope.get("is_error") or envelope.get("subtype") != "success":
            retryable = isinstance(status, int) and (status == 429 or status >= 500)
            raise ProviderFailure(
                f"Claude Code reported a failed session{_status_suffix(status)}",
                retryable=retryable,
                usage=usage,
                request_was_sent=True,
            )
        stop_reason = envelope.get("stop_reason")
        if stop_reason not in (None, "end_turn"):
            safe = stop_reason if isinstance(stop_reason, str) else None
            raise ProviderFailure(
                "Claude Code response was incomplete"
                + (f" ({safe})" if safe else ""),
                retryable=False,
                usage=usage,
                request_was_sent=True,
            )

    @staticmethod
    def _require_pinned_model(
        envelope: Mapping[str, Any], request: ModelRequest, usage: Usage
    ) -> None:
        """Confirm the pinned model, not an auxiliary one, did the work."""

        per_model = envelope.get("modelUsage")
        if not isinstance(per_model, Mapping) or not per_model:
            raise ProviderFailure(
                "Claude Code did not report which model produced the response",
                retryable=False,
                usage=usage,
                request_was_sent=True,
            )
        outputs: dict[str, int] = {}
        for name, entry in per_model.items():
            if not isinstance(entry, Mapping):
                continue
            canonical = entry.get("canonicalModel")
            key = canonical if isinstance(canonical, str) and canonical else name
            try:
                outputs[key] = max(
                    outputs.get(key, 0), _non_negative_int(entry.get("outputTokens"))
                )
            except (TypeError, ValueError):
                continue
        if request.model not in outputs:
            raise ProviderFailure(
                "Claude Code did not use the pinned model",
                retryable=False,
                usage=usage,
                request_was_sent=True,
            )
        pinned = outputs[request.model]
        if any(
            other > pinned for name, other in outputs.items() if name != request.model
        ):
            # The harness's auxiliary calls are small by design. One that
            # out-generated the pinned model means the answer may not have come
            # from the model the run approved.
            raise ProviderFailure(
                "an auxiliary model out-generated the pinned model",
                retryable=False,
                usage=usage,
                request_was_sent=True,
            )


def _structured_text(value: Any, usage: Usage) -> str:
    """Recover the JSON document from a harness that may narrate around it.

    The API route can demand a schema; this one can only ask. Being tolerant
    here is safe because it decides whether a generation is *accepted*, never
    whether it is *verified* -- the file bundle parser repeats every check and
    the sandboxed gates are what publish success.
    """

    if not isinstance(value, str) or not value.strip():
        raise ProviderFailure(
            "Claude Code response did not contain structured output",
            retryable=True,
            usage=usage,
            request_was_sent=True,
        )
    text = value.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group("body").strip()
    if text.startswith("{") or text.startswith("["):
        return text
    start = min(
        (index for index in (text.find("{"), text.find("[")) if index >= 0),
        default=-1,
    )
    if start < 0:
        raise ProviderFailure(
            "Claude Code response did not contain structured output",
            retryable=True,
            usage=usage,
            request_was_sent=True,
        )
    return text[start:].strip()


def _status_suffix(status: Any) -> str:
    return f" (HTTP {status})" if isinstance(status, int) else ""


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def _safe_session_id(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_SESSION_ID.fullmatch(value):
        return value
    return None


def _reap(process: "subprocess.Popen[bytes]") -> None:
    """Kill the whole session, not just the leader it spawned."""

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
