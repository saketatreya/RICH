"""A small, credential-safe adapter for Anthropic's Messages API.

The adapter deliberately exposes no tool surface.  It accepts a JSON Schema,
requests structured output through ``output_config.format``, and turns one HTTP
exchange into exactly one ``ModelResponse`` or ``ProviderFailure`` for the
gateway.

Its small HTTP-boundary helpers are intentionally duplicated from
``openai_provider`` rather than shared.  Each provider adapter is the trust
boundary for one vendor's wire format and is meant to be auditable on its own;
a shared helper module would mean a change reviewed against one vendor's
guarantees silently applies to the other.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any, Callable, Mapping, Protocol
from urllib import error, parse, request as urllib_request

from .budget import Usage
from .canonical import canonical_json_bytes
from .providers import (
    ModelRequest,
    ModelResponse,
    ProviderFailure,
    non_negative_int as _non_negative_int,
    safe_request_id,
)


_ONE_MILLION = Decimal(1_000_000)
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ANTHROPIC_API_VERSION = "2023-06-01"
# The Messages API does not expose the tokenizer-visible framing around its
# system prompt, message envelope, and structured-output grammar.  Account for
# the complete canonical HTTP request body by UTF-8 byte count -- byte-level BPE
# cannot emit more tokens than input bytes -- then reserve this extra margin.
ANTHROPIC_INPUT_FRAMING_TOKEN_ALLOWANCE = 1_024
# ``effort`` shapes how many output tokens Claude spends, thinking included.
# ``high`` is the API default; naming it explicitly keeps the request envelope
# fully determined by this module rather than by a server-side default.
ANTHROPIC_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
DEFAULT_EFFORT = "high"
# 409 (conflict) and 429 (rate limit) are documented as retryable; 500, 504 and
# 529 are covered by the >= 500 rule at the call site.  408 and 425 are not
# emitted by the API itself but can be injected by an intermediary proxy.
_RETRYABLE_HTTP_STATUSES = frozenset({408, 409, 425, 429})
_RETRYABLE_ERROR_TYPES = frozenset(
    {
        "api_error",
        "overloaded_error",
        "rate_limit_error",
        "timeout_error",
    }
)
# Every stop reason that is not a completed structured response.  ``tool_use``
# and ``pause_turn`` cannot occur for a request that declares no tools, so
# seeing one means the wire contract changed underneath this adapter.
_INCOMPLETE_STOP_REASONS = frozenset(
    {
        "max_tokens",
        "model_context_window_exceeded",
        "pause_turn",
        "refusal",
        "stop_sequence",
        "tool_use",
    }
)


@dataclass(frozen=True, slots=True)
class AnthropicTokenRates:
    """USD rates per one million tokens for one exact model identifier.

    Anthropic prices four distinct input classifications.  Cache writes are
    split by time-to-live because a one-hour write costs twice base input while
    a five-minute write costs 1.25x, and the response reports the two counts
    separately.  Keeping them separate is what lets ``_usage`` charge the exact
    reported mix instead of assuming the worst case for every input token.
    """

    input: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal
    output: Decimal

    def __post_init__(self) -> None:
        for name, value in (
            ("input", self.input),
            ("cache_write_5m", self.cache_write_5m),
            ("cache_write_1h", self.cache_write_1h),
            ("cache_read", self.cache_read),
            ("output", self.output),
        ):
            try:
                valid = value.is_finite() and value >= 0
            except (AttributeError, InvalidOperation):
                valid = False
            if not valid:
                raise ValueError(
                    f"{name} token rate must be a finite non-negative Decimal"
                )

    @property
    def costliest_input(self) -> Decimal:
        """The rate a reservation must assume when the mix is not yet known."""

        return max(
            self.input,
            self.cache_write_5m,
            self.cache_write_1h,
            self.cache_read,
        )


@dataclass(frozen=True, slots=True)
class AnthropicHTTPResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not 100 <= self.status_code <= 599:
            raise ValueError("HTTP status code is out of range")
        if not isinstance(self.body, bytes):
            raise TypeError("HTTP response body must be bytes")


class AnthropicTransport(Protocol):
    """Trusted HTTP boundary, injectable for deterministic tests."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> AnthropicHTTPResponse:
        ...


