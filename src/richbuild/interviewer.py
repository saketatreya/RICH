"""A bounded model call that turns prose into an interview the compiler accepts.

The interview was a form: nine questions whose answer kinds were JSON type
signatures, requirement ids typed by hand, scenarios cross-referenced by
comma-separated id strings, and a browser oracle written as raw JSON in a
textarea. Twelve of the seventeen steps from "open the app" to "deployed
preview" needed a developer's knowledge, and this form was most of them.

This module asks a model for the answers instead, one bounded call per turn,
and holds the answer to the same validator the form was held to:
``AdaptiveInterview.compile`` is unchanged, only what feeds it is. What the
model is asked for is shaped so that an invalid oracle step is unrepresentable
-- one schema branch per action carrying exactly that action's fields, locator
kinds and ARIA roles as enums -- and what it cannot be asked for, the stable
ids, is derived: it names things by a short key, and ``answers_from_keys``
mints the ids a person never has to see.

The discipline is the architect's. A rejected answer is retried once with the
validator's own message as the repair; the call never raises over a
validation failure, because a rejected draft that never reaches a human cannot
be corrected by one. Without a model route the deterministic questions still
answer, tagged ``form-fallback``, exactly as ``planner-fallback`` does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import json
import re
from typing import Any, Callable, Mapping, Sequence

from .budget import Usage
from .interview import (
    AdaptiveInterview,
    InterviewIncomplete,
    InterviewState,
)
from .models import (
    AcceptanceAction,
    BrowserLocatorKind,
    RequirementPriority,
)
# Private on purpose: the vocabulary is the models package's to define, and the
# schema below has to mirror the step constructor's rules exactly. A test holds
# the two to each other rather than this module keeping a second copy.
from .models._common import _ARIA_ROLES
from .models.spec import (
    _LOCATOR_ONLY_ACTIONS,
    _LOCATOR_VALUE_ACTIONS,
    _NO_ARGUMENT_ACTIONS,
    _VALUE_ONLY_ACTIONS,
)
from .providers import GenerationRole, ModelGateway, ModelRequest


class InterviewProposalError(ValueError):
    """A model answer that cannot become interview answers, and why."""


MODEL_SOURCE = "model"
FORM_FALLBACK_SOURCE = "form-fallback"

# Keys are the model's names for things; ids are derived from them and never
# shown. Short and lowercase so `req.<key>` and `scenario.<key>` stay readable
# in evidence and inside the stable-id grammar.
_KEY = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_KEY_SCHEMA = {"type": "string", "pattern": _KEY.pattern}
_MAX_QUESTIONS = 3
_MAX_STEPS = 40
_MAX_TRANSCRIPT_TURNS = 16
_MAX_TRANSCRIPT_CHARS = 24_000

INTERVIEWER_MAX_INPUT_TOKENS = 48_000
INTERVIEWER_MAX_OUTPUT_TOKENS = 16_000
INTERVIEWER_TIMEOUT_SECONDS = 600.0


# ── the shape a model must answer in ───────────────────────────────────────


def _text_schema(max_length: int) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": max_length}


def _texts_schema(*, min_items: int = 0) -> dict[str, Any]:
    return {"type": "array", "minItems": min_items, "items": _text_schema(600)}


def locator_schema() -> dict[str, Any]:
    """Two branches: a role locator names an ARIA role; every other kind names text."""

    other_kinds = [
        kind.value for kind in BrowserLocatorKind if kind is not BrowserLocatorKind.ROLE
    ]
    return {
        "anyOf": [
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": [BrowserLocatorKind.ROLE.value]},
                    "value": {"type": "string", "enum": sorted(_ARIA_ROLES)},
                    "name": _text_schema(200),
                    "exact": {"type": "boolean"},
                },
                "required": ["kind", "value"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": other_kinds},
                    "value": _text_schema(200),
                    "exact": {"type": "boolean"},
                },
                "required": ["kind", "value"],
                "additionalProperties": False,
            },
        ]
    }


def step_schema(action: AcceptanceAction) -> dict[str, Any]:
    """One branch per action, carrying exactly the fields that action takes.

    The step constructor rejects a `fill` without a value or a `reload` with a
    locator; a decoder given this branch cannot produce either.
    """

    properties: dict[str, Any] = {
        "action": {"type": "string", "enum": [action.value]}
    }
    required = ["action"]
    if action in _LOCATOR_ONLY_ACTIONS or action in _LOCATOR_VALUE_ACTIONS:
        properties["locator"] = locator_schema()
        required.append("locator")
    if action in _LOCATOR_VALUE_ACTIONS or action in _VALUE_ONLY_ACTIONS:
        if action in {AcceptanceAction.NAVIGATE, AcceptanceAction.ASSERT_URL}:
            # A local path: starts with one slash, never a scheme or host.
            properties["value"] = {
                "type": "string",
                "pattern": r"^/(?!/)[^\\\x00]*$",
                "maxLength": 400,
            }
        else:
            properties["value"] = _text_schema(4096)
        required.append("value")
    elif action not in _LOCATOR_ONLY_ACTIONS and action not in _NO_ARGUMENT_ACTIONS:
        # An action in none of the four sets would get a branch that accepts
        # anything, which is the opposite of what a branch is for.
        raise ValueError(f"no field rule for acceptance action {action.value!r}")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _requirement_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "key": _KEY_SCHEMA,
            "title": _text_schema(120),
            "statement": _text_schema(600),
            "priority": {
                "type": "string",
                "enum": [member.value for member in RequirementPriority],
            },
        },
        "required": ["key", "title", "statement"],
        "additionalProperties": False,
    }


def _scenario_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "key": _KEY_SCHEMA,
            "title": _text_schema(120),
            "requirement_keys": {"type": "array", "minItems": 1, "items": _KEY_SCHEMA},
            "given": _texts_schema(),
            "when": _texts_schema(min_items=1),
            "then": _texts_schema(min_items=1),
            "oracle": {
                "type": "array",
                "minItems": 2,
                "maxItems": _MAX_STEPS,
                "items": {"anyOf": [step_schema(action) for action in AcceptanceAction]},
            },
        },
        "required": ["key", "title", "requirement_keys", "when", "then", "oracle"],
        "additionalProperties": False,
    }


def answers_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "goal": _text_schema(1200),
            "audiences": _texts_schema(min_items=1),
            "capabilities": {"type": "array", "minItems": 1, "items": _requirement_schema()},
            "quality_constraints": {
                "type": "array",
                "minItems": 1,
                "items": _requirement_schema(),
            },
            "scenarios": {"type": "array", "minItems": 1, "items": _scenario_schema()},
            "roles": _texts_schema(),
            "data_policy": _texts_schema(),
            "integration_failure_policy": _texts_schema(),
            "concurrency_policy": _texts_schema(),
        },
        "required": ["goal", "audiences", "capabilities", "quality_constraints", "scenarios"],
        "additionalProperties": False,
    }


def interviewer_response_schema() -> dict[str, Any]:
    """Either questions or a complete candidate; never a free-form essay."""

    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["questions", "complete"]},
            "summary": _text_schema(600),
            "questions": {
                "type": "array",
                "maxItems": _MAX_QUESTIONS,
                "items": {
                    "type": "object",
                    "properties": {"prompt": _text_schema(400), "why": _text_schema(400)},
                    "required": ["prompt", "why"],
                    "additionalProperties": False,
                },
            },
            "answers": answers_schema(),
        },
        "required": ["status", "summary"],
        "additionalProperties": False,
    }


# ── from keys to the answers the compiler takes ────────────────────────────


def _objects(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise InterviewProposalError(f"{label} must be an array")
    if any(not isinstance(item, Mapping) for item in value):
        raise InterviewProposalError(f"every {label} entry must be an object")
    return list(value)


def _strings(value: Any, label: str, *, required: bool) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise InterviewProposalError(f"{label} must be an array of text")
    out = [str(item).strip() for item in value]
    if any(not item for item in out):
        raise InterviewProposalError(f"{label} contains empty text")
    return out


def _key(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _KEY.match(value):
        raise InterviewProposalError(
            f"{label} key {value!r} must match {_KEY.pattern} (lowercase, digits, hyphens)"
        )
    return value


def answers_from_keys(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Turn a keyed candidate into the answers `AdaptiveInterview.compile` takes.

    Ids are minted here (`req.<key>`, `scenario.<key>`), once, and are never
    part of the model's vocabulary. References are checked against the keys the
    same answer declares, and an unknown one is named in the error so the
    repair says exactly what to fix.
    """

    if not isinstance(candidate, Mapping):
        raise InterviewProposalError("answers must be an object")
    goal = str(candidate.get("goal", "")).strip()
    if not goal:
        raise InterviewProposalError("answers need a goal")
    requirements: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for label, default_priority in (("capabilities", "must"), ("quality_constraints", "should")):
        for entry in _objects(candidate.get(label, []), label):
            key = _key(entry.get("key"), label)
            if key in seen:
                raise InterviewProposalError(
                    f"requirement key {key!r} is declared twice ({seen[key]} and {label})"
                )
            seen[key] = label
            title = str(entry.get("title", "")).strip()
            statement = str(entry.get("statement", "")).strip()
            if not title or not statement:
                raise InterviewProposalError(f"{label} entry {key!r} needs a title and a statement")
            priority = entry.get("priority", default_priority)
            if priority not in {member.value for member in RequirementPriority}:
                raise InterviewProposalError(f"{label} entry {key!r} has an unknown priority {priority!r}")
            requirements.append(
                (label, {"id": f"req.{key}", "title": title, "statement": statement, "priority": priority})
            )
    scenarios: list[dict[str, Any]] = []
    scenario_keys: set[str] = set()
    for entry in _objects(candidate.get("scenarios", []), "scenarios"):
        key = _key(entry.get("key"), "scenario")
        if key in scenario_keys:
            raise InterviewProposalError(f"scenario key {key!r} is declared twice")
        scenario_keys.add(key)
        references = _strings(entry.get("requirement_keys"), f"scenario {key!r} requirement_keys", required=True)
        unknown = [reference for reference in references if reference not in seen]
        if unknown:
            raise InterviewProposalError(
                f"scenario {key!r} references requirement keys that no capability or "
                f"quality constraint declares: {unknown}; declared keys are {sorted(seen)}"
            )
        steps = entry.get("oracle")
        if not isinstance(steps, Sequence) or isinstance(steps, str) or not steps:
            raise InterviewProposalError(f"scenario {key!r} needs a non-empty oracle")
        if len(steps) > _MAX_STEPS:
            raise InterviewProposalError(f"scenario {key!r} has more than {_MAX_STEPS} steps")
        scenarios.append(
            {
                "id": f"scenario.{key}",
                "title": str(entry.get("title", "")).strip() or key,
                "requirement_ids": [f"req.{reference}" for reference in references],
                "given": _strings(entry.get("given"), f"scenario {key!r} given", required=False),
                "when": _strings(entry.get("when"), f"scenario {key!r} when", required=True),
                "then": _strings(entry.get("then"), f"scenario {key!r} then", required=True),
                "oracle": [dict(step) if isinstance(step, Mapping) else step for step in steps],
            }
        )
    answers: dict[str, Any] = {
        "goal": goal,
        "audiences": _strings(candidate.get("audiences"), "audiences", required=True),
        "capabilities": [item for label, item in requirements if label == "capabilities"],
        "quality_constraints": [item for label, item in requirements if label == "quality_constraints"],
        "scenarios": scenarios,
    }
    for policy in ("roles", "data_policy", "integration_failure_policy", "concurrency_policy"):
        values = _strings(candidate.get(policy), policy, required=False)
        if values:
            answers[policy] = values
    return answers


