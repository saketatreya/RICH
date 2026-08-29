"""The value-type vocabulary that contracts are written in."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import date as _date, datetime as _datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Iterable, Mapping, Sequence

from ._common import (
    CharSet,
    ModelValidationError,
    ValueTypeKind,
    _enum,
    _json_mapping,
    _models,
    _non_negative_int,
    _serialized,
    _stable_id,
    _strict_fields,
    _strings,
    _text,
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
# The persistence kinds. An identifier is an opaque slug of bounded length,
# the shape ids take in running software (uuids, "task-1"); it may name the
# entity it refers to, which is documentation for a reader and a hook for a
# later relation, not a check performed here. A timestamp is an RFC 3339 UTC
# instant ending in Z, a date an ISO calendar date. A decimal is a string --
# never a float -- with a declared precision and scale, so the invariant
# "money is a decimal string" reaches generated software: the checker counts
# digits after normalisation, and the property gate compares after it too.
MAX_IDENTIFIER_LENGTH = 64
MAX_DECIMAL_PRECISION = 38
DEFAULT_DECIMAL_PRECISION = 18
DEFAULT_DECIMAL_SCALE = 2
_ENTITY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_DECIMAL_RE = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
_TIMESTAMP_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z$"
)
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


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
    ValueTypeKind.IDENTIFIER: frozenset({"entity"}),
    ValueTypeKind.TIMESTAMP: frozenset(),
    ValueTypeKind.DATE: frozenset(),
    ValueTypeKind.DECIMAL: frozenset({"precision", "scale"}),
}
_VALUE_TYPE_REQUIRED_SLOTS: dict[ValueTypeKind, frozenset[str]] = {
    ValueTypeKind.ENUM: frozenset({"members"}),
    ValueTypeKind.LIST: frozenset({"element"}),
    ValueTypeKind.RECORD: frozenset({"record_fields"}),
    ValueTypeKind.OPTIONAL: frozenset({"element"}),
    # A decimal without a precision and scale is a float with extra steps.
    ValueTypeKind.DECIMAL: frozenset({"precision", "scale"}),
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
    "entity": None,
    "precision": None,
    "scale": None,
}


@dataclass(frozen=True, slots=True)
class ValueType:
    """A closed type language for operation inputs and outputs.

    JSON Schema is expressive enough to describe almost anything and therefore
    cannot be quantified over: you cannot compile ``forall x`` against a schema
    without a nameable type, nor sample from it without a generatable domain.
    This language is small on purpose -- eleven kinds, bounded nesting, no
    regular expressions, no recursion -- because every construct here has to
    have one meaning in a property test and the same meaning in a theorem.
    The four persistence kinds (identifier, timestamp, date, decimal) are
    scalars with a fixed grammar each, so they add no bounds to derive.

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
    entity: str | None = None
    precision: int | None = None
    scale: int | None = None

    def __post_init__(self) -> None:
        kind = _enum(self.kind, ValueTypeKind, "value_type.kind")
        object.__setattr__(self, "kind", kind)
        for name in ("minimum", "maximum"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                raise ModelValidationError(f"value_type.{name} must be an integer")
        if self.entity is not None and (
            not isinstance(self.entity, str) or not _ENTITY_RE.fullmatch(self.entity)
        ):
            raise ModelValidationError(
                "value_type.entity must be a 1-64 character name starting with a "
                "letter and containing only letters, digits or '_'"
            )
        for name in ("precision", "scale"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self, name, _non_negative_int(value, f"value_type.{name}")
                )
        if self.precision is not None and not (
            1 <= self.precision <= MAX_DECIMAL_PRECISION
        ):
            raise ModelValidationError(
                f"value_type.precision must be between 1 and {MAX_DECIMAL_PRECISION}"
            )
        if (
            self.scale is not None
            and self.precision is not None
            and self.scale > self.precision
        ):
            raise ModelValidationError("value_type.scale cannot exceed precision")
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
        if kind in (ValueTypeKind.BOOLEAN, ValueTypeKind.ENUM) or kind in _SCALAR_GRAMMARS:
            # The persistence kinds are bounded by their grammar: an identifier
            # by its length, a decimal by its precision, an instant and a date
            # by the range the sampler draws from.
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
        if kind in _SCALAR_GRAMMARS:
            # Finite, but nowhere near enumerable: 64 slug characters, every
            # instant, every decimal of a given precision.
            return None
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
        if kind is ValueTypeKind.IDENTIFIER:
            if not isinstance(value, str):
                return f"{path} must be an identifier string, got {_type_name(value)}"
            if not 1 <= len(value) <= MAX_IDENTIFIER_LENGTH:
                return (
                    f"{path} is {len(value)} characters; an identifier is 1 to "
                    f"{MAX_IDENTIFIER_LENGTH}"
                )
            stray = sorted(set(value) - set(CharSet.ASCII_SLUG.alphabet))
            if stray:
                return (
                    f"{path} contains characters an identifier cannot: "
                    f"{''.join(stray)!r} (letters, digits, '-' and '_' only)"
                )
            return None
        if kind is ValueTypeKind.TIMESTAMP:
            if not isinstance(value, str):
                return f"{path} must be a timestamp string, got {_type_name(value)}"
            return _explain_timestamp(value, path)
        if kind is ValueTypeKind.DATE:
            if not isinstance(value, str):
                return f"{path} must be a date string, got {_type_name(value)}"
            return _explain_date(value, path)
        if kind is ValueTypeKind.DECIMAL:
            assert self.precision is not None and self.scale is not None
            if not isinstance(value, str):
                return (
                    f"{path} must be a decimal string such as '12.50', got "
                    f"{_type_name(value)}"
                )
            return _explain_decimal(value, path, self.precision, self.scale)
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
        if kind is ValueTypeKind.DECIMAL:
            # Widen the scale to the most decimal places any example carries
            # and the precision to fit the widest integer part beside it. An
            # example that would need more than the language allows is left to
            # inhabitation to refuse, with its reason.
            assert self.precision is not None and self.scale is not None
            scale, integer_digits = self.scale, self.precision - self.scale
            for text in samples:
                digits = _decimal_digits(text)
                if digits is None:
                    continue
                whole, fraction = digits
                scale = max(scale, fraction)
                integer_digits = max(integer_digits, whole)
            if scale + integer_digits > MAX_DECIMAL_PRECISION:
                return self
            return replace(self, precision=scale + integer_digits, scale=scale)
        # boolean, enum, identifier, timestamp and date carry no bounds to
        # derive; an enum's members are the author's own vocabulary and
        # widening them would invent product meaning.
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
        if kind is ValueTypeKind.IDENTIFIER:
            return {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_IDENTIFIER_LENGTH,
                "pattern": "^[A-Za-z0-9_-]+$",
            }
        if kind is ValueTypeKind.TIMESTAMP:
            return {"type": "string", "format": "date-time"}
        if kind is ValueTypeKind.DATE:
            return {"type": "string", "format": "date"}
        if kind is ValueTypeKind.DECIMAL:
            # The precision and scale are the type's, not the schema's: JSON
            # Schema has no spelling for them that is not a regular
            # expression, and this type is the source of truth.
            return {"type": "string", "pattern": _DECIMAL_RE.pattern}
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
                "entity",
                "precision",
                "scale",
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
            entity=doc.get("entity"),
            precision=doc.get("precision"),
            scale=doc.get("scale"),
        )