class _UrllibTransport:
    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> AnthropicHTTPResponse:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        http_request = urllib_request.Request(
            url=url,
            data=encoded,
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib_request.urlopen(
                http_request, timeout=timeout_seconds
            ) as response:
                body = _bounded_read(response)
                return AnthropicHTTPResponse(
                    status_code=int(response.status),
                    body=body,
                    headers=dict(response.headers.items()),
                )
        except error.HTTPError as exc:
            # HTTPError is also a file-like response.  Preserve only the status and
            # bytes for internal classification; its body never reaches an exception.
            body = _bounded_read(exc)
            return AnthropicHTTPResponse(
                status_code=int(exc.code),
                body=body,
                headers=dict(exc.headers.items()) if exc.headers else {},
            )


ApiKeySource = str | Callable[[], str]


class AnthropicMessagesProvider:
    """Tool-free ``ModelProvider`` implementation for ``POST /v1/messages``."""

    name = "anthropic"

    def __init__(
        self,
        api_key: ApiKeySource,
        *,
        rates: Mapping[str, AnthropicTokenRates] | None = None,
        transport: AnthropicTransport | None = None,
        base_url: str = "https://api.anthropic.com/v1",
        effort: str = DEFAULT_EFFORT,
    ) -> None:
        self._validate_base_url(base_url)
        if effort not in ANTHROPIC_EFFORT_LEVELS:
            raise ValueError("effort must be one of the published effort levels")
        self._api_key_source = api_key
        self._rates = dict(rates or {})
        if any(not model for model in self._rates):
            raise ValueError("pricing model identifiers cannot be empty")
        if any(
            not isinstance(rate, AnthropicTokenRates)
            for rate in self._rates.values()
        ):
            raise TypeError("rates must map model identifiers to AnthropicTokenRates")
        self._transport = transport or _UrllibTransport()
        self._endpoint = f"{base_url.rstrip('/')}/messages"
        self._effort = effort

    def __repr__(self) -> str:
        priced_models = tuple(sorted(self._rates))
        return (
            f"{type(self).__name__}(endpoint={self._endpoint!r}, "
            f"priced_models={priced_models!r})"
        )

    def generate(self, request: ModelRequest) -> ModelResponse:
        started = monotonic()
        self._preflight(request)
        api_key = self._resolve_api_key()
        payload = self._payload(request)
        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            response = self._transport.post_json(
                url=self._endpoint,
                headers=headers,
                payload=payload,
                timeout_seconds=request.timeout_seconds,
            )
        except (TimeoutError, socket.timeout):
            raise ProviderFailure(
                "Anthropic request timed out",
                retryable=True,
                request_was_sent=True,
            ) from None
        except Exception:
            # Transport exception strings are intentionally discarded: libraries and
            # proxies have been known to embed headers or response bodies in them.
            raise ProviderFailure(
                "Anthropic request failed at the network boundary",
                retryable=True,
                request_was_sent=True,
            ) from None
        finally:
            # Drop the local reference as soon as the trusted transport call returns.
            api_key = ""
            headers = {}

        elapsed = min(max(monotonic() - started, 0.0), request.timeout_seconds)
        if not isinstance(response, AnthropicHTTPResponse):
            raise ProviderFailure(
                "Anthropic transport returned an invalid response",
                retryable=True,
                request_was_sent=True,
            )
        if not 200 <= response.status_code < 300:
            self._raise_for_http_error(response)

        document = self._decode_document(response.body)
        usage = self._usage(document.get("usage"), request, elapsed)
        self._raise_for_response_failure(document, usage)
        text = self._extract_output_text(document, usage)
        try:
            parsed_output = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            raise ProviderFailure(
                "Anthropic returned invalid structured output",
                retryable=True,
                usage=usage,
                request_was_sent=True,
            ) from None

        return ModelResponse(
            text=text,
            provider=self.name,
            model=request.model,
            usage=usage,
            parsed=parsed_output,
            provider_request_id=_safe_request_id(
                document.get("id"), response.headers or {}
            ),
        )

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
                "Anthropic structured generation requires a response schema",
                retryable=False,
                request_was_sent=False,
            )
        if request.max_output_tokens < 1:
            raise ProviderFailure(
                "Anthropic max output tokens must be at least one",
                retryable=False,
                request_was_sent=False,
            )
        try:
            input_upper_bound = (
                len(canonical_json_bytes(self._payload(request)))
                + ANTHROPIC_INPUT_FRAMING_TOKEN_ALLOWANCE
            )
        except (TypeError, ValueError):
            raise ProviderFailure(
                "Anthropic response schema is not valid JSON",
                retryable=False,
                request_was_sent=False,
            ) from None
        if input_upper_bound > request.max_input_tokens:
            raise ProviderFailure(
                "Anthropic request envelope exceeds its input token reservation",
                retryable=False,
                request_was_sent=False,
            )

        rates = self._rates.get(request.model)
        if rates is not None:
            maximum_cost = (
                Decimal(request.max_input_tokens) * rates.costliest_input
                + Decimal(request.max_output_tokens) * rates.output
            ) / _ONE_MILLION
            if maximum_cost > request.max_cost_usd:
                raise ProviderFailure(
                    "approved cost limit is below the priced token reservation",
                    retryable=False,
                    request_was_sent=False,
                )

    def _resolve_api_key(self) -> str:
        try:
            value = (
                self._api_key_source()
                if callable(self._api_key_source)
                else self._api_key_source
            )
        except Exception:
            raise ProviderFailure(
                "Anthropic credential could not be resolved",
                retryable=False,
                request_was_sent=False,
            ) from None
        if (
            not isinstance(value, str)
            or not value.strip()
            or any(character in value for character in "\r\n\0")
        ):
            raise ProviderFailure(
                "Anthropic credential is missing or invalid",
                retryable=False,
                request_was_sent=False,
            )
        return value.strip()

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        # There is intentionally no ``tools`` field.  This provider is a bounded
        # structured-generation primitive, not an agent execution environment.
        #
        # ``max_tokens`` is a hard ceiling on thinking plus response text
        # together, so a reservation that is tight for the response alone
        # truncates.  That surfaces as a non-retryable "response was incomplete"
        # failure rather than a silently short file.
        return {
            "model": request.model,
            "system": request.system_prompt,
            "messages": [{"role": "user", "content": request.user_prompt}],
            "max_tokens": request.max_output_tokens,
            "output_config": {
                "effort": self._effort,
                "format": {
                    "type": "json_schema",
                    "schema": dict(request.response_schema),
                },
            },
        }

    def _usage(
        self,
        raw_usage: Any,
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
        if not isinstance(raw_usage, Mapping):
            return reservation
        try:
            base_input = _non_negative_int(raw_usage.get("input_tokens"))
            output_tokens = _non_negative_int(raw_usage.get("output_tokens"))
            cache_read = _non_negative_int(
                raw_usage.get("cache_read_input_tokens") or 0
            )
            cache_written = _non_negative_int(
                raw_usage.get("cache_creation_input_tokens") or 0
            )
            write_5m, write_1h = self._cache_write_split(raw_usage, cache_written)
        except (TypeError, ValueError):
            return reservation

        rates = self._rates.get(request.model)
        if rates is None:
            cost = request.max_cost_usd
        else:
            cost = (
                Decimal(base_input) * rates.input
                + Decimal(write_5m) * rates.cache_write_5m
                + Decimal(write_1h) * rates.cache_write_1h
                + Decimal(cache_read) * rates.cache_read
                + Decimal(output_tokens) * rates.output
            ) / _ONE_MILLION
            # Return the provider's complete report even if it exceeds the
            # reservation.  ModelGateway durably records and charges the overage,
            # then fails the attempt closed; replacing it with the reservation
            # here would silently undercount known usage.
        return Usage(
            model_attempts=1,
            # ``Usage`` is deliberately flat and ``input_tokens`` is the whole
            # prompt.  Anthropic's ``input_tokens`` is only the uncached
            # remainder, so reporting it alone would undercount the prompt by
            # everything caching served.
            input_tokens=base_input + cache_written + cache_read,
            output_tokens=output_tokens,
            cost_usd=cost,
            execution_seconds=elapsed,
        )

    @staticmethod
    def _cache_write_split(
        raw_usage: Mapping[str, Any], cache_written: int
    ) -> tuple[int, int]:
        """Split cache-write tokens by time-to-live, conservatively.

        The one-hour rate is twice the five-minute rate.  When the breakdown is
        absent or does not reconcile with the total, charge every cache-write
        token at the costlier one-hour classification.
        """

        breakdown = raw_usage.get("cache_creation")
        if not isinstance(breakdown, Mapping):
            return 0, cache_written
        write_5m = _non_negative_int(
            breakdown.get("ephemeral_5m_input_tokens") or 0
        )
        write_1h = _non_negative_int(
            breakdown.get("ephemeral_1h_input_tokens") or 0
        )
        if write_5m + write_1h != cache_written:
            return 0, cache_written
        return write_5m, write_1h

    @staticmethod
    def _decode_document(body: bytes) -> Mapping[str, Any]:
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ProviderFailure(
                "Anthropic response exceeded the maximum accepted size",
                retryable=True,
                request_was_sent=True,
            )
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderFailure(
                "Anthropic returned an invalid JSON response",
                retryable=True,
                request_was_sent=True,
            ) from None
        if not isinstance(document, Mapping):
            raise ProviderFailure(
                "Anthropic returned an invalid response envelope",
                retryable=True,
                request_was_sent=True,
            )
        return document

    @classmethod
    def _raise_for_http_error(cls, response: AnthropicHTTPResponse) -> None:
        """Classify a non-2xx response without echoing any of its body."""

        status = response.status_code
        # The status alone decides retryability. The body is attacker-reachable
        # through any intermediary, and letting it widen the classification
        # would let a 400 for a malformed request be replayed as if transient.
        # It contributes an allowlisted label to the message and nothing more.
        retryable = status in _RETRYABLE_HTTP_STATUSES or status >= 500
        safe_type = None
        try:
            document = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            document = None
        if isinstance(document, Mapping):
            error_value = document.get("error")
            if isinstance(error_value, Mapping):
                error_type = error_value.get("type")
                if (
                    isinstance(error_type, str)
                    and error_type in _RETRYABLE_ERROR_TYPES
                ):
                    safe_type = error_type
        suffix = f" ({safe_type})" if safe_type else ""
        raise ProviderFailure(
            f"Anthropic request failed (HTTP {status}){suffix}",
            retryable=retryable,
            request_was_sent=True,
        )

    @staticmethod
    def _raise_for_response_failure(
        document: Mapping[str, Any], usage: Usage
    ) -> None:
        if document.get("type") == "error":
            error_value = document.get("error")
            error_type = (
                error_value.get("type")
                if isinstance(error_value, Mapping)
                else None
            )
            safe_type = (
                error_type
                if isinstance(error_type, str) and error_type in _RETRYABLE_ERROR_TYPES
                else None
            )
            suffix = f" ({safe_type})" if safe_type else ""
            raise ProviderFailure(
                f"Anthropic response reported a failure{suffix}",
                retryable=safe_type is not None,
                usage=usage,
                request_was_sent=True,
            )
        stop_reason = document.get("stop_reason")
        if isinstance(stop_reason, str) and stop_reason in _INCOMPLETE_STOP_REASONS:
            # Retrying an identical request against an identical reservation
            # reproduces every one of these outcomes, so none is retryable.
            # ``max_tokens`` counts thinking tokens, so the fix is a larger
            # output reservation or a lower effort level, not another attempt.
            raise ProviderFailure(
                f"Anthropic response was incomplete ({stop_reason})",
                retryable=False,
                usage=usage,
                request_was_sent=True,
            )

    @staticmethod
    def _extract_output_text(
        document: Mapping[str, Any], usage: Usage
    ) -> str:
        content = document.get("content")
        if not isinstance(content, list):
            raise ProviderFailure(
                "Anthropic response did not contain structured output",
                retryable=True,
                usage=usage,
                request_was_sent=True,
            )
        texts: list[str] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            # ``thinking`` and ``redacted_thinking`` blocks precede the answer
            # whenever thinking is active.  Only ``text`` blocks carry the
            # structured output.
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
        if not texts:
            raise ProviderFailure(
                "Anthropic response did not contain structured output",
                retryable=True,
                usage=usage,
                request_was_sent=True,
            )
        return "\n".join(texts)

    @staticmethod
    def _validate_base_url(base_url: str) -> None:
        parsed = parse.urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Anthropic base URL must be an HTTPS origin/path")


def _bounded_read(response: Any) -> bytes:
    body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        # This exception is sanitized by the provider's network boundary.
        raise ValueError("response too large")
    return body


def _safe_request_id(value, headers):
    """This API answers with 'request-id'; everything else is shared."""

    return safe_request_id(value, headers, header_name="request-id")