# ── the prompt ─────────────────────────────────────────────────────────────


def interviewer_prompt(
    *,
    project_id: str,
    project_name: str,
    transcript: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Any] | None,
    repair: str | None = None,
) -> tuple[str, str]:
    """The (system, user) pair for one interview turn."""

    actions = ", ".join(action.value for action in AcceptanceAction)
    system_prompt = (
        "You are the RICH interviewer. A person describes software they want in "
        "prose; you turn it into an approved-shape product specification: "
        "requirements, and for every requirement at least one Given/When/Then "
        "scenario with an executable browser oracle. The person never sees ids "
        "or JSON -- they read your scenarios as sentences and approve them.\n"
        "\n"
        "Answer with status 'questions' only when something you genuinely need "
        "is missing -- who can see what (roles), what is stored and for how long "
        "(data_policy), what happens when an external service fails "
        "(integration_failure_policy), how simultaneous edits behave "
        f"(concurrency_policy) -- and ask at most {_MAX_QUESTIONS} questions, "
        "each with why it matters. Otherwise answer 'complete' with the whole "
        "specification. Do not ask about things a sensible default settles.\n"
        "\n"
        "Rules that are checked mechanically, so violating one wastes the "
        "attempt:\n"
        "- Name every requirement and scenario with a short lowercase key "
        "(letters, digits, hyphens). Never write ids; they are derived from "
        "your keys.\n"
        "- Every requirement needs at least one scenario, and a scenario's "
        "requirement_keys must be keys you declared. Give at least one quality "
        "constraint (accessibility, performance, resilience, security).\n"
        "- A scenario's oracle is a list of steps the browser test runner "
        f"executes. Actions: {actions}. 'open_requirement' opens the page "
        "generated for the scenario's first requirement; 'navigate' takes a "
        "local path such as '/'. 'fill', 'press', 'assert_text' and "
        "'assert_value' take a locator and a value; 'click', 'assert_visible' "
        "and 'assert_focused' take a locator only; 'keyboard' and 'assert_url' "
        "take a value only; 'open_requirement' and 'reload' take nothing.\n"
        "- A locator is {kind, value[, name]}. Prefer kind 'label' with the "
        "field's label text for inputs, and kind 'role' with value 'button' "
        "and the button's visible name for buttons. Kind 'text' matches "
        "visible text.\n"
        "- The labels, button names and texts you write become demands on the "
        "implementation: the generated software must show exactly them. Choose "
        "plain, specific words.\n"
        "- Every oracle ends with an assertion, and a scenario about "
        "persistence includes 'reload' before its final assertion.\n"
        "\n"
        "Write scenarios a product person would recognise, not test scripts: "
        "one observable outcome each, in their words."
    )
    trimmed = list(transcript)[-_MAX_TRANSCRIPT_TURNS:]
    document: dict[str, Any] = {
        "project": {"id": project_id, "name": project_name},
        "transcript": [
            {"role": str(turn.get("role", "user")), "text": str(turn.get("text", ""))}
            for turn in trimmed
        ],
        "current_answers": dict(answers) if answers else None,
    }
    user_prompt = (
        "Continue this interview. If current_answers is present, revise it "
        "rather than starting over.\n"
        + json.dumps(document, ensure_ascii=False, sort_keys=True)
    )
    if len(user_prompt) > _MAX_TRANSCRIPT_CHARS:
        document["transcript"] = document["transcript"][-4:]
        user_prompt = (
            "Continue this interview (earlier turns omitted for length). If "
            "current_answers is present, revise it rather than starting over.\n"
            + json.dumps(document, ensure_ascii=False, sort_keys=True)
        )
    if repair:
        user_prompt += (
            "\n\nA previous attempt was rejected. Fix exactly this and change "
            f"nothing else:\n{repair}"
        )
    return system_prompt, user_prompt