# Kinds whose values have one fixed grammar: nothing to bound, nothing to
# derive, and a sampler that draws from a representative range.
_SCALAR_GRAMMARS = frozenset(
    {
        ValueTypeKind.IDENTIFIER,
        ValueTypeKind.TIMESTAMP,
        ValueTypeKind.DATE,
        ValueTypeKind.DECIMAL,
    }
)


def _explain_timestamp(value: str, path: str) -> str | None:
    match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        return (
            f"{path} must be an RFC 3339 UTC instant such as "
            f"'2026-08-29T12:00:00Z', got {value!r}"
        )
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    try:
        _datetime(year, month, day, hour, minute, second)
    except ValueError:
        return f"{path} is not a real instant: {value!r}"
    return None


def _explain_date(value: str, path: str) -> str | None:
    match = _DATE_RE.fullmatch(value)
    if match is None:
        return f"{path} must be an ISO calendar date such as '2026-08-29', got {value!r}"
    try:
        _date(*(int(part) for part in match.groups()))
    except ValueError:
        return f"{path} is not a real calendar date: {value!r}"
    return None


def _decimal_digits(value: Any) -> tuple[int, int] | None:
    """(integer digits, fractional digits) of a decimal string after
    normalisation, or ``None`` when it is not one."""

    if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    if number == 0:
        return 0, 0
    _, digits, exponent = number.normalize().as_tuple()
    assert isinstance(exponent, int)
    fraction = max(0, -exponent)
    whole = max(0, len(digits) + exponent)
    return whole, fraction


