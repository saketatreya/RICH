"""Shared vocabulary: enums, errors, and validation helpers."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any, Iterable, Mapping, TypeVar



SCHEMA_VERSION = "2.0"
_STABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class ModelValidationError(ValueError):
    """A document is structurally invalid or internally inconsistent."""


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
    # models layering and imports no sibling module, and a domain vocabulary that
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