# ── one turn ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class InterviewOutcome:
    """What one turn produced, and an honest record of how."""

    status: str  # "complete" | "questions" | "partial"
    summary: str
    questions: tuple[dict[str, str], ...]
    answers: dict[str, Any] | None
    rejections: tuple[str, ...]
    attempts: int
    source: str
    usage: Usage = field(default_factory=Usage)


def _compile_check(project_id: str, project_name: str, answers: Mapping[str, Any]) -> None:
    AdaptiveInterview(
        InterviewState(project_id=project_id, project_name=project_name, answers=dict(answers))
    ).compile()


def form_fallback(
    *,
    project_id: str,
    project_name: str,
    answers: Mapping[str, Any] | None,
) -> InterviewOutcome:
    """The deterministic questions, when no model can be asked.

    This is what the form always did: say which questions the answers so far
    still leave open. Tagged so a surface can say which one answered.
    """

    state = InterviewState(project_id=project_id, project_name=project_name, answers=dict(answers or {}))
    outstanding = AdaptiveInterview(state).next_questions(limit=len(state.answers) + 9)
    if not outstanding and answers:
        try:
            _compile_check(project_id, project_name, answers)
        except InterviewIncomplete as exc:
            return InterviewOutcome(
                status="partial", summary="The answers so far do not compile yet.",
                questions=(), answers=dict(answers), rejections=(str(exc),), attempts=0,
                source=FORM_FALLBACK_SOURCE,
            )
        return InterviewOutcome(
            status="complete", summary="Every question this project raises has an answer.",
            questions=(), answers=dict(answers), rejections=(), attempts=0, source=FORM_FALLBACK_SOURCE,
        )
    return InterviewOutcome(
        status="questions",
        summary="No model route is configured; these are the questions the interview still needs answered.",
        questions=tuple({"prompt": q.prompt, "why": q.rationale} for q in outstanding[:_MAX_QUESTIONS]),
        answers=dict(answers) if answers else None,
        rejections=(),
        attempts=0,
        source=FORM_FALLBACK_SOURCE,
    )


