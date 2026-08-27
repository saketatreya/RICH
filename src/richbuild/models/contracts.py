"""Operations, proof obligations, invariants, and contracts."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Iterable, Mapping

from ._common import (
    ModelValidationError,
    ObligationRelation,
    ObligationTier,
    SCHEMA_VERSION,
    ValueTypeKind,
    _check_schema_version,
    _enum,
    _json_mapping,
    _json_value,
    _models,
    _non_negative_int,
    _positive_revision,
    _serialized,
    _stable_id,
    _strict_fields,
    _strings,
    _text,
    _unique_by_id,
)
from .types import (
    ErrorContract,
    ValueType,
)



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