def _explain_decimal(value: str, path: str, precision: int, scale: int) -> str | None:
    digits = _decimal_digits(value)
    if digits is None:
        return (
            f"{path} must be a decimal string of digits with one optional '.' "
            f"and an optional leading '-', such as '12.50', got {value!r}"
        )
    whole, fraction = digits
    if fraction > scale:
        return (
            f"{path} has {fraction} decimal places, over the scale of {scale}"
        )
    if whole > precision - scale:
        return (
            f"{path} has {whole} integer digits, over the {precision - scale} "
            f"that precision {precision} and scale {scale} allow"
        )
    return None


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
    scalar_kinds = [
        "boolean",
        "integer",
        "string",
        "enum",
        "identifier",
        "timestamp",
        "date",
        "decimal",
    ]
    compound_kinds = ["list", "record", "optional"] if depth > 1 else []
    # Deliberately absent: minimum, maximum, min_length, max_length, char_set.
    # Every one is derived by ``ValueType.fitted_to`` from the examples the
    # same answer supplies, because a declared bound and a declared example are
    # two guesses at one intent and the example is the one carrying meaning.
    # Five of six measured live failures were a bound refusing its own example.
    # Present: entity, precision and scale. An identifier's entity is a name
    # only the author knows, and a decimal's precision and scale are a
    # declaration about the quantity -- money has two places because it is
    # money -- which fitting can widen to admit an example but should not
    # have to invent.
    properties: dict[str, Any] = {
        "kind": {"type": "string", "enum": scalar_kinds + compound_kinds},
        "members": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_ENUM_MEMBERS,
        },
        "entity": _nullable({"type": "string"}),
        "precision": _nullable(
            {"type": "integer", "minimum": 1, "maximum": MAX_DECIMAL_PRECISION}
        ),
        "scale": _nullable(
            {"type": "integer", "minimum": 0, "maximum": MAX_DECIMAL_PRECISION}
        ),
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
        elif name in ("precision", "scale"):
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            trimmed[name] = max(0, min(int(value), MAX_DECIMAL_PRECISION))
        elif name == "entity":
            if isinstance(value, str) and _ENTITY_RE.fullmatch(value):
                trimmed[name] = value
        else:
            trimmed[name] = value
    if resolved is ValueTypeKind.DECIMAL:
        # An omitted declaration gets the money default, not a refusal; the
        # example the same answer supplies widens it through ``fitted_to``.
        trimmed.setdefault("precision", DEFAULT_DECIMAL_PRECISION)
        trimmed.setdefault("scale", DEFAULT_DECIMAL_SCALE)
        trimmed["precision"] = max(1, trimmed["precision"])
        trimmed["scale"] = min(trimmed["scale"], trimmed["precision"])
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