def propose_interview(
    *,
    project_id: str,
    project_name: str,
    transcript: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Any] | None,
    gateway: ModelGateway,
    provider: str,
    model: str,
    run_id: str,
    task_id: str,
    correlation_id: str,
    max_cost_usd: Decimal,
    max_attempts: int = 2,
    max_input_tokens: int = INTERVIEWER_MAX_INPUT_TOKENS,
    max_output_tokens: int = INTERVIEWER_MAX_OUTPUT_TOKENS,
    timeout_seconds: float = INTERVIEWER_TIMEOUT_SECONDS,
) -> InterviewOutcome:
    """Ask for the next turn, and keep asking only while the answer is learning.

    A candidate that the compiler rejects is retried once with the compiler's
    own message; a candidate that still fails is returned as ``partial`` with
    the rejections attached, never raised, so a person can finish it in the
    editor. Questions end the turn immediately: there is nothing to validate.
    """

    if not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    rejections: list[str] = []
    repair: str | None = None
    last_candidate: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        system_prompt, user_prompt = interviewer_prompt(
            project_id=project_id,
            project_name=project_name,
            transcript=transcript,
            answers=answers,
            repair=repair,
        )
        response = gateway.generate(
            ModelRequest(
                run_id=run_id,
                task_id=task_id,
                correlation_id=f"{correlation_id}:{attempt}",
                role=GenerationRole.INTERVIEWER,
                provider=provider,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_schema=interviewer_response_schema(),
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
                max_cost_usd=max_cost_usd,
                timeout_seconds=timeout_seconds,
            )
        )
        document = response.parsed
        if not isinstance(document, Mapping):
            repair = "the answer was not a JSON object"
            rejections.append(repair)
            continue
        summary = str(document.get("summary", "")).strip()
        if document.get("status") == "questions":
            raw_questions = document.get("questions") or []
            questions = tuple(
                {"prompt": str(item.get("prompt", "")).strip(), "why": str(item.get("why", "")).strip()}
                for item in raw_questions
                if isinstance(item, Mapping) and str(item.get("prompt", "")).strip()
            )[:_MAX_QUESTIONS]
            if not questions:
                repair = "status 'questions' needs at least one question with a prompt"
                rejections.append(repair)
                continue
            return InterviewOutcome(
                status="questions", summary=summary, questions=questions,
                answers=dict(answers) if answers else None, rejections=tuple(rejections),
                attempts=attempt, source=MODEL_SOURCE, usage=gateway.usage,
            )
        try:
            candidate = answers_from_keys(document.get("answers") or {})
            last_candidate = candidate
            _compile_check(project_id, project_name, candidate)
        except (InterviewProposalError, InterviewIncomplete) as exc:
            repair = str(exc)
            rejections.append(repair)
            continue
        return InterviewOutcome(
            status="complete", summary=summary, questions=(), answers=candidate,
            rejections=tuple(rejections), attempts=attempt, source=MODEL_SOURCE,
            usage=gateway.usage,
        )
    # No exception. The best candidate travels with what was wrong with it, so
    # the person finishes it in the editor instead of meeting an error.
    return InterviewOutcome(
        status="partial",
        summary="The interviewer could not produce a specification that compiles; the closest attempt is shown with what was wrong.",
        questions=(),
        answers=last_candidate if last_candidate is not None else (dict(answers) if answers else None),
        rejections=tuple(rejections),
        attempts=max_attempts,
        source=MODEL_SOURCE,
        usage=gateway.usage,
    )


@dataclass(frozen=True, slots=True)
class ModelInterviewer:
    """An interviewer backed by bounded model turns; a fresh ledger per turn."""

    gateway_factory: Callable[[], ModelGateway]
    provider: str
    model: str
    max_cost_usd: Decimal = Decimal("1.00")
    max_attempts: int = 2
    project_prefix: str = "interviewer"

    @property
    def attempt_cost_usd(self) -> Decimal:
        return self.max_cost_usd / self.max_attempts

    def turn(
        self,
        *,
        project_id: str,
        project_name: str,
        transcript: Sequence[Mapping[str, Any]],
        answers: Mapping[str, Any] | None,
    ) -> InterviewOutcome:
        return propose_interview(
            project_id=project_id,
            project_name=project_name,
            transcript=transcript,
            answers=answers,
            gateway=self.gateway_factory(),
            provider=self.provider,
            model=self.model,
            run_id=f"{self.project_prefix}:{project_id}",
            task_id=f"{self.project_prefix}:{project_id}:turn",
            correlation_id=f"{self.project_prefix}:{project_id}:{len(transcript)}",
            max_cost_usd=self.attempt_cost_usd,
            max_attempts=self.max_attempts,
        )
