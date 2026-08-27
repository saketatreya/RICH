"""Approved product intent: requirements and acceptance scenarios."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ._common import (
    AcceptanceAction,
    BrowserLocatorKind,
    ModelValidationError,
    RequirementKind,
    RequirementPriority,
    SCHEMA_VERSION,
    _ARIA_ROLES,
    _check_schema_version,
    _enum,
    _json_mapping,
    _models,
    _positive_revision,
    _serialized,
    _stable_id,
    _strict_fields,
    _strings,
    _text,
    _unique_by_id,
)



@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    title: str
    statement: str
    kind: RequirementKind = RequirementKind.FUNCTIONAL
    priority: RequirementPriority = RequirementPriority.MUST
    source: str = "user"

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "requirement.id"))
        object.__setattr__(self, "title", _text(self.title, "requirement.title"))
        object.__setattr__(
            self, "statement", _text(self.statement, "requirement.statement")
        )
        object.__setattr__(
            self, "kind", _enum(self.kind, RequirementKind, "requirement.kind")
        )
        object.__setattr__(
            self,
            "priority",
            _enum(self.priority, RequirementPriority, "requirement.priority"),
        )
        object.__setattr__(self, "source", _text(self.source, "requirement.source"))

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Requirement":
        doc = _strict_fields(
            data,
            label="Requirement",
            required={"id", "title", "statement"},
            optional={"kind", "priority", "source"},
        )
        return cls(
            id=doc["id"],
            title=doc["title"],
            statement=doc["statement"],
            kind=doc.get("kind", RequirementKind.FUNCTIONAL),
            priority=doc.get("priority", RequirementPriority.MUST),
            source=doc.get("source", "user"),
        )


@dataclass(frozen=True, slots=True)
class BrowserLocator:
    kind: BrowserLocatorKind
    value: str
    name: str | None = None
    exact: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "kind",
            _enum(self.kind, BrowserLocatorKind, "browser_locator.kind"),
        )
        value = _text(self.value, "browser_locator.value")
        if len(value.encode("utf-8")) > 1024:
            raise ModelValidationError(
                "browser_locator.value cannot exceed 1024 UTF-8 bytes"
            )
        object.__setattr__(self, "value", value)
        if self.kind is BrowserLocatorKind.ROLE and value not in _ARIA_ROLES:
            raise ModelValidationError(
                f"browser_locator.value is not a supported ARIA role: {value!r}"
            )
        if self.name is not None:
            name = _text(self.name, "browser_locator.name")
            if len(name.encode("utf-8")) > 1024:
                raise ModelValidationError(
                    "browser_locator.name cannot exceed 1024 UTF-8 bytes"
                )
            object.__setattr__(self, "name", name)
        if self.kind is not BrowserLocatorKind.ROLE and self.name is not None:
            raise ModelValidationError(
                "browser_locator.name is supported only for role locators"
            )
        if not isinstance(self.exact, bool):
            raise ModelValidationError("browser_locator.exact must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BrowserLocator":
        doc = _strict_fields(
            data,
            label="BrowserLocator",
            required={"kind", "value"},
            optional={"name", "exact"},
        )
        return cls(
            kind=doc["kind"],
            value=doc["value"],
            name=doc.get("name"),
            exact=doc.get("exact", True),
        )


_LOCATOR_ONLY_ACTIONS = frozenset(
    {
        AcceptanceAction.CLICK,
        AcceptanceAction.ASSERT_VISIBLE,
        AcceptanceAction.ASSERT_FOCUSED,
    }
)
_LOCATOR_VALUE_ACTIONS = frozenset(
    {
        AcceptanceAction.FILL,
        AcceptanceAction.PRESS,
        AcceptanceAction.ASSERT_TEXT,
        AcceptanceAction.ASSERT_VALUE,
    }
)
_VALUE_ONLY_ACTIONS = frozenset(
    {
        AcceptanceAction.NAVIGATE,
        AcceptanceAction.KEYBOARD,
        AcceptanceAction.ASSERT_URL,
    }
)
_NO_ARGUMENT_ACTIONS = frozenset(
    {AcceptanceAction.OPEN_REQUIREMENT, AcceptanceAction.RELOAD}
)
_ASSERTION_ACTIONS = frozenset(
    {
        AcceptanceAction.ASSERT_VISIBLE,
        AcceptanceAction.ASSERT_FOCUSED,
        AcceptanceAction.ASSERT_TEXT,
        AcceptanceAction.ASSERT_VALUE,
        AcceptanceAction.ASSERT_URL,
    }
)


@dataclass(frozen=True, slots=True)
class AcceptanceStep:
    action: AcceptanceAction
    locator: BrowserLocator | None = None
    value: str | None = None

    def __post_init__(self) -> None:
        action = _enum(self.action, AcceptanceAction, "acceptance_step.action")
        object.__setattr__(self, "action", action)
        locator = self.locator
        if isinstance(locator, Mapping):
            locator = BrowserLocator.from_dict(locator)
            object.__setattr__(self, "locator", locator)
        if locator is not None and not isinstance(locator, BrowserLocator):
            raise ModelValidationError(
                "acceptance_step.locator must be a BrowserLocator"
            )
        value = self.value
        if value is not None:
            value = _text(value, "acceptance_step.value")
            if len(value.encode("utf-8")) > 4096:
                raise ModelValidationError(
                    "acceptance_step.value cannot exceed 4096 UTF-8 bytes"
                )
            object.__setattr__(self, "value", value)
        if action in _LOCATOR_ONLY_ACTIONS:
            valid = locator is not None and value is None
        elif action in _LOCATOR_VALUE_ACTIONS:
            valid = locator is not None and value is not None
        elif action in _VALUE_ONLY_ACTIONS:
            valid = locator is None and value is not None
        else:
            valid = action in _NO_ARGUMENT_ACTIONS and locator is None and value is None
        if not valid:
            raise ModelValidationError(
                f"acceptance action {action.value!r} has invalid locator/value fields"
            )
        if action in {AcceptanceAction.NAVIGATE, AcceptanceAction.ASSERT_URL}:
            assert value is not None
            if (
                not value.startswith("/")
                or value.startswith("//")
                or "\\" in value
                or "\x00" in value
            ):
                raise ModelValidationError(
                    f"acceptance action {action.value!r} requires a local URL path"
                )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AcceptanceStep":
        doc = _strict_fields(
            data,
            label="AcceptanceStep",
            required={"action"},
            optional={"locator", "value"},
        )
        return cls(
            action=doc["action"],
            locator=doc.get("locator"),
            value=doc.get("value"),
        )


@dataclass(frozen=True, slots=True)
class AcceptanceScenario:
    id: str
    title: str
    when: tuple[str, ...]
    then: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    oracle: tuple[AcceptanceStep, ...]
    given: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "scenario.id"))
        object.__setattr__(self, "title", _text(self.title, "scenario.title"))
        object.__setattr__(
            self, "given", _strings(self.given, "scenario.given", allow_empty=True)
        )
        object.__setattr__(
            self, "when", _strings(self.when, "scenario.when", allow_empty=False)
        )
        object.__setattr__(
            self, "then", _strings(self.then, "scenario.then", allow_empty=False)
        )
        object.__setattr__(
            self,
            "requirement_ids",
            _strings(
                self.requirement_ids,
                "scenario.requirement_ids",
                allow_empty=False,
                stable_ids=True,
            ),
        )
        object.__setattr__(
            self,
            "oracle",
            _models(self.oracle, AcceptanceStep, "scenario.oracle"),
        )
        if not self.oracle:
            raise ModelValidationError("scenario.oracle cannot be empty")
        if len(self.oracle) > 64:
            raise ModelValidationError("scenario.oracle cannot exceed 64 steps")
        if not any(step.action in _ASSERTION_ACTIONS for step in self.oracle):
            raise ModelValidationError(
                "scenario.oracle requires at least one observable assertion"
            )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AcceptanceScenario":
        doc = _strict_fields(
            data,
            label="AcceptanceScenario",
            required={
                "id",
                "title",
                "when",
                "then",
                "requirement_ids",
                "oracle",
            },
            optional={"given"},
        )
        return cls(
            id=doc["id"],
            title=doc["title"],
            given=doc.get("given", ()),
            when=doc["when"],
            then=doc["then"],
            requirement_ids=doc["requirement_ids"],
            oracle=doc["oracle"],
        )


@dataclass(frozen=True, slots=True)
class ProjectSpec:
    id: str
    name: str
    goal: str
    audiences: tuple[str, ...]
    requirements: tuple[Requirement, ...]
    acceptance_scenarios: tuple[AcceptanceScenario, ...]
    constraints: tuple[str, ...] = ()
    revision: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "project.id"))
        object.__setattr__(self, "name", _text(self.name, "project.name"))
        object.__setattr__(self, "goal", _text(self.goal, "project.goal"))
        object.__setattr__(
            self,
            "audiences",
            _strings(self.audiences, "project.audiences", allow_empty=False),
        )
        object.__setattr__(
            self,
            "requirements",
            _models(self.requirements, Requirement, "project.requirements"),
        )
        object.__setattr__(
            self,
            "acceptance_scenarios",
            _models(
                self.acceptance_scenarios,
                AcceptanceScenario,
                "project.acceptance_scenarios",
            ),
        )
        object.__setattr__(
            self,
            "constraints",
            _strings(self.constraints, "project.constraints", allow_empty=True),
        )
        object.__setattr__(
            self, "revision", _positive_revision(self.revision, "project.revision")
        )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "project.metadata")
        )
        self.validate()

    @property
    def requirement_index(self) -> dict[str, Requirement]:
        return {requirement.id: requirement for requirement in self.requirements}

    @property
    def scenario_index(self) -> dict[str, AcceptanceScenario]:
        return {scenario.id: scenario for scenario in self.acceptance_scenarios}

    def validate(self) -> None:
        requirements = _unique_by_id(self.requirements, "project.requirements")
        scenarios = _unique_by_id(
            self.acceptance_scenarios, "project.acceptance_scenarios"
        )
        if not requirements:
            raise ModelValidationError("project.requirements cannot be empty")
        if not scenarios:
            raise ModelValidationError("project.acceptance_scenarios cannot be empty")

        referenced: set[str] = set()
        for scenario in scenarios.values():
            unknown = set(scenario.requirement_ids) - requirements.keys()
            if unknown:
                raise ModelValidationError(
                    f"scenario {scenario.id!r} references unknown requirements: "
                    f"{sorted(unknown)}"
                )
            referenced.update(scenario.requirement_ids)
        uncovered = requirements.keys() - referenced
        if uncovered:
            raise ModelValidationError(
                "every requirement needs an executable acceptance scenario; "
                f"uncovered requirements: {sorted(uncovered)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProjectSpec":
        doc = _strict_fields(
            data,
            label="ProjectSpec",
            required={
                "schema_version",
                "id",
                "name",
                "goal",
                "audiences",
                "requirements",
                "acceptance_scenarios",
            },
            optional={"constraints", "revision", "metadata"},
        )
        _check_schema_version(doc, "ProjectSpec")
        return cls(
            id=doc["id"],
            name=doc["name"],
            goal=doc["goal"],
            audiences=doc["audiences"],
            requirements=doc["requirements"],
            acceptance_scenarios=doc["acceptance_scenarios"],
            constraints=doc.get("constraints", ()),
            revision=doc.get("revision", 1),
            metadata=doc.get("metadata", {}),
        )


