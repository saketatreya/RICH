"""A small, credential-safe adapter for OpenAI's Responses API.

The adapter deliberately exposes no tool surface.  It accepts a JSON Schema,
requests strict structured output, and turns one HTTP exchange into exactly one
``ModelResponse`` or ``ProviderFailure`` for the v2 gateway.

This adapter is **not** wired into the trusted runtime, which pins exactly one
Anthropic model (see ``runtime.py``).  Nothing constructs it outside its own
tests, so it is not a fallback: it is the second implementation that keeps the
``ModelProvider`` seam honest about being vendor-neutral.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any, Callable, Mapping
from urllib import parse

from .budget import Usage
from ._http import HTTPResponse, MAX_RESPONSE_BYTES, Transport, UrllibTransport
from .canonical import canonical_json_bytes
from .providers import (
    ModelRequest,
    ModelResponse,
    ProviderFailure,
    non_negative_int as _non_negative_int,
    safe_request_id,
)


_ONE_MILLION = Decimal(1_000_000)
# The Responses API does not expose the tokenizer-visible framing around its
# instructions, input, and structured-output grammar.  Account for the complete
# canonical HTTP request envelope by UTF-8 byte count (a conservative upper
# bound for text tokens), then reserve this additional framing margin.
OPENAI_INPUT_FRAMING_TOKEN_ALLOWANCE = 1_024
_RETRYABLE_HTTP_STATUSES = frozenset({408, 409, 425, 429})
_RETRYABLE_ERROR_CODES = frozenset(
    {
        "rate_limit_exceeded",
        "server_error",
        "temporarily_unavailable",
        "timeout",
    }
)


@dataclass(frozen=True, slots=True)
class OpenAITokenRates:
    """USD rates per one million tokens for one exact model identifier."""

    input: Decimal
    cached_input: Decimal
    output: Decimal

    def __post_init__(self) -> None:
        for name, value in (
            ("input", self.input),
            ("cached_input", self.cached_input),
            ("output", self.output),
        ):
            try:
                valid = value.is_finite() and value >= 0
            except (AttributeError, InvalidOperation):
                valid = False
            if not valid:
                raise ValueError(f"{name} token rate must be a finite non-negative Decimal")


# The response envelope and the transport are not vendor-specific; the one
# definition is in _http. The names stay so that code and tests speaking of
# an OpenAI response keep doing so.
OpenAIHTTPResponse = HTTPResponse
OpenAITransport = Transport

ApiKeySource = str | Callable[[], str]


class OpenAIResponsesProvider:
    """Tool-free ``ModelProvider`` implementation for ``POST /v1/responses``."""

    name = "openai"

    def __init__(
        self,
        api_key: ApiKeySource,
        *,
        rates: Mapping[str, OpenAITokenRates] | None = None,
        transport: OpenAITransport | None = None,
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self._validate_base_url(base_url)
        self._api_key_source = api_key
        self._rates = dict(rates or {})
        if any(not model for model in self._rates):
            raise ValueError("pricing model identifiers cannot be empty")
        if any(not isinstance(rate, OpenAITokenRates) for rate in self._rates.values()):
            raise TypeError("rates must map model identifiers to OpenAITokenRates")
        self._transport = transport or UrllibTransport()
        self._endpoint = f"{base_url.rstrip('/')}/responses"

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
            "Authorization": f"Bearer {api_key}",
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
                "OpenAI request timed out",
                retryable=True,
                request_was_sent=True,
            ) from None
        except Exception:
            # Transport exception strings are intentionally discarded: libraries and
            # proxies have been known to embed headers or response bodies in them.
            raise ProviderFailure(
                "OpenAI request failed at the network boundary",
                retryable=True,
                request_was_sent=True,
            ) from None
        finally:
            # Drop the local reference as soon as the trusted transport call returns.
            api_key = ""
            headers = {}

        elapsed = min(max(monotonic() - started, 0.0), request.timeout_seconds)
        if not isinstance(response, OpenAIHTTPResponse):
            raise ProviderFailure(
                "OpenAI transport returned an invalid response",
                retryable=True,
                request_was_sent=True,
            )
        if not 200 <= response.status_code < 300:
            raise ProviderFailure(
                f"OpenAI request failed (HTTP {response.status_code})",
                retryable=(
                    response.status_code in _RETRYABLE_HTTP_STATUSES
                    or response.status_code >= 500
                ),
                request_was_sent=True,
            )

        document = self._decode_document(response.body)
        usage = self._usage(document.get("usage"), request, elapsed)
        self._raise_for_response_failure(document, usage)
        text = self._extract_output_text(document, usage)
        try:
            parsed_output = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            raise ProviderFailure(
                "OpenAI returned invalid structured output",
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
        if not isinstance(request.response_schema, Mapping) or not request.response_schema:
            raise ProviderFailure(
                "OpenAI structured generation requires a response schema",
                retryable=False,
                request_was_sent=False,
            )
        if request.max_output_tokens < 1:
            raise ProviderFailure(
                "OpenAI max output tokens must be at least one",
                retryable=False,
                request_was_sent=False,
            )
        try:
            input_upper_bound = (
                len(canonical_json_bytes(self._payload(request)))
                + OPENAI_INPUT_FRAMING_TOKEN_ALLOWANCE
            )
        except (TypeError, ValueError):
            raise ProviderFailure(
                "OpenAI response schema is not valid JSON",
                retryable=False,
                request_was_sent=False,
            ) from None
        if input_upper_bound > request.max_input_tokens:
            raise ProviderFailure(
                "OpenAI request envelope exceeds its input token reservation",
                retryable=False,
                request_was_sent=False,
            )

        rates = self._rates.get(request.model)
        if rates is not None:
            maximum_cost = (
                Decimal(request.max_input_tokens) * max(rates.input, rates.cached_input)
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
                "OpenAI credential could not be resolved",
                retryable=False,
                request_was_sent=False,
            ) from None
        if (
            not isinstance(value, str)
            or not value.strip()
            or any(character in value for character in "\r\n\0")
        ):
            raise ProviderFailure(
                "OpenAI credential is missing or invalid",
                retryable=False,
                request_was_sent=False,
            )
        return value.strip()

    @staticmethod
    def _payload(request: ModelRequest) -> dict[str, Any]:
        # There is intentionally no ``tools`` field.  This provider is a bounded
        # structured-generation primitive, not an agent execution environment.
        return {
            "model": request.model,
            "instructions": request.system_prompt,
            "input": request.user_prompt,
            "max_output_tokens": request.max_output_tokens,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": f"rich_{request.role.value}_response",
                    "schema": dict(request.response_schema),
                    "strict": True,
                }
            },
        }

    def _usage(
        self,
        raw_usage: Any,
        request: ModelRequest,
        elapsed: float,
    ) -> Usage:
        if not isinstance(raw_usage, Mapping):
            return Usage(
                model_attempts=1,
                input_tokens=request.max_input_tokens,
                output_tokens=request.max_output_tokens,
                cost_usd=request.max_cost_usd,
                execution_seconds=elapsed,
            )
        try:
            input_tokens = _non_negative_int(raw_usage.get("input_tokens"))
            output_tokens = _non_negative_int(raw_usage.get("output_tokens"))
            details = raw_usage.get("input_tokens_details") or {}
            if not isinstance(details, Mapping):
                raise ValueError
            rates = self._rates.get(request.model)
            if "cached_tokens" in details:
                cached_tokens = _non_negative_int(details["cached_tokens"])
            elif rates is not None and rates.cached_input > rates.input:
                # If a future model prices cached tokens above ordinary input and the
                # provider omits the breakdown, choose the costlier classification.
                cached_tokens = input_tokens
            else:
                cached_tokens = 0
            if cached_tokens > input_tokens:
                raise ValueError
        except (TypeError, ValueError):
            return Usage(
                model_attempts=1,
                input_tokens=request.max_input_tokens,
                output_tokens=request.max_output_tokens,
                cost_usd=request.max_cost_usd,
                execution_seconds=elapsed,
            )

        rates = self._rates.get(request.model)
        if rates is None:
            cost = request.max_cost_usd
        else:
            cost = (
                Decimal(input_tokens - cached_tokens) * rates.input
                + Decimal(cached_tokens) * rates.cached_input
                + Decimal(output_tokens) * rates.output
            ) / _ONE_MILLION
            # Return the provider's complete report even if it exceeds the
            # reservation.  ModelGateway durably records and charges the overage,
            # then fails the attempt closed; replacing it with the reservation
            # here would silently undercount known usage.
        return Usage(
            model_attempts=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            execution_seconds=elapsed,
        )

    @staticmethod
    def _decode_document(body: bytes) -> Mapping[str, Any]:
        if len(body) > MAX_RESPONSE_BYTES:
            raise ProviderFailure(
                "OpenAI response exceeded the maximum accepted size",
                retryable=True,
                request_was_sent=True,
            )
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderFailure(
                "OpenAI returned an invalid JSON response",
                retryable=True,
                request_was_sent=True,
            ) from None
        if not isinstance(document, Mapping):
            raise ProviderFailure(
                "OpenAI returned an invalid response envelope",
                retryable=True,
                request_was_sent=True,
            )
        return document

    @staticmethod
    def _raise_for_response_failure(
        document: Mapping[str, Any], usage: Usage
    ) -> None:
        error_value = document.get("error")
        status = document.get("status")
        if error_value or status == "failed":
            code = (
                error_value.get("code")
                if isinstance(error_value, Mapping)
                else None
            )
            safe_code = (
                code
                if isinstance(code, str) and code in _RETRYABLE_ERROR_CODES
                else None
            )
            suffix = f" ({safe_code})" if safe_code else ""
            raise ProviderFailure(
                f"OpenAI response reported a failure{suffix}",
                retryable=safe_code in _RETRYABLE_ERROR_CODES,
                usage=usage,
                request_was_sent=True,
            )
        if status == "incomplete":
            details = document.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, Mapping) else None
            safe_reason = (
                reason
                if isinstance(reason, str)
                and reason in {"max_output_tokens", "content_filter"}
                else None
            )
            suffix = f" ({safe_reason})" if safe_reason else ""
            raise ProviderFailure(
                f"OpenAI response was incomplete{suffix}",
                retryable=False,
                usage=usage,
                request_was_sent=True,
            )

    @staticmethod
    def _extract_output_text(
        document: Mapping[str, Any], usage: Usage
    ) -> str:
        top_level = document.get("output_text")
        if isinstance(top_level, str) and top_level:
            return top_level

        texts: list[str] = []
        refused = False
        output = document.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, Mapping) or item.get("type") != "message":
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, Mapping):
                        continue
                    if part.get("type") == "refusal":
                        refused = True
                    elif (
                        part.get("type") == "output_text"
                        and isinstance(part.get("text"), str)
                    ):
                        texts.append(part["text"])
        if refused:
            raise ProviderFailure(
                "OpenAI refused the structured generation request",
                retryable=False,
                usage=usage,
                request_was_sent=True,
            )
        if not texts:
            raise ProviderFailure(
                "OpenAI response did not contain structured output",
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
            raise ValueError("OpenAI base URL must be an HTTPS origin/path")


def _safe_request_id(value, headers):
    """This API answers with 'x-request-id'; everything else is shared."""

    return safe_request_id(value, headers, header_name="x-request-id")
