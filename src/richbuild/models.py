"""Typed, versioned domain models for the RICH control plane.

The module intentionally depends only on the Python standard library.  These
models are the boundary between product intent, the architecture compiler, and
durable execution state.  They therefore validate eagerly, serialize to plain
JSON-compatible dictionaries, and reject unknown fields when loading.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import Enum
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any, Iterable, Mapping, Sequence, TypeVar


SCHEMA_VERSION = "2.0"
_STABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class ModelValidationError(ValueError):
    """A v2 document is structurally invalid or internally inconsistent."""


class UnsupportedSchemaVersion(ModelValidationError):
    """A serialized document uses a schema version this module cannot load."""


class _StringEnum(str, Enum):
    """Enum whose values serialize as stable lowercase strings."""


class RequirementKind(_StringEnum):
    FUNCTIONAL = "functional"
    NON_FUNCTIONAL = "non_functional"
    CONSTRAINT = "constraint"


class RequirementPriority(_StringEnum):
    MUST = "must"
    SHOULD = "should"
    COULD = "could"


class BrowserLocatorKind(_StringEnum):
    ROLE = "role"
    LABEL = "label"
    TEXT = "text"
    TEST_ID = "test_id"
    PLACEHOLDER = "placeholder"


_ARIA_ROLES = frozenset(
    {
        "alert",
        "alertdialog",
        "application",
        "article",
        "banner",
        "blockquote",
        "button",
        "caption",
        "cell",
        "checkbox",
        "code",
        "columnheader",
        "combobox",
        "complementary",
        "contentinfo",
        "definition",
        "deletion",
        "dialog",
        "directory",
        "document",
        "emphasis",
        "feed",
        "figure",
        "form",
        "generic",
        "grid",
        "gridcell",
        "group",
        "heading",
        "img",
        "insertion",
        "link",
        "list",
        "listbox",
        "listitem",
        "log",
        "main",
        "marquee",
        "math",
        "meter",
        "menu",
        "menubar",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "navigation",
        "none",
        "note",
        "option",
        "paragraph",
        "presentation",
        "progressbar",
        "radio",
        "radiogroup",
        "region",
        "row",
        "rowgroup",
        "rowheader",
        "scrollbar",
        "search",
        "searchbox",
        "separator",
        "slider",
        "spinbutton",
        "status",
        "strong",
        "subscript",
        "superscript",
        "switch",
        "tab",
        "table",
        "tablist",
        "tabpanel",
        "term",
        "textbox",
        "time",
        "timer",
        "toolbar",
        "tooltip",
        "tree",
        "treegrid",
        "treeitem",
    }
)


class AcceptanceAction(_StringEnum):
    OPEN_REQUIREMENT = "open_requirement"
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    PRESS = "press"
    KEYBOARD = "keyboard"
    RELOAD = "reload"
    ASSERT_VISIBLE = "assert_visible"
    ASSERT_FOCUSED = "assert_focused"
    ASSERT_TEXT = "assert_text"
    ASSERT_VALUE = "assert_value"
    ASSERT_URL = "assert_url"


class NodeKind(_StringEnum):
    APPLICATION = "application"
    SERVICE = "service"
    MODULE = "module"
    DOMAIN = "domain"
    UI = "ui"
    DATA = "data"
    ADAPTER = "adapter"
    WORKFLOW = "workflow"
    RESOURCE = "resource"


class EdgeKind(_StringEnum):
    CONTAINS = "contains"
    CALL = "call"
    DATA = "data"
    CAPABILITY = "capability"
    EVENT = "event"
    RESOURCE = "resource"
    SCHEMA = "schema"


class PortDirection(_StringEnum):
    INPUT = "input"
    OUTPUT = "output"


class RunStatus(_StringEnum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    READY = "ready"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    BLOCKED = "blocked"


class TaskKind(_StringEnum):
    PLAN = "plan"
    SCAFFOLD = "scaffold"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    PACKAGE = "package"
    DEPLOY = "deploy"


class TaskStatus(_StringEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CACHED = "cached"
    CANCELED = "canceled"
    BLOCKED = "blocked"


class EvidenceKind(_StringEnum):
    GENERATION = "generation"
    EXECUTION = "execution"
    SCHEMA = "schema"
    STATIC = "static"
    LINT = "lint"
    UNIT = "unit"
    BUILD = "build"
    PROPERTY = "property"
    COMPOSITION = "composition"
    ACCEPTANCE = "acceptance"
    ADVERSARIAL = "adversarial"
    PREVIEW = "preview"


class EvidenceStatus(_StringEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class ApprovalGate(_StringEnum):
    PRODUCT_SPEC = "product_spec"
    ARCHITECTURE = "architecture"
    AUTHORITY_EXPANSION = "authority_expansion"
    PREVIEW_DEPLOYMENT = "preview_deployment"
    EXPORT = "export"


class ApprovalStatus(_StringEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELED = "canceled"


class ArtifactKind(_StringEnum):
    PRODUCT_SPEC = "product_spec"
    ARCHITECTURE = "architecture"
    CONTRACT = "contract"
    SOURCE = "source"
    TEST = "test"
    LOG = "log"
    REPORT = "report"
    BUNDLE = "bundle"
    PREVIEW = "preview"


class ArtifactStatus(_StringEnum):
    PENDING = "pending"
    AVAILABLE = "available"
    QUARANTINED = "quarantined"
    EXPIRED = "expired"
    DELETED = "deleted"


class ValueTypeKind(_StringEnum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    STRING = "string"
    ENUM = "enum"
    LIST = "list"
    RECORD = "record"
    OPTIONAL = "optional"


class CharSet(_StringEnum):
    """Named, closed character sets.  Deliberately not regular expressions.

    A regex denotes an unbounded language that a value generator, a JSON Schema
    validator and a proof assistant would each interpret slightly differently.
    A named set has exactly one definition -- ``_CHAR_SET_ALPHABETS`` below --
    that every consumer reads from the same place.
    """

    ASCII_DIGITS = "ascii_digits"
    ASCII_LETTERS = "ascii_letters"
    ASCII_ALPHANUMERIC = "ascii_alphanumeric"
    ASCII_IDENTIFIER = "ascii_identifier"
    ASCII_SLUG = "ascii_slug"
    ASCII_PRINTABLE = "ascii_printable"
    UNICODE_SAMPLE = "unicode_sample"

    @property
    def alphabet(self) -> str:
        return _CHAR_SET_ALPHABETS[self]


_ASCII_DIGITS = "0123456789"
_ASCII_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_CHAR_SET_ALPHABETS: dict[CharSet, str] = {
    CharSet.ASCII_DIGITS: _ASCII_DIGITS,
    CharSet.ASCII_LETTERS: _ASCII_LETTERS,
    CharSet.ASCII_ALPHANUMERIC: _ASCII_LETTERS + _ASCII_DIGITS,
    CharSet.ASCII_IDENTIFIER: _ASCII_LETTERS + _ASCII_DIGITS + "_",
    # Identifiers in running software are hyphenated far more often than not --
    # slugs, uuids, "task-1". Without this the only way to type such a field
    # was to widen it to all printable ASCII, which throws away the constraint
    # that made it worth typing.
    CharSet.ASCII_SLUG: _ASCII_LETTERS + _ASCII_DIGITS + "-_",
    CharSet.ASCII_PRINTABLE: "".join(chr(code) for code in range(32, 127)),
    # A deliberately small curated set rather than "all of Unicode": it must be
    # finite for a generator to draw from, and every member here exercises a
    # distinct encoding hazard -- combining marks, astral-plane surrogates,
    # right-to-left embedding, and CJK width.
    CharSet.UNICODE_SAMPLE: (
        _ASCII_LETTERS + _ASCII_DIGITS + " " + "éüñçå" + "日本語中文" + "🙂🚀" + "עברית"
    ),
}


class ObligationRelation(_StringEnum):
    """The point-free relations a proof obligation may assert.

    Each is chosen because it names a real defect class, compiles to a
    one-line property test, and compiles to a one-line theorem statement.  The
    bodies are built only from operation application, composition and equality,
    so there is no expression language to specify, evaluate or embed.

    Deliberately absent: COMMUTATIVE, INVOLUTIVE, MONOTONE and DETERMINISTIC.
    The enum extends without schema churn, and shipping relations no compiler
    consumes would repeat the mistake ``EvidenceKind`` already made.
    """

    EXAMPLE = "example"
    TOTAL = "total"
    ROUND_TRIP = "round_trip"
    IDEMPOTENT = "idempotent"
    PRESERVES = "preserves"
    ESTABLISHES = "establishes"


class ObligationTier(_StringEnum):
    """How strongly an obligation is discharged.

    ``SAMPLE`` checks finitely many inputs; ``PROOF`` covers the whole domain.
    There is deliberately no third "it typechecks" tier: typechecking is a
    property of the implementation, not a claim about a relation, and admitting
    it here would let a run report obligations as satisfied by having written
    them down.
    """

    SAMPLE = "sample"
    PROOF = "proof"


EnumT = TypeVar("EnumT", bound=Enum)


def _stable_id(value: str, label: str = "id") -> str:
    if not isinstance(value, str) or not _STABLE_ID_RE.fullmatch(value):
        raise ModelValidationError(
            f"{label} must be a stable 1-128 character identifier containing only "
            "letters, numbers, '.', '_', ':', '/', or '-'"
        )
    return value


def _text(value: str, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ModelValidationError(f"{label} must be a string")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ModelValidationError(f"{label} cannot be empty")
    return normalized


def _positive_revision(value: int, label: str = "revision") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ModelValidationError(f"{label} must be a positive integer")
    return value


def _non_negative_int(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelValidationError(f"{label} must be a non-negative integer")
    return value


def _strings(
    value: Iterable[str],
    label: str,
    *,
    allow_empty: bool = True,
    stable_ids: bool = False,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ModelValidationError(f"{label} must be a sequence")
    result = tuple(value)
    normalized: list[str] = []
    for index, item in enumerate(result):
        if stable_ids:
            normalized.append(_stable_id(item, f"{label}[{index}]"))
        else:
            normalized.append(_text(item, f"{label}[{index}]"))
    if not allow_empty and not normalized:
        raise ModelValidationError(f"{label} cannot be empty")
    if len(set(normalized)) != len(normalized):
        raise ModelValidationError(f"{label} cannot contain duplicates")
    return tuple(normalized)


def _enum(value: Any, enum_type: type[EnumT], label: str) -> EnumT:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        choices = ", ".join(member.value for member in enum_type)
        raise ModelValidationError(f"{label} must be one of: {choices}") from exc


def _json_value(value: Any, label: str) -> Any:
    """Return a detached JSON-compatible value, rejecting lossy coercions."""

    def validate(item: Any, path: str) -> None:
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ModelValidationError(f"{path} contains a non-finite number")
            return
        if isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                validate(nested, f"{path}[{index}]")
            return
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise ModelValidationError(f"{path} has a non-string object key")
                validate(nested, f"{path}.{key}")
            return
        raise ModelValidationError(
            f"{path} contains non-JSON value of type {type(item).__name__}"
        )

    validate(value, label)
    return json.loads(json.dumps(value, ensure_ascii=False))


def _json_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelValidationError(f"{label} must be an object")
    detached = _json_value(value, label)
    if not isinstance(detached, dict):
        raise ModelValidationError(f"{label} must be an object")
    return detached


def _serialized(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        # A model may declare which of its fields carry information. Types in
        # particular have many mutually exclusive slots, and emitting the empty
        # ones quadruples a contract for no reader's benefit. from_dict treats
        # every omitted slot as its default, so this stays lossless.
        selected = getattr(value, "_serialized_field_names", None)
        names = (
            selected() if selected is not None else [item.name for item in fields(value)]
        )
        return {name: _serialized(getattr(value, name)) for name in names}
    if isinstance(value, tuple):
        return [_serialized(item) for item in value]
    if isinstance(value, list):
        return [_serialized(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _serialized(item) for key, item in value.items()}
    return value


def _mapping(data: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ModelValidationError(f"{label} must be an object")
    return dict(data)


def _strict_fields(
    data: Mapping[str, Any],
    *,
    label: str,
    required: set[str],
    optional: set[str] = frozenset(),
) -> dict[str, Any]:
    document = _mapping(data, label)
    missing = required - document.keys()
    if missing:
        raise ModelValidationError(f"{label} is missing fields: {sorted(missing)}")
    unknown = document.keys() - required - optional
    if unknown:
        raise ModelValidationError(f"{label} has unknown fields: {sorted(unknown)}")
    return document


def _check_schema_version(data: Mapping[str, Any], label: str) -> None:
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"{label} schema_version must be {SCHEMA_VERSION!r}, got {version!r}"
        )


def _models(
    value: Iterable[Any],
    model_type: type[Any],
    label: str,
) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise ModelValidationError(f"{label} must be a sequence")
    result = []
    for index, item in enumerate(value):
        if isinstance(item, model_type):
            result.append(item)
        elif isinstance(item, Mapping):
            result.append(model_type.from_dict(item))
        else:
            raise ModelValidationError(
                f"{label}[{index}] must be {model_type.__name__} or an object"
            )
    return tuple(result)


def _unique_by_id(items: Iterable[Any], label: str) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for item in items:
        if item.id in index:
            raise ModelValidationError(f"{label} contains duplicate id {item.id!r}")
        index[item.id] = item
    return index


def _relative_owned_path(value: str, label: str) -> str:
    # Deliberately not delegating to paths.safe_relative_path, which every
    # other caller in the package now shares: models.py is the bottom of the
    # v2 layering and imports no sibling module, and a domain vocabulary that
    # reaches sideways for a helper stops being one. The rules below are the
    # same rules, applied to a path that is being *declared* in a spec rather
    # than written to a disk.
    normalized = _text(value, label)
    if "\\" in normalized:
        raise ModelValidationError(f"{label} must use POSIX '/' separators")
    path = PurePosixPath(normalized)
    if path.is_absolute() or normalized in {".", ".."} or ".." in path.parts:
        raise ModelValidationError(f"{label} must be a normalized relative path")
    if str(path) != normalized:
        raise ModelValidationError(f"{label} must be a normalized relative path")
    return normalized


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


@dataclass(frozen=True, slots=True)
class ErrorContract:
    id: str
    code: str
    description: str
    schema: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "error.id"))
        object.__setattr__(self, "code", _stable_id(self.code, "error.code"))
        object.__setattr__(
            self, "description", _text(self.description, "error.description")
        )
        object.__setattr__(
            self, "schema", _json_mapping(self.schema, "error.schema")
        )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ErrorContract":
        doc = _strict_fields(
            data,
            label="ErrorContract",
            required={"id", "code", "description"},
            optional={"schema"},
        )
        return cls(
            id=doc["id"],
            code=doc["code"],
            description=doc["description"],
            schema=doc.get("schema", {}),
        )


MAX_VALUE_TYPE_DEPTH = 4
MAX_RECORD_FIELDS = 24
MAX_ENUM_MEMBERS = 64
# Bounds sampling, not expressiveness. 4 KiB refused to describe rendered
# markup, which a UI operation genuinely returns; 64 KiB still keeps a sampled
# draw cheap and keeps every domain finite.
MAX_VALUE_LENGTH = 65_536
# Above this, "how many values inhabit this type" stops being a useful
# question: no gate enumerates a domain that large, and computing the exact
# integer costs more than the answer is worth.
_MAX_CARDINALITY_BOUND = 1 << 32
# Applied when a derived type is still unbounded. Bounds exist so a generator
# has somewhere to draw from; any finite value serves, and these are wide
# enough not to distort what the type means.
DEFAULT_SAMPLED_LENGTH = 256
DEFAULT_SAMPLED_ITEMS = 8
DEFAULT_SAMPLED_INTEGER = 1_000_000
# The intersection of what a JSON object key, a TypeScript property and a Lean
# structure field all accept without quoting.
_FIELD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class RecordField:
    name: str
    value_type: "ValueType"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _FIELD_NAME_RE.fullmatch(self.name):
            raise ModelValidationError(
                "record_field.name must be a 1-64 character identifier starting "
                "with a letter and containing only letters, digits or '_'"
            )
        value_type = self.value_type
        if isinstance(value_type, Mapping):
            value_type = ValueType.from_dict(value_type)
            object.__setattr__(self, "value_type", value_type)
        if not isinstance(value_type, ValueType):
            raise ModelValidationError("record_field.value_type must be a ValueType")

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RecordField":
        doc = _strict_fields(
            data, label="RecordField", required={"name", "value_type"}
        )
        return cls(name=doc["name"], value_type=doc["value_type"])


# Which optional slots each kind may carry.  Every slot outside a kind's set
# must be left at its empty value, so a malformed type is rejected at
# construction rather than reinterpreted by whichever compiler reads it first.
_VALUE_TYPE_SLOTS: dict[ValueTypeKind, frozenset[str]] = {
    ValueTypeKind.BOOLEAN: frozenset(),
    ValueTypeKind.INTEGER: frozenset({"minimum", "maximum"}),
    ValueTypeKind.STRING: frozenset({"min_length", "max_length", "char_set"}),
    ValueTypeKind.ENUM: frozenset({"members"}),
    ValueTypeKind.LIST: frozenset({"min_length", "max_length", "element"}),
    ValueTypeKind.RECORD: frozenset({"record_fields"}),
    ValueTypeKind.OPTIONAL: frozenset({"element"}),
}
_VALUE_TYPE_REQUIRED_SLOTS: dict[ValueTypeKind, frozenset[str]] = {
    ValueTypeKind.ENUM: frozenset({"members"}),
    ValueTypeKind.LIST: frozenset({"element"}),
    ValueTypeKind.RECORD: frozenset({"record_fields"}),
    ValueTypeKind.OPTIONAL: frozenset({"element"}),
}
_VALUE_TYPE_EMPTY: dict[str, Any] = {
    "minimum": None,
    "maximum": None,
    "min_length": None,
    "max_length": None,
    "char_set": None,
    "members": (),
    "element": None,
    "record_fields": (),
}


@dataclass(frozen=True, slots=True)
class ValueType:
    """A closed type language for operation inputs and outputs.

    JSON Schema is expressive enough to describe almost anything and therefore
    cannot be quantified over: you cannot compile ``forall x`` against a schema
    without a nameable type, nor sample from it without a generatable domain.
    This language is small on purpose -- seven kinds, bounded nesting, no
    regular expressions, no recursion -- because every construct here has to
    have one meaning in a property test and the same meaning in a theorem.

    Bounded nesting is also what keeps the type describable to a
    structured-output model later: a recursive schema cannot be expressed, but
    a depth-4 one can be unrolled.
    """

    kind: ValueTypeKind
    minimum: int | None = None
    maximum: int | None = None
    min_length: int | None = None
    max_length: int | None = None
    char_set: CharSet | None = None
    members: tuple[str, ...] = ()
    element: "ValueType | None" = None
    record_fields: tuple[RecordField, ...] = ()

    def __post_init__(self) -> None:
        kind = _enum(self.kind, ValueTypeKind, "value_type.kind")
        object.__setattr__(self, "kind", kind)
        for name in ("minimum", "maximum"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                raise ModelValidationError(f"value_type.{name} must be an integer")
        for name in ("min_length", "max_length"):
            value = getattr(self, name)
            if value is not None:
                bound = _non_negative_int(value, f"value_type.{name}")
                if bound > MAX_VALUE_LENGTH:
                    raise ModelValidationError(
                        f"value_type.{name} cannot exceed {MAX_VALUE_LENGTH}"
                    )
                object.__setattr__(self, name, bound)
        if self.char_set is not None:
            object.__setattr__(
                self, "char_set", _enum(self.char_set, CharSet, "value_type.char_set")
            )
        object.__setattr__(
            self, "members", _strings(self.members, "value_type.members")
        )
        element = self.element
        if isinstance(element, Mapping):
            element = ValueType.from_dict(element)
            object.__setattr__(self, "element", element)
        if element is not None and not isinstance(element, ValueType):
            raise ModelValidationError("value_type.element must be a ValueType")
        object.__setattr__(
            self,
            "record_fields",
            _models(self.record_fields, RecordField, "value_type.record_fields"),
        )

        allowed = _VALUE_TYPE_SLOTS[kind]
        for name, empty in _VALUE_TYPE_EMPTY.items():
            if name not in allowed and getattr(self, name) != empty:
                raise ModelValidationError(
                    f"value type {kind.value!r} cannot carry {name!r}"
                )
        for name in _VALUE_TYPE_REQUIRED_SLOTS.get(kind, frozenset()):
            if getattr(self, name) == _VALUE_TYPE_EMPTY[name]:
                raise ModelValidationError(
                    f"value type {kind.value!r} requires {name!r}"
                )

        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ModelValidationError("value_type.minimum cannot exceed maximum")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ModelValidationError("value_type.min_length cannot exceed max_length")
        if kind is ValueTypeKind.ENUM and len(self.members) > MAX_ENUM_MEMBERS:
            raise ModelValidationError(
                f"value_type.members cannot exceed {MAX_ENUM_MEMBERS} entries"
            )
        if kind is ValueTypeKind.RECORD:
            if len(self.record_fields) > MAX_RECORD_FIELDS:
                raise ModelValidationError(
                    f"value_type.record_fields cannot exceed {MAX_RECORD_FIELDS}"
                )
            names = [record_field.name for record_field in self.record_fields]
            if len(set(names)) != len(names):
                raise ModelValidationError(
                    "value_type.record_fields contains duplicate names"
                )
        if (
            kind is ValueTypeKind.OPTIONAL
            and element is not None
            and element.kind is ValueTypeKind.OPTIONAL
        ):
            # An optional optional has no distinct inhabitant in JSON, in
            # TypeScript, or in the sampler.  Reject the ambiguity outright.
            raise ModelValidationError("value_type 'optional' cannot nest an optional")
        if self.depth > MAX_VALUE_TYPE_DEPTH:
            raise ModelValidationError(
                f"value type nesting cannot exceed depth {MAX_VALUE_TYPE_DEPTH}"
            )

    @property
    def depth(self) -> int:
        if self.element is not None:
            return 1 + self.element.depth
        if self.record_fields:
            return 1 + max(
                record_field.value_type.depth for record_field in self.record_fields
            )
        return 1

    @property
    def is_finitely_sampleable(self) -> bool:
        """Whether a generator can draw a value from this domain.

        Distinct from ``cardinality_bound``: a bounded string over a 95
        character alphabet is sampleable but nowhere near enumerable.
        """

        kind = self.kind
        if kind in (ValueTypeKind.BOOLEAN, ValueTypeKind.ENUM):
            return True
        if kind is ValueTypeKind.INTEGER:
            return self.minimum is not None and self.maximum is not None
        if kind is ValueTypeKind.STRING:
            return self.max_length is not None
        if kind is ValueTypeKind.LIST:
            assert self.element is not None
            return self.max_length is not None and self.element.is_finitely_sampleable
        if kind is ValueTypeKind.OPTIONAL:
            assert self.element is not None
            return self.element.is_finitely_sampleable
        return all(
            record_field.value_type.is_finitely_sampleable
            for record_field in self.record_fields
        )

    @property
    def cardinality_bound(self) -> int | None:
        """Distinct inhabitants, or ``None`` when unbounded or impractically large."""

        kind = self.kind
        if kind is ValueTypeKind.BOOLEAN:
            return 2
        if kind is ValueTypeKind.ENUM:
            return len(self.members)
        if kind is ValueTypeKind.INTEGER:
            if self.minimum is None or self.maximum is None:
                return None
            return _bounded(self.maximum - self.minimum + 1)
        if kind is ValueTypeKind.STRING:
            if self.max_length is None or self.char_set is None:
                return None
            alphabet = len(self.char_set.alphabet)
            total = 0
            for length in range(self.min_length or 0, self.max_length + 1):
                total += alphabet**length
                if total > _MAX_CARDINALITY_BOUND:
                    return None
            return _bounded(total)
        if kind is ValueTypeKind.OPTIONAL:
            assert self.element is not None
            inner = self.element.cardinality_bound
            return None if inner is None else _bounded(inner + 1)
        if kind is ValueTypeKind.LIST:
            assert self.element is not None
            inner = self.element.cardinality_bound
            if inner is None or self.max_length is None:
                return None
            total = 0
            for length in range(self.min_length or 0, self.max_length + 1):
                total += inner**length
                if total > _MAX_CARDINALITY_BOUND:
                    return None
            return _bounded(total)
        total = 1
        for record_field in self.record_fields:
            inner = record_field.value_type.cardinality_bound
            if inner is None:
                return None
            total *= inner
            if total > _MAX_CARDINALITY_BOUND:
                return None
        return _bounded(total)

    def accepts(self, value: Any) -> bool:
        """Whether a JSON value inhabits this type."""

        return self.explain(value) is None

    def explain(self, value: Any, path: str = "value") -> str | None:
        """Say precisely why a value does not inhabit this type, or ``None``.

        ``accepts`` answers yes or no, which is enough to reject and useless
        for repair. A rejection is read by a person correcting a contract and
        by a model retrying against the validator's own message, and neither
        can fix "does not inhabit the subject input type". Both can fix
        "value.dueDate is 11 characters, over the maximum of 8".
        """

        kind = self.kind
        if kind is ValueTypeKind.BOOLEAN:
            if not isinstance(value, bool):
                return f"{path} must be a boolean, got {_type_name(value)}"
            return None
        if kind is ValueTypeKind.INTEGER:
            if isinstance(value, bool) or not isinstance(value, int):
                return f"{path} must be an integer, got {_type_name(value)}"
            if self.minimum is not None and value < self.minimum:
                return f"{path} is {value}, below the minimum of {self.minimum}"
            if self.maximum is not None and value > self.maximum:
                return f"{path} is {value}, above the maximum of {self.maximum}"
            return None
        if kind is ValueTypeKind.STRING:
            if not isinstance(value, str):
                return f"{path} must be a string, got {_type_name(value)}"
            if self.min_length is not None and len(value) < self.min_length:
                return (
                    f"{path} is {len(value)} characters, under the minimum of "
                    f"{self.min_length}"
                )
            if self.max_length is not None and len(value) > self.max_length:
                return (
                    f"{path} is {len(value)} characters, over the maximum of "
                    f"{self.max_length}"
                )
            if self.char_set is not None:
                stray = sorted(set(value) - set(self.char_set.alphabet))
                if stray:
                    return (
                        f"{path} contains characters outside the "
                        f"{self.char_set.value!r} character set: "
                        f"{''.join(stray)!r}"
                    )
            return None
        if kind is ValueTypeKind.ENUM:
            if not isinstance(value, str) or value not in self.members:
                return (
                    f"{path} must be one of {list(self.members)}, got "
                    f"{value!r}"
                )
            return None
        if kind is ValueTypeKind.OPTIONAL:
            assert self.element is not None
            if value is None:
                return None
            return self.element.explain(value, path)
        if kind is ValueTypeKind.LIST:
            assert self.element is not None
            if not isinstance(value, list):
                return f"{path} must be a list, got {_type_name(value)}"
            if self.min_length is not None and len(value) < self.min_length:
                return (
                    f"{path} has {len(value)} items, under the minimum of "
                    f"{self.min_length}"
                )
            if self.max_length is not None and len(value) > self.max_length:
                return (
                    f"{path} has {len(value)} items, over the maximum of "
                    f"{self.max_length}"
                )
            for index, item in enumerate(value):
                reason = self.element.explain(item, f"{path}[{index}]")
                if reason is not None:
                    return reason
            return None
        if not isinstance(value, Mapping):
            return f"{path} must be an object, got {_type_name(value)}"
        expected = {record_field.name for record_field in self.record_fields}
        present = set(value.keys())
        missing = sorted(expected - present)
        if missing:
            return f"{path} is missing required fields: {missing}"
        unexpected = sorted(present - expected)
        if unexpected:
            return (
                f"{path} has fields the type does not declare: {unexpected}; "
                f"declared fields are {sorted(expected)}"
            )
        for record_field in self.record_fields:
            reason = record_field.value_type.explain(
                value[record_field.name], f"{path}.{record_field.name}"
            )
            if reason is not None:
                return reason
        return None

    def fitted_to(self, values: Iterable[Any]) -> "ValueType":
        """Return a type that admits every one of these values and is sampleable.

        A declared bound and a declared example are two guesses at the same
        intent, made minutes apart, and when they disagree it is the example
        that carries the meaning -- the author was showing what the field looks
        like. Measured over six live proposals, five failed because a bound or
        character set refused an example the same answer supplied: ids written
        ``"task-1"`` against a set with no hyphen, a uuid one character under a
        declared minimum. None was a design mistake.

        So bounds are derived here rather than demanded up front. Widening is
        the only direction taken -- a type never becomes narrower than declared
        -- and anything still unbounded afterwards gets a sampling default,
        because an unbounded domain is one no property gate can draw from.
        """

        samples = [value for value in values]
        kind = self.kind
        if kind is ValueTypeKind.INTEGER:
            numbers = [
                value
                for value in samples
                if isinstance(value, int) and not isinstance(value, bool)
            ]
            low = min([*numbers, self.minimum if self.minimum is not None else 0])
            high = max([*numbers, self.maximum if self.maximum is not None else 0])
            return replace(
                self,
                minimum=min(low, -DEFAULT_SAMPLED_INTEGER)
                if self.minimum is None
                else min(low, self.minimum),
                maximum=max(high, DEFAULT_SAMPLED_INTEGER)
                if self.maximum is None
                else max(high, self.maximum),
            )
        if kind is ValueTypeKind.STRING:
            texts = [value for value in samples if isinstance(value, str)]
            longest = max([len(text) for text in texts] + [0])
            shortest = min([len(text) for text in texts] + [longest])
            max_length = max(
                longest, self.max_length or 0, DEFAULT_SAMPLED_LENGTH
            )
            min_length = (
                min(self.min_length, shortest)
                if self.min_length is not None
                else None
            )
            return replace(
                self,
                min_length=min_length,
                max_length=min(max_length, MAX_VALUE_LENGTH),
                char_set=_narrowest_char_set(texts, self.char_set),
            )
        if kind is ValueTypeKind.LIST:
            assert self.element is not None
            lists = [value for value in samples if isinstance(value, list)]
            longest = max([len(item) for item in lists] + [0])
            items = [entry for item in lists for entry in item]
            return replace(
                self,
                max_length=min(
                    max(longest, self.max_length or 0, DEFAULT_SAMPLED_ITEMS),
                    MAX_VALUE_LENGTH,
                ),
                element=self.element.fitted_to(items),
            )
        if kind is ValueTypeKind.OPTIONAL:
            assert self.element is not None
            return replace(
                self,
                element=self.element.fitted_to(
                    [value for value in samples if value is not None]
                ),
            )
        if kind is ValueTypeKind.RECORD:
            return replace(
                self,
                record_fields=tuple(
                    RecordField(
                        name=record_field.name,
                        value_type=record_field.value_type.fitted_to(
                            [
                                value[record_field.name]
                                for value in samples
                                if isinstance(value, Mapping)
                                and record_field.name in value
                            ]
                        ),
                    )
                    for record_field in self.record_fields
                ),
            )
        # boolean and enum carry no bounds; an enum's members are the author's
        # own vocabulary and widening them would invent product meaning.
        return self

    def json_schema(self) -> dict[str, Any]:
        """Project this type onto JSON Schema.

        The projection is deliberately lossy in one place: ``char_set`` has no
        standard JSON Schema spelling that is not a regular expression, so it
        is dropped.  This type, not the schema, is the source of truth; the
        schema exists so contracts stay readable to humans and to a model.
        """

        kind = self.kind
        if kind is ValueTypeKind.BOOLEAN:
            return {"type": "boolean"}
        if kind is ValueTypeKind.INTEGER:
            schema: dict[str, Any] = {"type": "integer"}
            if self.minimum is not None:
                schema["minimum"] = self.minimum
            if self.maximum is not None:
                schema["maximum"] = self.maximum
            return schema
        if kind is ValueTypeKind.STRING:
            schema = {"type": "string"}
            if self.min_length is not None:
                schema["minLength"] = self.min_length
            if self.max_length is not None:
                schema["maxLength"] = self.max_length
            return schema
        if kind is ValueTypeKind.ENUM:
            return {"type": "string", "enum": list(self.members)}
        if kind is ValueTypeKind.OPTIONAL:
            assert self.element is not None
            return {"anyOf": [self.element.json_schema(), {"type": "null"}]}
        if kind is ValueTypeKind.LIST:
            assert self.element is not None
            schema = {"type": "array", "items": self.element.json_schema()}
            if self.min_length is not None:
                schema["minItems"] = self.min_length
            if self.max_length is not None:
                schema["maxItems"] = self.max_length
            return schema
        return {
            "type": "object",
            "properties": {
                record_field.name: record_field.value_type.json_schema()
                for record_field in self.record_fields
            },
            "required": [
                record_field.name for record_field in self.record_fields
            ],
            "additionalProperties": False,
        }

    def _serialized_field_names(self) -> list[str]:
        # Declaration order, not set order: the architecture document is
        # content-hashed, so this has to be byte-stable across processes.
        allowed = _VALUE_TYPE_SLOTS[self.kind]
        return [
            item.name
            for item in fields(self)
            if item.name == "kind"
            or (
                item.name in allowed
                and getattr(self, item.name) != _VALUE_TYPE_EMPTY[item.name]
            )
        ]

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ValueType":
        doc = _strict_fields(
            data,
            label="ValueType",
            required={"kind"},
            optional={
                "minimum",
                "maximum",
                "min_length",
                "max_length",
                "char_set",
                "members",
                "element",
                "record_fields",
            },
        )
        return cls(
            kind=doc["kind"],
            minimum=doc.get("minimum"),
            maximum=doc.get("maximum"),
            min_length=doc.get("min_length"),
            max_length=doc.get("max_length"),
            char_set=doc.get("char_set"),
            members=doc.get("members", ()),
            element=doc.get("element"),
            record_fields=doc.get("record_fields", ()),
        )


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    return {
        bool: "a boolean",
        int: "an integer",
        float: "a number",
        str: "a string",
        list: "a list",
        dict: "an object",
    }.get(type(value), type(value).__name__)


# Narrowest first. A derived set should say as much as the evidence supports:
# picking `ascii_printable` for a field whose every example is digits throws
# away a constraint worth keeping.
_CHAR_SET_PREFERENCE = (
    CharSet.ASCII_DIGITS,
    CharSet.ASCII_LETTERS,
    CharSet.ASCII_ALPHANUMERIC,
    CharSet.ASCII_IDENTIFIER,
    CharSet.ASCII_SLUG,
    CharSet.ASCII_PRINTABLE,
    CharSet.UNICODE_SAMPLE,
)


def _narrowest_char_set(
    texts: Sequence[str], declared: CharSet | None
) -> CharSet | None:
    """The tightest named set admitting every example, or None if none does.

    ``None`` is a legitimate answer, not a failure: an unconstrained string is
    still finitely sampleable once it has a length bound, and inventing a set
    that excluded the author's own example is exactly the bug this replaces.
    """

    if not texts:
        return declared
    characters = set("".join(texts))
    for candidate in _CHAR_SET_PREFERENCE:
        if characters <= set(candidate.alphabet):
            return candidate
    return None


def _bounded(total: int) -> int | None:
    return None if total > _MAX_CARDINALITY_BOUND else total


def value_type_request_schema(
    max_depth: int = MAX_VALUE_TYPE_DEPTH,
) -> dict[str, Any]:
    """A JSON Schema for *asking a model* to describe a type.

    ``ValueType.json_schema`` projects a type onto the values it admits; this
    describes the type language itself, so a structured-output request can
    return one. The two are opposites and both are needed.

    It is unrolled rather than recursive. Structured-output decoders reject
    recursive schemas, which would make the whole typed vocabulary impossible
    to ask for -- and the bounded nesting depth, which looked like an arbitrary
    limit when it was introduced, is exactly what makes unrolling finite. The
    innermost level offers only the scalar kinds, so the expansion terminates
    instead of bottoming out in something unrepresentable.
    """

    if not isinstance(max_depth, int) or isinstance(max_depth, bool):
        raise ModelValidationError("max_depth must be an integer")
    if not 1 <= max_depth <= MAX_VALUE_TYPE_DEPTH:
        raise ModelValidationError(
            f"max_depth must be between 1 and {MAX_VALUE_TYPE_DEPTH}"
        )
    return _value_type_level(max_depth)


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    # anyOf rather than a type array: it is the spelling every structured-output
    # decoder in use accepts, and an optional slot has to be expressible or the
    # scalar kinds cannot say "no bound".
    return {"anyOf": [schema, {"type": "null"}]}


def _value_type_level(depth: int) -> dict[str, Any]:
    scalar_kinds = ["boolean", "integer", "string", "enum"]
    compound_kinds = ["list", "record", "optional"] if depth > 1 else []
    # Deliberately absent: minimum, maximum, min_length, max_length, char_set.
    # Every one is derived by ``ValueType.fitted_to`` from the examples the
    # same answer supplies, because a declared bound and a declared example are
    # two guesses at one intent and the example is the one carrying meaning.
    # Five of six measured live failures were a bound refusing its own example.
    properties: dict[str, Any] = {
        "kind": {"type": "string", "enum": scalar_kinds + compound_kinds},
        "members": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_ENUM_MEMBERS,
        },
    }
    if depth > 1:
        inner = _value_type_level(depth - 1)
        properties["element"] = _nullable(inner)
        properties["record_fields"] = {
            "type": "array",
            "maxItems": MAX_RECORD_FIELDS,
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "value_type": inner},
                "required": ["name", "value_type"],
                "additionalProperties": False,
            },
        }
    return {
        "type": "object",
        "properties": properties,
        # Only ``kind`` is required. Requiring every slot at every level was
        # measured to cost 164 bytes for a single boolean instead of 19, and a
        # decomposition carrying a dozen nested types overran its output
        # reservation on that alone. ``additionalProperties`` stays false, so
        # the language is still closed -- an answer may omit a slot, never
        # invent one.
        "required": ["kind"],
        "additionalProperties": False,
    }


def value_type_from_request(document: Mapping[str, Any]) -> ValueType:
    """Build a ``ValueType`` from a request-schema answer.

    The request schema requires every slot at every level so a decoder never
    has to choose which keys to emit. That means a scalar arrives carrying
    empty compound slots, which ``ValueType`` rejects outright -- correctly, as
    a boolean with a ``record_fields`` key is not a boolean. Dropping the empty
    ones here is the whole translation.
    """

    if not isinstance(document, Mapping):
        raise ModelValidationError("value type answer must be an object")
    kind = document.get("kind")
    try:
        resolved = ValueTypeKind(kind)
    except (TypeError, ValueError):
        raise ModelValidationError(
            f"value type answer requires a known kind, got {kind!r}"
        ) from None
    # Slots this kind cannot carry are dropped rather than rejected. An answer
    # that names a bound on a boolean has made a bookkeeping slip, not a design
    # one, and refusing the whole proposal over it buys nothing.
    allowed = _VALUE_TYPE_SLOTS[resolved]
    trimmed: dict[str, Any] = {"kind": resolved}
    for name, value in document.items():
        if name == "kind" or name not in allowed or value in (None, [], ()):
            continue
        if name == "element":
            trimmed[name] = value_type_from_request(value)
        elif name == "record_fields":
            if isinstance(value, (str, bytes, Mapping)) or not isinstance(
                value, Iterable
            ):
                continue
            trimmed[name] = tuple(
                RecordField(
                    name=str(entry.get("name", "")),
                    value_type=value_type_from_request(entry.get("value_type", {})),
                )
                for entry in value
                if isinstance(entry, Mapping)
            )
        elif name in ("min_length", "max_length") and isinstance(value, int):
            trimmed[name] = max(0, min(int(value), MAX_VALUE_LENGTH))
        else:
            trimmed[name] = value
    # An inverted range is a transposition, and swapping preserves the intent
    # exactly; refusing it does not.
    for low, high in (("minimum", "maximum"), ("min_length", "max_length")):
        first, second = trimmed.get(low), trimmed.get(high)
        if (
            isinstance(first, int)
            and isinstance(second, int)
            and not isinstance(first, bool)
            and not isinstance(second, bool)
            and first > second
        ):
            trimmed[low], trimmed[high] = second, first
    return ValueType(**trimmed)


def _without_derivable_schemas(operation: "OperationContract") -> dict[str, Any]:
    """Serialize an operation without a schema its declared type already fixes.

    ``__post_init__`` requires the two to agree exactly, so shipping both to a
    reader spends bytes on a value it could compute. The durable document keeps
    both; only this view drops one.
    """

    document = operation.to_dict()
    if operation.input_type is not None:
        document.pop("input_schema", None)
    if operation.output_type is not None:
        document.pop("output_schema", None)
    return document


@dataclass(frozen=True, slots=True)
class OperationContract:
    id: str
    name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    requirement_ids: tuple[str, ...]
    errors: tuple[ErrorContract, ...] = ()
    description: str = ""
    input_type: ValueType | None = None
    output_type: ValueType | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "operation.id"))
        object.__setattr__(self, "name", _stable_id(self.name, "operation.name"))
        object.__setattr__(
            self,
            "description",
            _text(self.description, "operation.description", allow_empty=True),
        )
        object.__setattr__(
            self,
            "input_schema",
            _json_mapping(self.input_schema, "operation.input_schema"),
        )
        object.__setattr__(
            self,
            "output_schema",
            _json_mapping(self.output_schema, "operation.output_schema"),
        )
        object.__setattr__(
            self,
            "requirement_ids",
            _strings(
                self.requirement_ids,
                "operation.requirement_ids",
                allow_empty=False,
                stable_ids=True,
            ),
        )
        object.__setattr__(
            self, "errors", _models(self.errors, ErrorContract, "operation.errors")
        )
        _unique_by_id(self.errors, "operation.errors")
        codes = [error.code for error in self.errors]
        if len(set(codes)) != len(codes):
            raise ModelValidationError("operation.errors contains duplicate codes")
        for slot, schema_name in (
            ("input_type", "input_schema"),
            ("output_type", "output_schema"),
        ):
            declared = getattr(self, slot)
            if declared is None:
                continue
            if isinstance(declared, Mapping):
                declared = ValueType.from_dict(declared)
                object.__setattr__(self, slot, declared)
            if not isinstance(declared, ValueType):
                raise ModelValidationError(f"operation.{slot} must be a ValueType")
            # The schema is a projection of the type, never a second source of
            # truth.  Requiring exact agreement means every existing consumer
            # of input_schema/output_schema keeps working while the typed view
            # becomes the thing the obligation compilers quantify over.
            if declared.json_schema() != getattr(self, schema_name):
                raise ModelValidationError(
                    f"operation.{schema_name} must equal the JSON Schema projection "
                    f"of operation.{slot}"
                )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OperationContract":
        doc = _strict_fields(
            data,
            label="OperationContract",
            required={
                "id",
                "name",
                "input_schema",
                "output_schema",
                "requirement_ids",
            },
            optional={"errors", "description", "input_type", "output_type"},
        )
        return cls(
            id=doc["id"],
            name=doc["name"],
            input_schema=doc["input_schema"],
            output_schema=doc["output_schema"],
            requirement_ids=doc["requirement_ids"],
            errors=doc.get("errors", ()),
            description=doc.get("description", ""),
            input_type=doc.get("input_type"),
            output_type=doc.get("output_type"),
        )


@dataclass(frozen=True, slots=True)
class ObligationExample:
    """One ground fact: this argument yields this result."""

    argument: Any
    result: Any

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "argument", _json_value(self.argument, "obligation_example.argument")
        )
        object.__setattr__(
            self, "result", _json_value(self.result, "obligation_example.result")
        )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ObligationExample":
        doc = _strict_fields(
            data, label="ObligationExample", required={"argument", "result"}
        )
        return cls(argument=doc["argument"], result=doc["result"])


MAX_SAMPLE_SIZE = 4096
# Which operand slots each relation may carry, and which it must.
OBLIGATION_SLOTS: dict[ObligationRelation, frozenset[str]] = {
    ObligationRelation.EXAMPLE: frozenset({"example"}),
    ObligationRelation.TOTAL: frozenset({"guard_operation_id"}),
    ObligationRelation.ROUND_TRIP: frozenset({"witness_operation_id"}),
    ObligationRelation.IDEMPOTENT: frozenset(),
    ObligationRelation.PRESERVES: frozenset({"predicate_operation_id"}),
    ObligationRelation.ESTABLISHES: frozenset({"predicate_operation_id"}),
}
_OBLIGATION_REQUIRED_SLOTS: dict[ObligationRelation, frozenset[str]] = {
    ObligationRelation.EXAMPLE: frozenset({"example"}),
    # TOTAL's guard is optional: an operation total on its whole declared input
    # type needs no domain restriction.
    ObligationRelation.TOTAL: frozenset(),
    ObligationRelation.ROUND_TRIP: frozenset({"witness_operation_id"}),
    ObligationRelation.IDEMPOTENT: frozenset(),
    ObligationRelation.PRESERVES: frozenset({"predicate_operation_id"}),
    ObligationRelation.ESTABLISHES: frozenset({"predicate_operation_id"}),
}


@dataclass(frozen=True, slots=True)
class ProofObligation:
    """One machine-checkable claim about an operation.

    Obligations hang off the contract rather than off an operation because
    ``ROUND_TRIP`` names two operations and could not honestly belong to
    either, and because ``EXAMPLE`` obligations are numerous and mechanical --
    forcing each to carry an ``Invariant``'s required prose statement would
    make the anti-vacuity rule expensive enough to skip.
    """

    id: str
    relation: ObligationRelation
    subject_operation_id: str
    requirement_ids: tuple[str, ...]
    tier: ObligationTier = ObligationTier.SAMPLE
    witness_operation_id: str | None = None
    predicate_operation_id: str | None = None
    guard_operation_id: str | None = None
    example: ObligationExample | None = None
    sample_size: int | None = None
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "obligation.id"))
        relation = _enum(self.relation, ObligationRelation, "obligation.relation")
        object.__setattr__(self, "relation", relation)
        tier = _enum(self.tier, ObligationTier, "obligation.tier")
        object.__setattr__(self, "tier", tier)
        object.__setattr__(
            self,
            "subject_operation_id",
            _stable_id(self.subject_operation_id, "obligation.subject_operation_id"),
        )
        object.__setattr__(
            self,
            "requirement_ids",
            _strings(
                self.requirement_ids,
                "obligation.requirement_ids",
                allow_empty=False,
                stable_ids=True,
            ),
        )
        object.__setattr__(
            self,
            "description",
            _text(self.description, "obligation.description", allow_empty=True),
        )
        for slot in ("witness_operation_id", "predicate_operation_id", "guard_operation_id"):
            value = getattr(self, slot)
            if value is not None:
                object.__setattr__(self, slot, _stable_id(value, f"obligation.{slot}"))
        example = self.example
        if isinstance(example, Mapping):
            example = ObligationExample.from_dict(example)
            object.__setattr__(self, "example", example)
        if example is not None and not isinstance(example, ObligationExample):
            raise ModelValidationError(
                "obligation.example must be an ObligationExample"
            )

        allowed = OBLIGATION_SLOTS[relation]
        for slot in (
            "witness_operation_id",
            "predicate_operation_id",
            "guard_operation_id",
            "example",
        ):
            if slot not in allowed and getattr(self, slot) is not None:
                raise ModelValidationError(
                    f"obligation relation {relation.value!r} cannot carry {slot!r}"
                )
        for slot in _OBLIGATION_REQUIRED_SLOTS[relation]:
            if getattr(self, slot) is None:
                raise ModelValidationError(
                    f"obligation relation {relation.value!r} requires {slot!r}"
                )

        if relation is ObligationRelation.EXAMPLE:
            # An example is exactly one sample, so it can never be a proof for
            # all inputs and needs no sample count.
            if tier is not ObligationTier.SAMPLE:
                raise ModelValidationError(
                    "example obligations are a single sample and cannot claim a "
                    "proof tier"
                )
            if self.sample_size is not None:
                raise ModelValidationError(
                    "example obligations cannot carry a sample size"
                )
        elif tier is ObligationTier.SAMPLE:
            if self.sample_size is None:
                raise ModelValidationError(
                    "sample-tier obligations require a sample size"
                )
            size = _non_negative_int(self.sample_size, "obligation.sample_size")
            if not 1 <= size <= MAX_SAMPLE_SIZE:
                raise ModelValidationError(
                    f"obligation.sample_size must be between 1 and {MAX_SAMPLE_SIZE}"
                )
            object.__setattr__(self, "sample_size", size)
        elif self.sample_size is not None:
            raise ModelValidationError(
                "proof-tier obligations cover the whole domain and cannot carry a "
                "sample size"
            )

    @property
    def operand_operation_ids(self) -> tuple[str, ...]:
        """Every operation id this obligation names, subject first."""

        return tuple(
            operation_id
            for operation_id in (
                self.subject_operation_id,
                self.witness_operation_id,
                self.predicate_operation_id,
                self.guard_operation_id,
            )
            if operation_id is not None
        )

    def _serialized_field_names(self) -> list[str]:
        # Most relations leave most operand slots empty; omitting them keeps a
        # contract readable and keeps it inside a bounded prompt. Every omitted
        # name is optional in from_dict with exactly the default dropped here.
        defaults: dict[str, Any] = {
            "tier": ObligationTier.SAMPLE,
            "witness_operation_id": None,
            "predicate_operation_id": None,
            "guard_operation_id": None,
            "example": None,
            "sample_size": None,
            "description": "",
        }
        return [
            item.name
            for item in fields(self)
            if item.name not in defaults
            or getattr(self, item.name) != defaults[item.name]
        ]

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProofObligation":
        doc = _strict_fields(
            data,
            label="ProofObligation",
            required={
                "id",
                "relation",
                "subject_operation_id",
                "requirement_ids",
            },
            optional={
                "tier",
                "witness_operation_id",
                "predicate_operation_id",
                "guard_operation_id",
                "example",
                "sample_size",
                "description",
            },
        )
        return cls(
            id=doc["id"],
            relation=doc["relation"],
            subject_operation_id=doc["subject_operation_id"],
            requirement_ids=doc["requirement_ids"],
            tier=doc.get("tier", ObligationTier.SAMPLE),
            witness_operation_id=doc.get("witness_operation_id"),
            predicate_operation_id=doc.get("predicate_operation_id"),
            guard_operation_id=doc.get("guard_operation_id"),
            example=doc.get("example"),
            sample_size=doc.get("sample_size"),
            description=doc.get("description", ""),
        )


@dataclass(frozen=True, slots=True)
class Invariant:
    """A prose statement about the system, optionally cited by formal obligations.

    The prose is kept verbatim and is never derived from the obligations, nor
    they from it.  A formalisation that *claims* to be the prose is a lie; one
    that *cites* it is a traceability edge.
    """

    id: str
    statement: str
    requirement_ids: tuple[str, ...]
    obligation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "invariant.id"))
        object.__setattr__(
            self, "statement", _text(self.statement, "invariant.statement")
        )
        object.__setattr__(
            self,
            "requirement_ids",
            _strings(
                self.requirement_ids,
                "invariant.requirement_ids",
                allow_empty=False,
                stable_ids=True,
            ),
        )
        object.__setattr__(
            self,
            "obligation_ids",
            _strings(
                self.obligation_ids,
                "invariant.obligation_ids",
                stable_ids=True,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Invariant":
        doc = _strict_fields(
            data,
            label="Invariant",
            required={"id", "statement", "requirement_ids"},
            optional={"obligation_ids"},
        )
        return cls(
            id=doc["id"],
            statement=doc["statement"],
            requirement_ids=doc["requirement_ids"],
            obligation_ids=doc.get("obligation_ids", ()),
        )


@dataclass(frozen=True, slots=True)
class Contract:
    id: str
    node_id: str
    operations: tuple[OperationContract, ...]
    invariants: tuple[Invariant, ...] = ()
    obligations: tuple[ProofObligation, ...] = ()
    revision: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "contract.id"))
        object.__setattr__(self, "node_id", _stable_id(self.node_id, "contract.node_id"))
        object.__setattr__(
            self,
            "operations",
            _models(self.operations, OperationContract, "contract.operations"),
        )
        object.__setattr__(
            self,
            "invariants",
            _models(self.invariants, Invariant, "contract.invariants"),
        )
        object.__setattr__(
            self,
            "obligations",
            _models(self.obligations, ProofObligation, "contract.obligations"),
        )
        object.__setattr__(
            self, "revision", _positive_revision(self.revision, "contract.revision")
        )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "contract.metadata")
        )
        self.validate()

    @property
    def operation_index(self) -> dict[str, OperationContract]:
        return {operation.id: operation for operation in self.operations}

    @property
    def obligation_index(self) -> dict[str, ProofObligation]:
        return {obligation.id: obligation for obligation in self.obligations}

    @property
    def traced_requirement_ids(self) -> frozenset[str]:
        requirement_ids: set[str] = set()
        for behavior in (*self.operations, *self.invariants, *self.obligations):
            requirement_ids.update(behavior.requirement_ids)
        return frozenset(requirement_ids)

    def validate(self) -> None:
        operations = _unique_by_id(self.operations, "contract.operations")
        invariants = _unique_by_id(self.invariants, "contract.invariants")
        obligations = _unique_by_id(self.obligations, "contract.obligations")
        duplicate_behavior_ids = (
            (operations.keys() & invariants.keys())
            | (operations.keys() & obligations.keys())
            | (invariants.keys() & obligations.keys())
        )
        if duplicate_behavior_ids:
            raise ModelValidationError(
                "contract behavior ids must be globally unique within the contract: "
                f"{sorted(duplicate_behavior_ids)}"
            )
        if not operations and not invariants:
            raise ModelValidationError(
                "contract must define at least one operation or invariant"
            )
        self._validate_obligations(operations)
        for invariant in self.invariants:
            unknown = set(invariant.obligation_ids) - obligations.keys()
            if unknown:
                raise ModelValidationError(
                    f"invariant {invariant.id!r} cites unknown obligations: "
                    f"{sorted(unknown)}"
                )

    def _validate_obligations(
        self, operations: dict[str, OperationContract]
    ) -> None:
        anchored: set[str] = set()
        constrained: set[str] = set()
        for obligation in self.obligations:
            operands = {}
            for slot in (
                "subject_operation_id",
                "witness_operation_id",
                "predicate_operation_id",
                "guard_operation_id",
            ):
                operation_id = getattr(obligation, slot)
                if operation_id is None:
                    operands[slot] = None
                    continue
                operation = operations.get(operation_id)
                if operation is None:
                    raise ModelValidationError(
                        f"obligation {obligation.id!r} references unknown operation "
                        f"{operation_id!r}"
                    )
                operands[slot] = operation
            subject = operands["subject_operation_id"]
            assert subject is not None
            stray = set(obligation.requirement_ids) - set(subject.requirement_ids)
            if stray:
                # An obligation cannot claim to serve a requirement the
                # operation it constrains does not itself serve.
                raise ModelValidationError(
                    f"obligation {obligation.id!r} traces requirements its subject "
                    f"does not: {sorted(stray)}"
                )
            self._validate_obligation_shape(obligation, subject, operands)
            if obligation.relation is ObligationRelation.EXAMPLE:
                anchored.add(subject.id)
            else:
                # Every operation the obligation names, not just its subject.
                # A predicate that always returns true makes PRESERVES and
                # ESTABLISHES trivially true no matter what the subject does,
                # and a guard that always returns false makes TOTAL trivially
                # true by excluding every case. Anchoring only subjects would
                # leave both holes open.
                constrained.update(obligation.operand_operation_ids)
        unanchored = constrained - anchored
        if unanchored:
            # Identity satisfies IDEMPOTENT, PRESERVES and ROUND_TRIP, and a
            # function that never fails satisfies TOTAL.  Without at least one
            # ground example pinning what each named operation actually
            # computes, a passing property gate -- or a machine-checked proof --
            # says nothing at all.
            raise ModelValidationError(
                "every operation a non-example obligation names needs at least "
                "one example obligation to rule out a trivial implementation; "
                f"unanchored operations: {sorted(unanchored)}"
            )

    @staticmethod
    def _validate_obligation_shape(
        obligation: "ProofObligation",
        subject: OperationContract,
        operands: dict[str, OperationContract | None],
    ) -> None:
        relation = obligation.relation
        label = f"obligation {obligation.id!r}"

        def require_same(
            left: ValueType | None, right: ValueType | None, message: str
        ) -> None:
            # Types are optional, so a contract that declares none is checked
            # structurally only.  Two declared types that disagree are a bug.
            if left is not None and right is not None and left != right:
                raise ModelValidationError(f"{label} {message}")

        def require_predicate(
            operation: OperationContract, domain: ValueType | None
        ) -> None:
            if (
                operation.output_type is not None
                and operation.output_type.kind is not ValueTypeKind.BOOLEAN
            ):
                raise ModelValidationError(
                    f"{label} requires predicate {operation.id!r} to return a boolean"
                )
            require_same(
                operation.input_type,
                domain,
                f"requires predicate {operation.id!r} to accept the same type it judges",
            )

        if relation is ObligationRelation.EXAMPLE:
            example = obligation.example
            assert example is not None
            for declared, value, side in (
                (subject.input_type, example.argument, "argument"),
                (subject.output_type, example.result, "result"),
            ):
                if declared is None:
                    continue
                # Say why, not just that. This message is read by a person
                # correcting a contract and by a model retrying against it,
                # and neither can act on "does not inhabit".
                reason = declared.explain(value, side)
                if reason is not None:
                    raise ModelValidationError(
                        f"{label} example {side} does not inhabit the subject "
                        f"{'input' if side == 'argument' else 'output'} type: "
                        f"{reason}"
                    )
            return

        if relation is ObligationRelation.TOTAL:
            if not subject.errors:
                # Totality is the claim that a declared failure never happens.
                # An operation that declares none makes it trivially true.
                raise ModelValidationError(
                    f"{label} asserts totality of an operation that declares no "
                    "errors, which is vacuously true"
                )
            guard = operands["guard_operation_id"]
            if guard is not None:
                require_predicate(guard, subject.input_type)
        elif relation is ObligationRelation.ROUND_TRIP:
            witness = operands["witness_operation_id"]
            assert witness is not None
            require_same(
                witness.input_type,
                subject.output_type,
                f"requires witness {witness.id!r} to accept the subject's output",
            )
            require_same(
                witness.output_type,
                subject.input_type,
                f"requires witness {witness.id!r} to return the subject's input",
            )
        elif relation is ObligationRelation.IDEMPOTENT:
            require_same(
                subject.output_type,
                subject.input_type,
                "requires an endomorphism: the subject's output type must equal "
                "its input type",
            )
        elif relation is ObligationRelation.PRESERVES:
            predicate = operands["predicate_operation_id"]
            assert predicate is not None
            require_same(
                subject.output_type,
                subject.input_type,
                "requires an endomorphism: the subject's output type must equal "
                "its input type",
            )
            require_predicate(predicate, subject.input_type)
        else:
            predicate = operands["predicate_operation_id"]
            assert predicate is not None
            require_predicate(predicate, subject.output_type)

        if obligation.tier is ObligationTier.SAMPLE:
            domain = subject.input_type
            if domain is None or not domain.is_finitely_sampleable:
                raise ModelValidationError(
                    f"{label} claims a sample tier over a domain no generator can "
                    "draw from; declare a bounded input type or claim a proof tier"
                )

    def validate_requirement_ids(self, known_requirement_ids: Iterable[str]) -> None:
        known = set(known_requirement_ids)
        unknown = self.traced_requirement_ids - known
        if unknown:
            raise ModelValidationError(
                f"contract {self.id!r} traces unknown requirements: {sorted(unknown)}"
            )

    def projection(self, requirement_ids: Iterable[str]) -> dict[str, Any]:
        """Serialize only the behaviors that serve these requirements.

        A worker allocated part of a contract should see that part, not the
        whole thing -- both because a bounded prompt cannot hold the whole
        thing once operations carry types and obligations, and because the
        scope it is shown is the scope it will try to implement.

        Operations named as an obligation's operand are kept even when they
        serve other requirements, so a retained obligation never dangles.  The
        result is a view, not a document: a slice can hold an operation with no
        example of its own and so need not satisfy ``validate``.  It is
        deliberately metadata-free, since metadata is planner-defined and a
        consumer that wants part of it must project it itself.
        """

        wanted = set(requirement_ids)
        obligations = [
            obligation
            for obligation in self.obligations
            if wanted & set(obligation.requirement_ids)
        ]
        operation_ids = {
            operation.id
            for operation in self.operations
            if wanted & set(operation.requirement_ids)
        }
        for obligation in obligations:
            operation_ids.update(obligation.operand_operation_ids)
        retained = {obligation.id for obligation in obligations}
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "node_id": self.node_id,
            "revision": self.revision,
            "operations": [
                _without_derivable_schemas(operation)
                for operation in self.operations
                if operation.id in operation_ids
            ],
            "invariants": [
                {
                    **invariant.to_dict(),
                    "obligation_ids": [
                        obligation_id
                        for obligation_id in invariant.obligation_ids
                        if obligation_id in retained
                    ],
                }
                for invariant in self.invariants
                if wanted & set(invariant.requirement_ids)
            ],
            "obligations": [obligation.to_dict() for obligation in obligations],
        }

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Contract":
        doc = _strict_fields(
            data,
            label="Contract",
            required={"schema_version", "id", "node_id", "operations"},
            optional={"invariants", "obligations", "revision", "metadata"},
        )
        _check_schema_version(doc, "Contract")
        return cls(
            id=doc["id"],
            node_id=doc["node_id"],
            operations=doc["operations"],
            invariants=doc.get("invariants", ()),
            obligations=doc.get("obligations", ()),
            revision=doc.get("revision", 1),
            metadata=doc.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class PortSpec:
    id: str
    name: str
    direction: PortDirection
    schema: dict[str, Any]
    operation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "port.id"))
        object.__setattr__(self, "name", _text(self.name, "port.name"))
        object.__setattr__(
            self, "direction", _enum(self.direction, PortDirection, "port.direction")
        )
        object.__setattr__(self, "schema", _json_mapping(self.schema, "port.schema"))
        if self.operation_id is not None:
            object.__setattr__(
                self,
                "operation_id",
                _stable_id(self.operation_id, "port.operation_id"),
            )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PortSpec":
        doc = _strict_fields(
            data,
            label="PortSpec",
            required={"id", "name", "direction", "schema"},
            optional={"operation_id"},
        )
        return cls(
            id=doc["id"],
            name=doc["name"],
            direction=doc["direction"],
            schema=doc["schema"],
            operation_id=doc.get("operation_id"),
        )


@dataclass(frozen=True, slots=True)
class ArchitectureNode:
    id: str
    name: str
    kind: NodeKind
    contract_id: str | None
    ports: tuple[PortSpec, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    owned_paths: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "node.id"))
        object.__setattr__(self, "name", _text(self.name, "node.name"))
        object.__setattr__(self, "kind", _enum(self.kind, NodeKind, "node.kind"))
        if self.contract_id is not None:
            object.__setattr__(
                self,
                "contract_id",
                _stable_id(self.contract_id, "node.contract_id"),
            )
        object.__setattr__(
            self, "ports", _models(self.ports, PortSpec, "node.ports")
        )
        _unique_by_id(self.ports, "node.ports")
        object.__setattr__(
            self,
            "requirement_ids",
            _strings(
                self.requirement_ids,
                "node.requirement_ids",
                stable_ids=True,
            ),
        )
        if isinstance(self.owned_paths, (str, bytes)) or not isinstance(
            self.owned_paths, Iterable
        ):
            raise ModelValidationError("node.owned_paths must be a sequence")
        owned_paths = tuple(
            _relative_owned_path(path, f"node.owned_paths[{index}]")
            for index, path in enumerate(self.owned_paths)
        )
        if len(set(owned_paths)) != len(owned_paths):
            raise ModelValidationError("node.owned_paths cannot contain duplicates")
        object.__setattr__(self, "owned_paths", owned_paths)
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "node.metadata")
        )

    @property
    def port_index(self) -> dict[str, PortSpec]:
        return {port.id: port for port in self.ports}

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArchitectureNode":
        doc = _strict_fields(
            data,
            label="ArchitectureNode",
            required={"id", "name", "kind", "contract_id"},
            optional={"ports", "requirement_ids", "owned_paths", "metadata"},
        )
        return cls(
            id=doc["id"],
            name=doc["name"],
            kind=doc["kind"],
            contract_id=doc["contract_id"],
            ports=doc.get("ports", ()),
            requirement_ids=doc.get("requirement_ids", ()),
            owned_paths=doc.get("owned_paths", ()),
            metadata=doc.get("metadata", {}),
        )


_PORT_EDGE_KINDS = frozenset(
    {
        EdgeKind.CALL,
        EdgeKind.DATA,
        EdgeKind.CAPABILITY,
        EdgeKind.EVENT,
        EdgeKind.SCHEMA,
    }
)


@dataclass(frozen=True, slots=True)
class ArchitectureEdge:
    id: str
    kind: EdgeKind
    source_node_id: str
    target_node_id: str
    source_port_id: str | None = None
    target_port_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "edge.id"))
        object.__setattr__(self, "kind", _enum(self.kind, EdgeKind, "edge.kind"))
        object.__setattr__(
            self,
            "source_node_id",
            _stable_id(self.source_node_id, "edge.source_node_id"),
        )
        object.__setattr__(
            self,
            "target_node_id",
            _stable_id(self.target_node_id, "edge.target_node_id"),
        )
        if self.source_node_id == self.target_node_id:
            raise ModelValidationError(f"edge {self.id!r} cannot target its source")
        if self.source_port_id is not None:
            object.__setattr__(
                self,
                "source_port_id",
                _stable_id(self.source_port_id, "edge.source_port_id"),
            )
        if self.target_port_id is not None:
            object.__setattr__(
                self,
                "target_port_id",
                _stable_id(self.target_port_id, "edge.target_port_id"),
            )
        if self.kind in _PORT_EDGE_KINDS and (
            self.source_port_id is None or self.target_port_id is None
        ):
            raise ModelValidationError(
                f"{self.kind.value} edge {self.id!r} requires source and target ports"
            )
        if self.kind is EdgeKind.CONTAINS and (
            self.source_port_id is not None or self.target_port_id is not None
        ):
            raise ModelValidationError(
                f"contains edge {self.id!r} cannot carry ports"
            )
        if (self.source_port_id is None) != (self.target_port_id is None):
            raise ModelValidationError(
                f"edge {self.id!r} must define both ports or neither"
            )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "edge.metadata")
        )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArchitectureEdge":
        doc = _strict_fields(
            data,
            label="ArchitectureEdge",
            required={"id", "kind", "source_node_id", "target_node_id"},
            optional={"source_port_id", "target_port_id", "metadata"},
        )
        return cls(
            id=doc["id"],
            kind=doc["kind"],
            source_node_id=doc["source_node_id"],
            target_node_id=doc["target_node_id"],
            source_port_id=doc.get("source_port_id"),
            target_port_id=doc.get("target_port_id"),
            metadata=doc.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class ArchitectureSpec:
    id: str
    project_id: str
    root_node_id: str
    target_pack: str
    nodes: tuple[ArchitectureNode, ...]
    edges: tuple[ArchitectureEdge, ...]
    contracts: tuple[Contract, ...]
    project_spec_revision: int = 1
    revision: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "architecture.id"))
        object.__setattr__(
            self, "project_id", _stable_id(self.project_id, "architecture.project_id")
        )
        object.__setattr__(
            self,
            "root_node_id",
            _stable_id(self.root_node_id, "architecture.root_node_id"),
        )
        object.__setattr__(
            self, "target_pack", _stable_id(self.target_pack, "architecture.target_pack")
        )
        object.__setattr__(
            self,
            "nodes",
            _models(self.nodes, ArchitectureNode, "architecture.nodes"),
        )
        object.__setattr__(
            self,
            "edges",
            _models(self.edges, ArchitectureEdge, "architecture.edges"),
        )
        object.__setattr__(
            self,
            "contracts",
            _models(self.contracts, Contract, "architecture.contracts"),
        )
        object.__setattr__(
            self,
            "project_spec_revision",
            _positive_revision(
                self.project_spec_revision, "architecture.project_spec_revision"
            ),
        )
        object.__setattr__(
            self,
            "revision",
            _positive_revision(self.revision, "architecture.revision"),
        )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "architecture.metadata")
        )
        self.validate()

    @property
    def node_index(self) -> dict[str, ArchitectureNode]:
        return {node.id: node for node in self.nodes}

    @property
    def edge_index(self) -> dict[str, ArchitectureEdge]:
        return {edge.id: edge for edge in self.edges}

    @property
    def contract_index(self) -> dict[str, Contract]:
        return {contract.id: contract for contract in self.contracts}

    def validate(self) -> None:
        nodes = _unique_by_id(self.nodes, "architecture.nodes")
        edges = _unique_by_id(self.edges, "architecture.edges")
        contracts = _unique_by_id(self.contracts, "architecture.contracts")
        if not nodes:
            raise ModelValidationError("architecture.nodes cannot be empty")
        if self.root_node_id not in nodes:
            raise ModelValidationError(
                f"architecture root node {self.root_node_id!r} does not exist"
            )

        contract_owners: dict[str, str] = {}
        owned_paths: dict[str, str] = {}
        for node in nodes.values():
            for path in node.owned_paths:
                previous_owner = owned_paths.get(path)
                if previous_owner is not None:
                    raise ModelValidationError(
                        f"owned path {path!r} is assigned to both "
                        f"{previous_owner!r} and {node.id!r}"
                    )
                owned_paths[path] = node.id

            if node.contract_id is None:
                if node.kind is not NodeKind.RESOURCE:
                    raise ModelValidationError(
                        f"non-resource node {node.id!r} requires a contract"
                    )
                if node.requirement_ids:
                    raise ModelValidationError(
                        f"resource node {node.id!r} cannot own requirements "
                        "without a contract"
                    )
                continue

            contract = contracts.get(node.contract_id)
            if contract is None:
                raise ModelValidationError(
                    f"node {node.id!r} references unknown contract "
                    f"{node.contract_id!r}"
                )
            if contract.node_id != node.id:
                raise ModelValidationError(
                    f"contract {contract.id!r} belongs to {contract.node_id!r}, "
                    f"not node {node.id!r}"
                )
            if node.contract_id in contract_owners:
                raise ModelValidationError(
                    f"contract {node.contract_id!r} is assigned to multiple nodes"
                )
            contract_owners[node.contract_id] = node.id
            if set(node.requirement_ids) != set(contract.traced_requirement_ids):
                raise ModelValidationError(
                    f"node {node.id!r} requirement allocation must exactly match "
                    f"its contract traceability"
                )
            operation_ids = contract.operation_index.keys()
            for port in node.ports:
                if port.operation_id is not None and port.operation_id not in operation_ids:
                    raise ModelValidationError(
                        f"port {port.id!r} on node {node.id!r} references unknown "
                        f"operation {port.operation_id!r}"
                    )

        unassigned_contracts = contracts.keys() - contract_owners.keys()
        if unassigned_contracts:
            raise ModelValidationError(
                f"architecture has unassigned contracts: {sorted(unassigned_contracts)}"
            )

        incoming_contains: dict[str, int] = {node_id: 0 for node_id in nodes}
        containment_children: dict[str, list[str]] = {
            node_id: [] for node_id in nodes
        }
        for edge in edges.values():
            source = nodes.get(edge.source_node_id)
            target = nodes.get(edge.target_node_id)
            if source is None:
                raise ModelValidationError(
                    f"edge {edge.id!r} has unknown source {edge.source_node_id!r}"
                )
            if target is None:
                raise ModelValidationError(
                    f"edge {edge.id!r} has unknown target {edge.target_node_id!r}"
                )
            if edge.kind is EdgeKind.CONTAINS:
                incoming_contains[target.id] += 1
                containment_children[source.id].append(target.id)

            if edge.source_port_id is not None:
                source_port = source.port_index.get(edge.source_port_id)
                target_port = target.port_index.get(edge.target_port_id or "")
                if source_port is None:
                    raise ModelValidationError(
                        f"edge {edge.id!r} references unknown source port "
                        f"{edge.source_port_id!r}"
                    )
                if target_port is None:
                    raise ModelValidationError(
                        f"edge {edge.id!r} references unknown target port "
                        f"{edge.target_port_id!r}"
                    )
                if source_port.direction is not PortDirection.OUTPUT:
                    raise ModelValidationError(
                        f"edge {edge.id!r} source port must be an output"
                    )
                if target_port.direction is not PortDirection.INPUT:
                    raise ModelValidationError(
                        f"edge {edge.id!r} target port must be an input"
                    )

        if incoming_contains[self.root_node_id] != 0:
            raise ModelValidationError("architecture root cannot have a contains parent")
        invalid_parent_counts = {
            node_id: count
            for node_id, count in incoming_contains.items()
            if node_id != self.root_node_id and count != 1
        }
        if invalid_parent_counts:
            raise ModelValidationError(
                "every non-root node needs exactly one contains parent; "
                f"invalid counts: {invalid_parent_counts}"
            )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ModelValidationError(
                    f"contains edges form a cycle through node {node_id!r}"
                )
            if node_id in visited:
                return
            visiting.add(node_id)
            for child_id in containment_children[node_id]:
                visit(child_id)
            visiting.remove(node_id)
            visited.add(node_id)

        visit(self.root_node_id)
        unreachable = nodes.keys() - visited
        if unreachable:
            raise ModelValidationError(
                f"nodes are unreachable from architecture root: {sorted(unreachable)}"
            )

    def validate_against_project(self, project: ProjectSpec) -> None:
        if self.project_id != project.id:
            raise ModelValidationError(
                f"architecture project {self.project_id!r} does not match "
                f"project spec {project.id!r}"
            )
        if self.project_spec_revision != project.revision:
            raise ModelValidationError(
                "architecture was compiled for project revision "
                f"{self.project_spec_revision}, not {project.revision}"
            )
        known = set(project.requirement_index)
        allocated: set[str] = set()
        for node in self.nodes:
            allocated.update(node.requirement_ids)
        unknown = allocated - known
        if unknown:
            raise ModelValidationError(
                f"architecture allocates unknown requirements: {sorted(unknown)}"
            )
        missing = known - allocated
        if missing:
            raise ModelValidationError(
                f"architecture leaves requirements unallocated: {sorted(missing)}"
            )
        for contract in self.contracts:
            contract.validate_requirement_ids(known)

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArchitectureSpec":
        doc = _strict_fields(
            data,
            label="ArchitectureSpec",
            required={
                "schema_version",
                "id",
                "project_id",
                "root_node_id",
                "target_pack",
                "nodes",
                "edges",
                "contracts",
            },
            optional={"project_spec_revision", "revision", "metadata"},
        )
        _check_schema_version(doc, "ArchitectureSpec")
        return cls(
            id=doc["id"],
            project_id=doc["project_id"],
            root_node_id=doc["root_node_id"],
            target_pack=doc["target_pack"],
            nodes=doc["nodes"],
            edges=doc["edges"],
            contracts=doc["contracts"],
            project_spec_revision=doc.get("project_spec_revision", 1),
            revision=doc.get("revision", 1),
            metadata=doc.get("metadata", {}),
        )


_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.DRAFT: frozenset(
        {RunStatus.AWAITING_APPROVAL, RunStatus.CANCELED}
    ),
    RunStatus.AWAITING_APPROVAL: frozenset(
        {RunStatus.READY, RunStatus.BLOCKED, RunStatus.CANCELED}
    ),
    RunStatus.READY: frozenset({RunStatus.RUNNING, RunStatus.CANCELED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.VERIFYING,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
            RunStatus.CANCELED,
        }
    ),
    RunStatus.VERIFYING: frozenset(
        {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.RUNNING,
            RunStatus.BLOCKED,
            RunStatus.CANCELED,
        }
    ),
    RunStatus.BLOCKED: frozenset(
        {
            RunStatus.AWAITING_APPROVAL,
            RunStatus.READY,
            RunStatus.RUNNING,
            RunStatus.FAILED,
            RunStatus.CANCELED,
        }
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELED: frozenset(),
}

_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELED}
    ),
    TaskStatus.READY: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.CACHED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.VERIFYING,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELED,
        }
    ),
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.RUNNING,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELED,
        }
    ),
    TaskStatus.FAILED: frozenset({TaskStatus.READY, TaskStatus.CANCELED}),
    TaskStatus.BLOCKED: frozenset({TaskStatus.READY, TaskStatus.CANCELED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.CACHED: frozenset(),
    TaskStatus.CANCELED: frozenset(),
}

_EVIDENCE_TRANSITIONS: dict[EvidenceStatus, frozenset[EvidenceStatus]] = {
    EvidenceStatus.PENDING: frozenset(
        {EvidenceStatus.RUNNING, EvidenceStatus.SKIPPED, EvidenceStatus.ERROR}
    ),
    EvidenceStatus.RUNNING: frozenset(
        {
            EvidenceStatus.PASSED,
            EvidenceStatus.FAILED,
            EvidenceStatus.ERROR,
        }
    ),
    EvidenceStatus.PASSED: frozenset(),
    EvidenceStatus.FAILED: frozenset(),
    EvidenceStatus.SKIPPED: frozenset(),
    EvidenceStatus.ERROR: frozenset(),
}

_ARTIFACT_TRANSITIONS: dict[ArtifactStatus, frozenset[ArtifactStatus]] = {
    ArtifactStatus.PENDING: frozenset(
        {ArtifactStatus.AVAILABLE, ArtifactStatus.QUARANTINED, ArtifactStatus.DELETED}
    ),
    ArtifactStatus.AVAILABLE: frozenset(
        {
            ArtifactStatus.QUARANTINED,
            ArtifactStatus.EXPIRED,
            ArtifactStatus.DELETED,
        }
    ),
    ArtifactStatus.QUARANTINED: frozenset(
        {ArtifactStatus.AVAILABLE, ArtifactStatus.EXPIRED, ArtifactStatus.DELETED}
    ),
    ArtifactStatus.EXPIRED: frozenset({ArtifactStatus.DELETED}),
    ArtifactStatus.DELETED: frozenset(),
}


def _transition(
    current: EnumT,
    target: Any,
    enum_type: type[EnumT],
    transitions: Mapping[EnumT, frozenset[EnumT]],
    label: str,
) -> EnumT:
    target_status = _enum(target, enum_type, label)
    if target_status == current:
        return target_status
    if target_status not in transitions[current]:
        raise ModelValidationError(
            f"invalid {label} transition: {current.value} -> {target_status.value}"
        )
    return target_status


@dataclass(frozen=True, slots=True)
class BuildTask:
    id: str
    run_id: str
    node_id: str
    kind: TaskKind
    status: TaskStatus = TaskStatus.PENDING
    dependency_task_ids: tuple[str, ...] = ()
    attempt: int = 0
    cache_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "task.id"))
        object.__setattr__(self, "run_id", _stable_id(self.run_id, "task.run_id"))
        object.__setattr__(self, "node_id", _stable_id(self.node_id, "task.node_id"))
        object.__setattr__(self, "kind", _enum(self.kind, TaskKind, "task.kind"))
        object.__setattr__(
            self, "status", _enum(self.status, TaskStatus, "task.status")
        )
        object.__setattr__(
            self,
            "dependency_task_ids",
            _strings(
                self.dependency_task_ids,
                "task.dependency_task_ids",
                stable_ids=True,
            ),
        )
        if self.id in self.dependency_task_ids:
            raise ModelValidationError(f"task {self.id!r} cannot depend on itself")
        object.__setattr__(
            self, "attempt", _non_negative_int(self.attempt, "task.attempt")
        )
        if self.cache_key is not None:
            if not isinstance(self.cache_key, str) or not _SHA256_RE.fullmatch(
                self.cache_key
            ):
                raise ModelValidationError(
                    "task.cache_key must be a lowercase SHA-256 digest"
                )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "task.metadata")
        )

    def transitioned(self, status: TaskStatus | str) -> "BuildTask":
        target = _transition(
            self.status, status, TaskStatus, _TASK_TRANSITIONS, "task status"
        )
        attempt = self.attempt
        if target is TaskStatus.RUNNING and self.status is not TaskStatus.RUNNING:
            attempt += 1
        return replace(self, status=target, attempt=attempt)

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BuildTask":
        doc = _strict_fields(
            data,
            label="BuildTask",
            required={
                "schema_version",
                "id",
                "run_id",
                "node_id",
                "kind",
                "status",
            },
            optional={
                "dependency_task_ids",
                "attempt",
                "cache_key",
                "metadata",
            },
        )
        _check_schema_version(doc, "BuildTask")
        return cls(
            id=doc["id"],
            run_id=doc["run_id"],
            node_id=doc["node_id"],
            kind=doc["kind"],
            status=doc["status"],
            dependency_task_ids=doc.get("dependency_task_ids", ()),
            attempt=doc.get("attempt", 0),
            cache_key=doc.get("cache_key"),
            metadata=doc.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    run_id: str
    kind: EvidenceKind
    status: EvidenceStatus = EvidenceStatus.PENDING
    blocking: bool = True
    task_id: str | None = None
    node_id: str | None = None
    requirement_ids: tuple[str, ...] = ()
    acceptance_scenario_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "evidence.id"))
        object.__setattr__(
            self, "run_id", _stable_id(self.run_id, "evidence.run_id")
        )
        object.__setattr__(
            self, "kind", _enum(self.kind, EvidenceKind, "evidence.kind")
        )
        object.__setattr__(
            self,
            "status",
            _enum(self.status, EvidenceStatus, "evidence.status"),
        )
        if not isinstance(self.blocking, bool):
            raise ModelValidationError("evidence.blocking must be a boolean")
        for field_name in ("task_id", "node_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _stable_id(value, f"evidence.{field_name}"),
                )
        object.__setattr__(
            self,
            "requirement_ids",
            _strings(
                self.requirement_ids,
                "evidence.requirement_ids",
                stable_ids=True,
            ),
        )
        object.__setattr__(
            self,
            "acceptance_scenario_ids",
            _strings(
                self.acceptance_scenario_ids,
                "evidence.acceptance_scenario_ids",
                stable_ids=True,
            ),
        )
        object.__setattr__(
            self,
            "artifact_ids",
            _strings(
                self.artifact_ids, "evidence.artifact_ids", stable_ids=True
            ),
        )
        if (
            self.kind is EvidenceKind.ACCEPTANCE
            and not self.requirement_ids
        ):
            raise ModelValidationError(
                "acceptance evidence must trace requirements"
            )
        if (
            self.kind is EvidenceKind.ACCEPTANCE
            and self.status is EvidenceStatus.PASSED
            and not self.acceptance_scenario_ids
        ):
            raise ModelValidationError(
                "passed acceptance evidence must trace acceptance scenarios"
            )
        if self.status is EvidenceStatus.PASSED and not self.artifact_ids:
            raise ModelValidationError(
                "passed evidence requires at least one immutable result artifact"
            )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "evidence.metadata")
        )

    @property
    def satisfies_gate(self) -> bool:
        return self.status is EvidenceStatus.PASSED

    def transitioned(self, status: EvidenceStatus | str) -> "Evidence":
        target = _transition(
            self.status,
            status,
            EvidenceStatus,
            _EVIDENCE_TRANSITIONS,
            "evidence status",
        )
        return replace(self, status=target)

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Evidence":
        doc = _strict_fields(
            data,
            label="Evidence",
            required={
                "schema_version",
                "id",
                "run_id",
                "kind",
                "status",
            },
            optional={
                "blocking",
                "task_id",
                "node_id",
                "requirement_ids",
                "acceptance_scenario_ids",
                "artifact_ids",
                "metadata",
            },
        )
        _check_schema_version(doc, "Evidence")
        return cls(
            id=doc["id"],
            run_id=doc["run_id"],
            kind=doc["kind"],
            status=doc["status"],
            blocking=doc.get("blocking", True),
            task_id=doc.get("task_id"),
            node_id=doc.get("node_id"),
            requirement_ids=doc.get("requirement_ids", ()),
            acceptance_scenario_ids=doc.get("acceptance_scenario_ids", ()),
            artifact_ids=doc.get("artifact_ids", ()),
            metadata=doc.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class Approval:
    id: str
    project_id: str
    gate: ApprovalGate
    revision_id: str
    status: ApprovalStatus = ApprovalStatus.REQUESTED
    run_id: str | None = None
    requested_capabilities: tuple[str, ...] = ()
    decided_by: str | None = None
    decision_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "approval.id"))
        object.__setattr__(
            self,
            "project_id",
            _stable_id(self.project_id, "approval.project_id"),
        )
        object.__setattr__(
            self, "gate", _enum(self.gate, ApprovalGate, "approval.gate")
        )
        object.__setattr__(
            self,
            "revision_id",
            _stable_id(self.revision_id, "approval.revision_id"),
        )
        object.__setattr__(
            self,
            "status",
            _enum(self.status, ApprovalStatus, "approval.status"),
        )
        if self.run_id is not None:
            object.__setattr__(
                self, "run_id", _stable_id(self.run_id, "approval.run_id")
            )
        object.__setattr__(
            self,
            "requested_capabilities",
            _strings(
                self.requested_capabilities,
                "approval.requested_capabilities",
                stable_ids=True,
            ),
        )
        if self.decided_by is not None:
            object.__setattr__(
                self,
                "decided_by",
                _text(self.decided_by, "approval.decided_by"),
            )
        if self.decision_reason is not None:
            object.__setattr__(
                self,
                "decision_reason",
                _text(self.decision_reason, "approval.decision_reason"),
            )
        if self.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            if self.decided_by is None:
                raise ModelValidationError(
                    "approved or rejected approvals require decided_by"
                )
        elif self.status is ApprovalStatus.REQUESTED and (
            self.decided_by is not None or self.decision_reason is not None
        ):
            raise ModelValidationError(
                "a requested approval cannot contain a decision"
            )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "approval.metadata")
        )

    def decided(
        self,
        status: ApprovalStatus | str,
        *,
        decided_by: str,
        reason: str | None = None,
    ) -> "Approval":
        target = _enum(status, ApprovalStatus, "approval status")
        if self.status is not ApprovalStatus.REQUESTED:
            raise ModelValidationError(
                f"approval {self.id!r} has already been decided"
            )
        if target not in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.CANCELED,
        }:
            raise ModelValidationError(
                "approval decision must be approved, rejected, or canceled"
            )
        return replace(
            self,
            status=target,
            decided_by=decided_by,
            decision_reason=reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Approval":
        doc = _strict_fields(
            data,
            label="Approval",
            required={
                "schema_version",
                "id",
                "project_id",
                "gate",
                "revision_id",
                "status",
            },
            optional={
                "run_id",
                "requested_capabilities",
                "decided_by",
                "decision_reason",
                "metadata",
            },
        )
        _check_schema_version(doc, "Approval")
        return cls(
            id=doc["id"],
            project_id=doc["project_id"],
            gate=doc["gate"],
            revision_id=doc["revision_id"],
            status=doc["status"],
            run_id=doc.get("run_id"),
            requested_capabilities=doc.get("requested_capabilities", ()),
            decided_by=doc.get("decided_by"),
            decision_reason=doc.get("decision_reason"),
            metadata=doc.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    run_id: str
    kind: ArtifactKind
    status: ArtifactStatus = ArtifactStatus.PENDING
    digest: str | None = None
    uri: str | None = None
    media_type: str = "application/octet-stream"
    produced_by_task_id: str | None = None
    requirement_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "artifact.id"))
        object.__setattr__(
            self, "run_id", _stable_id(self.run_id, "artifact.run_id")
        )
        object.__setattr__(
            self, "kind", _enum(self.kind, ArtifactKind, "artifact.kind")
        )
        object.__setattr__(
            self,
            "status",
            _enum(self.status, ArtifactStatus, "artifact.status"),
        )
        if self.digest is not None:
            if not isinstance(self.digest, str) or not _SHA256_RE.fullmatch(
                self.digest
            ):
                raise ModelValidationError(
                    "artifact.digest must be a lowercase SHA-256 digest"
                )
        if self.uri is not None:
            object.__setattr__(self, "uri", _text(self.uri, "artifact.uri"))
        object.__setattr__(
            self, "media_type", _text(self.media_type, "artifact.media_type")
        )
        if self.produced_by_task_id is not None:
            object.__setattr__(
                self,
                "produced_by_task_id",
                _stable_id(
                    self.produced_by_task_id, "artifact.produced_by_task_id"
                ),
            )
        object.__setattr__(
            self,
            "requirement_ids",
            _strings(
                self.requirement_ids,
                "artifact.requirement_ids",
                stable_ids=True,
            ),
        )
        if self.status in {
            ArtifactStatus.AVAILABLE,
            ArtifactStatus.QUARANTINED,
        } and (self.digest is None or self.uri is None):
            raise ModelValidationError(
                f"{self.status.value} artifacts require digest and uri"
            )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "artifact.metadata")
        )

    def transitioned(self, status: ArtifactStatus | str) -> "Artifact":
        target = _transition(
            self.status,
            status,
            ArtifactStatus,
            _ARTIFACT_TRANSITIONS,
            "artifact status",
        )
        return replace(self, status=target)

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Artifact":
        doc = _strict_fields(
            data,
            label="Artifact",
            required={
                "schema_version",
                "id",
                "run_id",
                "kind",
                "status",
            },
            optional={
                "digest",
                "uri",
                "media_type",
                "produced_by_task_id",
                "requirement_ids",
                "metadata",
            },
        )
        _check_schema_version(doc, "Artifact")
        return cls(
            id=doc["id"],
            run_id=doc["run_id"],
            kind=doc["kind"],
            status=doc["status"],
            digest=doc.get("digest"),
            uri=doc.get("uri"),
            media_type=doc.get("media_type", "application/octet-stream"),
            produced_by_task_id=doc.get("produced_by_task_id"),
            requirement_ids=doc.get("requirement_ids", ()),
            metadata=doc.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class BuildRun:
    id: str
    project_id: str
    spec_revision_id: str
    architecture_revision_id: str
    status: RunStatus = RunStatus.DRAFT
    task_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    revision: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _stable_id(self.id, "run.id"))
        object.__setattr__(
            self, "project_id", _stable_id(self.project_id, "run.project_id")
        )
        object.__setattr__(
            self,
            "spec_revision_id",
            _stable_id(self.spec_revision_id, "run.spec_revision_id"),
        )
        object.__setattr__(
            self,
            "architecture_revision_id",
            _stable_id(
                self.architecture_revision_id, "run.architecture_revision_id"
            ),
        )
        object.__setattr__(self, "status", _enum(self.status, RunStatus, "run.status"))
        for field_name in ("task_ids", "evidence_ids", "artifact_ids"):
            object.__setattr__(
                self,
                field_name,
                _strings(
                    getattr(self, field_name),
                    f"run.{field_name}",
                    stable_ids=True,
                ),
            )
        object.__setattr__(
            self, "revision", _positive_revision(self.revision, "run.revision")
        )
        object.__setattr__(
            self, "metadata", _json_mapping(self.metadata, "run.metadata")
        )

    def transitioned(self, status: RunStatus | str) -> "BuildRun":
        target = _transition(
            self.status, status, RunStatus, _RUN_TRANSITIONS, "run status"
        )
        return replace(self, status=target, revision=self.revision + 1)

    def validate_records(
        self,
        *,
        tasks: Iterable[BuildTask],
        evidence: Iterable[Evidence],
        artifacts: Iterable[Artifact],
        required_requirement_ids: Iterable[str] = (),
        required_acceptance_scenario_ids: Iterable[str] = (),
    ) -> None:
        task_index = _unique_by_id(tasks, "run tasks")
        evidence_index = _unique_by_id(evidence, "run evidence")
        artifact_index = _unique_by_id(artifacts, "run artifacts")
        declared = (
            ("task", set(self.task_ids), set(task_index)),
            ("evidence", set(self.evidence_ids), set(evidence_index)),
            ("artifact", set(self.artifact_ids), set(artifact_index)),
        )
        for label, declared_ids, actual_ids in declared:
            if declared_ids != actual_ids:
                raise ModelValidationError(
                    f"run {label} ids do not match records; "
                    f"missing={sorted(declared_ids - actual_ids)}, "
                    f"undeclared={sorted(actual_ids - declared_ids)}"
                )

        for task in task_index.values():
            if task.run_id != self.id:
                raise ModelValidationError(
                    f"task {task.id!r} belongs to run {task.run_id!r}, not {self.id!r}"
                )
            unknown_dependencies = set(task.dependency_task_ids) - task_index.keys()
            if unknown_dependencies:
                raise ModelValidationError(
                    f"task {task.id!r} has unknown dependencies: "
                    f"{sorted(unknown_dependencies)}"
                )

        visited: set[str] = set()
        visiting: set[str] = set()

        def visit_task(task_id: str) -> None:
            if task_id in visiting:
                raise ModelValidationError(
                    f"build task dependencies form a cycle through {task_id!r}"
                )
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency_id in task_index[task_id].dependency_task_ids:
                visit_task(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_index:
            visit_task(task_id)

        for artifact in artifact_index.values():
            if artifact.run_id != self.id:
                raise ModelValidationError(
                    f"artifact {artifact.id!r} belongs to another run"
                )
            if (
                artifact.produced_by_task_id is not None
                and artifact.produced_by_task_id not in task_index
            ):
                raise ModelValidationError(
                    f"artifact {artifact.id!r} references unknown producing task "
                    f"{artifact.produced_by_task_id!r}"
                )

        for record in evidence_index.values():
            if record.run_id != self.id:
                raise ModelValidationError(
                    f"evidence {record.id!r} belongs to another run"
                )
            if record.task_id is not None and record.task_id not in task_index:
                raise ModelValidationError(
                    f"evidence {record.id!r} references unknown task "
                    f"{record.task_id!r}"
                )
            unknown_artifacts = set(record.artifact_ids) - artifact_index.keys()
            if unknown_artifacts:
                raise ModelValidationError(
                    f"evidence {record.id!r} references unknown artifacts: "
                    f"{sorted(unknown_artifacts)}"
                )

        required_requirements = set(
            _strings(
                required_requirement_ids,
                "required_requirement_ids",
                stable_ids=True,
            )
        )
        required_scenarios = set(
            _strings(
                required_acceptance_scenario_ids,
                "required_acceptance_scenario_ids",
                stable_ids=True,
            )
        )
        passed_acceptance = [
            record
            for record in evidence_index.values()
            if record.kind is EvidenceKind.ACCEPTANCE and record.satisfies_gate
        ]
        covered_requirements = {
            requirement_id
            for record in passed_acceptance
            for requirement_id in record.requirement_ids
        }
        covered_scenarios = {
            scenario_id
            for record in passed_acceptance
            for scenario_id in record.acceptance_scenario_ids
        }
        if required_requirements - covered_requirements:
            raise ModelValidationError(
                "passed acceptance evidence does not cover requirements: "
                f"{sorted(required_requirements - covered_requirements)}"
            )
        if required_scenarios - covered_scenarios:
            raise ModelValidationError(
                "passed acceptance evidence does not cover scenarios: "
                f"{sorted(required_scenarios - covered_scenarios)}"
            )

        if self.status is RunStatus.SUCCEEDED:
            unfinished = {
                task.id: task.status.value
                for task in task_index.values()
                if task.status not in {TaskStatus.SUCCEEDED, TaskStatus.CACHED}
            }
            if unfinished:
                raise ModelValidationError(
                    f"succeeded run has unfinished tasks: {unfinished}"
                )
            unsatisfied = {
                record.id: record.status.value
                for record in evidence_index.values()
                if record.blocking and not record.satisfies_gate
            }
            if unsatisfied:
                raise ModelValidationError(
                    f"succeeded run has unsatisfied blocking evidence: {unsatisfied}"
                )
            if not any(record.blocking for record in evidence_index.values()):
                raise ModelValidationError(
                    "succeeded run requires at least one blocking evidence record"
                )

    def to_dict(self) -> dict[str, Any]:
        return _serialized(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BuildRun":
        doc = _strict_fields(
            data,
            label="BuildRun",
            required={
                "schema_version",
                "id",
                "project_id",
                "spec_revision_id",
                "architecture_revision_id",
                "status",
            },
            optional={
                "task_ids",
                "evidence_ids",
                "artifact_ids",
                "revision",
                "metadata",
            },
        )
        _check_schema_version(doc, "BuildRun")
        return cls(
            id=doc["id"],
            project_id=doc["project_id"],
            spec_revision_id=doc["spec_revision_id"],
            architecture_revision_id=doc["architecture_revision_id"],
            status=doc["status"],
            task_ids=doc.get("task_ids", ()),
            evidence_ids=doc.get("evidence_ids", ()),
            artifact_ids=doc.get("artifact_ids", ()),
            revision=doc.get("revision", 1),
            metadata=doc.get("metadata", {}),
        )


def validate_release_traceability(
    *,
    project: ProjectSpec,
    architecture: ArchitectureSpec,
    run: BuildRun,
    tasks: Iterable[BuildTask],
    evidence: Iterable[Evidence],
    artifacts: Iterable[Artifact],
) -> None:
    """Validate the complete requirement-to-release proof chain."""

    if run.status is not RunStatus.SUCCEEDED:
        raise ModelValidationError("only a succeeded run can be release-ready")
    if run.project_id != project.id:
        raise ModelValidationError(
            f"run project {run.project_id!r} does not match {project.id!r}"
        )
    architecture.validate_against_project(project)

    evidence_records = tuple(evidence)
    artifact_records = tuple(artifacts)
    known_requirements = set(project.requirement_index)
    known_scenarios = set(project.scenario_index)
    for record in evidence_records:
        unknown_requirements = set(record.requirement_ids) - known_requirements
        unknown_scenarios = set(record.acceptance_scenario_ids) - known_scenarios
        if unknown_requirements:
            raise ModelValidationError(
                f"evidence {record.id!r} traces unknown requirements: "
                f"{sorted(unknown_requirements)}"
            )
        if unknown_scenarios:
            raise ModelValidationError(
                f"evidence {record.id!r} traces unknown scenarios: "
                f"{sorted(unknown_scenarios)}"
            )
        for scenario_id in record.acceptance_scenario_ids:
            scenario_requirements = set(
                project.scenario_index[scenario_id].requirement_ids
            )
            if not scenario_requirements <= set(record.requirement_ids):
                raise ModelValidationError(
                    f"evidence {record.id!r} does not trace every requirement "
                    f"of scenario {scenario_id!r}"
                )

    run.validate_records(
        tasks=tasks,
        evidence=evidence_records,
        artifacts=artifact_records,
        required_requirement_ids=known_requirements,
        required_acceptance_scenario_ids=known_scenarios,
    )
    release_artifacts = [
        artifact
        for artifact in artifact_records
        if artifact.status is ArtifactStatus.AVAILABLE
        and artifact.kind in {ArtifactKind.SOURCE, ArtifactKind.BUNDLE}
    ]
    if not release_artifacts:
        raise ModelValidationError(
            "release-ready run needs an available source or bundle artifact"
        )
