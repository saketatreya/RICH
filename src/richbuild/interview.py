"""Adaptive, deterministic completeness checks for product discovery."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
from typing import Any, Mapping

from .models import (
    AcceptanceScenario,
    ProjectSpec,
    Requirement,
    RequirementKind,
    RequirementPriority,
)


class InterviewIncomplete(ValueError):
    """The answers do not yet support a truthful executable product specification."""

    def __init__(self, missing: list[str]):
        self.missing = tuple(missing)
        super().__init__("interview is incomplete: " + "; ".join(missing))


@dataclass(frozen=True, slots=True)
class InterviewQuestion:
    id: str
    prompt: str
    answer_kind: str
    rationale: str


@dataclass(frozen=True, slots=True)
class InterviewState:
    project_id: str
    project_name: str
    answers: dict[str, Any] = field(default_factory=dict)
    revision: int = 1

    def answer(self, question_id: str, value: Any) -> "InterviewState":
        if not question_id or question_id not in QUESTION_INDEX:
            raise KeyError(f"unknown interview question {question_id!r}")
        answers = {**self.answers, question_id: value}
        return replace(self, answers=answers, revision=self.revision + 1)


QUESTIONS = (
    InterviewQuestion(
        "goal",
        "What outcome should this product create, and what problem replaces the status quo?",
        "text",
        "The architecture must optimize for an outcome rather than a feature list.",
    ),
    InterviewQuestion(
        "audiences",
        "Who uses the product, and who is the primary user for the first release?",
        "list[text]",
        "Journeys and permission boundaries depend on the people involved.",
    ),
    InterviewQuestion(
        "capabilities",
        "List the first-release capabilities with a stable id, title, and observable statement.",
        "list[{id,title,statement,kind?,priority?}]",
        "Stable requirements are the roots of the evidence graph.",
    ),
    InterviewQuestion(
        "roles",
        "Which roles exist, and what may each role see or change?",
        "list[text]",
        "Identity-related products need explicit authorization and isolation semantics.",
    ),
    InterviewQuestion(
        "data_policy",
        "What data is stored, how long is it retained, and which data is sensitive?",
        "list[text]",
        "Persistence without lifecycle and sensitivity rules produces unsafe defaults.",
    ),
    InterviewQuestion(
        "integration_failure_policy",
        "For every external service, what should users observe during timeout, rejection, or outage?",
        "list[text]",
        "External success paths alone are not executable product contracts.",
    ),
    InterviewQuestion(
        "concurrency_policy",
        "How should simultaneous edits, reconnects, duplicate events, and stale clients behave?",
        "list[text]",
        "Realtime behavior requires an explicit conflict and recovery model.",
    ),
    InterviewQuestion(
        "quality_constraints",
        "State the release constraints for accessibility, security, performance, resilience, and supported devices.",
        "list[{id,title,statement,priority?}]",
        "Quality expectations need traceable requirements and executable evidence.",
    ),
    InterviewQuestion(
        "scenarios",
        (
            "For every requirement, provide at least one Given/When/Then "
            "scenario, its requirement ids, and a data-only browser oracle."
        ),
        (
            "list[{id,title,requirement_ids,given?,when,then,"
            "oracle:list[{action,locator?,value?}]}]"
        ),
        (
            "RICH cannot claim correctness without user-approved actions and "
            "observable assertions."
        ),
    ),
)
QUESTION_INDEX = {question.id: question for question in QUESTIONS}


_IDENTITY = re.compile(r"\b(auth|login|account|user|tenant|team|role|permission)\w*\b", re.I)
_PERSISTENCE = re.compile(
    r"\b(data|database|persist|store|record|upload|profile|document|message)\w*\b", re.I
)
_INTEGRATION = re.compile(
    r"\b(api|payment|email|webhook|integration|provider|external|stripe|github)\w*\b",
    re.I,
)
_REALTIME = re.compile(
    r"\b(real[- ]?time|collaborat|live update|presence|websocket|simultaneous)\w*\b",
    re.I,
)


class AdaptiveInterview:
    """Chooses only questions made relevant by the brief answered so far."""

    def __init__(self, state: InterviewState):
        self.state = state

    def next_questions(self, *, limit: int = 3) -> tuple[InterviewQuestion, ...]:
        if limit < 1:
            raise ValueError("question limit must be at least one")
        answers = self.state.answers
        relevant = ["goal", "audiences", "capabilities"]
        searchable = " ".join(
            _answer_text(answers.get(key)) for key in ("goal", "capabilities")
        )
        if _IDENTITY.search(searchable):
            relevant.append("roles")
        if _PERSISTENCE.search(searchable):
            relevant.append("data_policy")
        if _INTEGRATION.search(searchable):
            relevant.append("integration_failure_policy")
        if _REALTIME.search(searchable):
            relevant.append("concurrency_policy")
        relevant.extend(["quality_constraints", "scenarios"])

        missing = []
        for question_id in relevant:
            value = answers.get(question_id)
            if value is None or value == "" or value == []:
                missing.append(QUESTION_INDEX[question_id])
        return tuple(missing[:limit])

    def compile(self) -> ProjectSpec:
        answers = self.state.answers
        missing = []
        for question_id in ("goal", "audiences", "capabilities", "quality_constraints", "scenarios"):
            if not answers.get(question_id):
                missing.append(f"answer {question_id!r}")
        adaptive_missing = [
            question.id
            for question in self.next_questions(limit=len(QUESTIONS))
            if question.id
            not in {"goal", "audiences", "capabilities", "quality_constraints", "scenarios"}
        ]
        missing.extend(f"answer adaptive question {question_id!r}" for question_id in adaptive_missing)
        if missing:
            raise InterviewIncomplete(missing)

        requirements = [
            _requirement(item, default_kind=RequirementKind.FUNCTIONAL)
            for item in _object_list(answers["capabilities"], "capabilities")
        ]
        requirements.extend(
            _requirement(item, default_kind=RequirementKind.CONSTRAINT)
            for item in _object_list(answers["quality_constraints"], "quality_constraints")
        )
        requirement_ids = {requirement.id for requirement in requirements}
        if len(requirement_ids) != len(requirements):
            raise InterviewIncomplete(["capability and quality requirement ids must be unique"])

        scenarios = [
            _scenario(item)
            for item in _object_list(answers["scenarios"], "scenarios")
        ]
        covered = {
            requirement_id
            for scenario in scenarios
            for requirement_id in scenario.requirement_ids
        }
        unknown = covered - requirement_ids
        if unknown:
            raise InterviewIncomplete(
                [f"scenarios reference unknown requirements {sorted(unknown)}"]
            )
        uncovered = requirement_ids - covered
        if uncovered:
            raise InterviewIncomplete(
                [f"add executable scenarios for requirements {sorted(uncovered)}"]
            )

        constraints = []
        for key in (
            "roles",
            "data_policy",
            "integration_failure_policy",
            "concurrency_policy",
        ):
            constraints.extend(_text_list(answers.get(key, []), key))

        metadata = {
            "interview_revision": self.state.revision,
            "discovery": {
                key: answers[key]
                for key in (
                    "roles",
                    "data_policy",
                    "integration_failure_policy",
                    "concurrency_policy",
                )
                if key in answers
            },
        }
        return ProjectSpec(
            id=self.state.project_id,
            name=self.state.project_name,
            goal=str(answers["goal"]),
            audiences=tuple(_text_list(answers["audiences"], "audiences")),
            requirements=tuple(requirements),
            acceptance_scenarios=tuple(scenarios),
            constraints=tuple(constraints),
            revision=self.state.revision,
            metadata=metadata,
        )


def _answer_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return " ".join(_answer_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_answer_text(item) for item in value)
    return str(value)


def _object_list(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)) or not value:
        raise InterviewIncomplete([f"{label} must be a non-empty list"])
    if any(not isinstance(item, Mapping) for item in value):
        raise InterviewIncomplete([f"every {label} entry must be an object"])
    return list(value)


def _text_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise InterviewIncomplete([f"{label} must be a list of non-empty text values"])
    return [item.strip() for item in value]


def _requirement(
    raw: Mapping[str, Any], *, default_kind: RequirementKind
) -> Requirement:
    try:
        return Requirement(
            id=raw["id"],
            title=raw["title"],
            statement=raw["statement"],
            kind=raw.get("kind", default_kind),
            priority=raw.get("priority", RequirementPriority.MUST),
            source="adaptive_interview",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InterviewIncomplete([f"invalid requirement: {exc}"]) from exc


def _scenario(raw: Mapping[str, Any]) -> AcceptanceScenario:
    try:
        return AcceptanceScenario(
            id=raw["id"],
            title=raw["title"],
            given=tuple(raw.get("given", ())),
            when=tuple(raw["when"]),
            then=tuple(raw["then"]),
            requirement_ids=tuple(raw["requirement_ids"]),
            oracle=tuple(raw["oracle"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InterviewIncomplete([f"invalid acceptance scenario: {exc}"]) from exc
