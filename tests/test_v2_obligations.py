import pytest

from rich_v2.models import (
    CharSet,
    ContractV2,
    ErrorContract,
    Invariant,
    ModelValidationError,
    ObligationExample,
    ObligationRelation,
    ObligationTier,
    OperationContract,
    ProofObligation,
    RecordField,
    ValueType,
    ValueTypeKind,
)


BOOLEAN = ValueType(kind=ValueTypeKind.BOOLEAN)
SMALL_INT = ValueType(kind=ValueTypeKind.INTEGER, minimum=0, maximum=9)
UNBOUNDED_INT = ValueType(kind=ValueTypeKind.INTEGER)
INT_LIST = ValueType(kind=ValueTypeKind.LIST, element=SMALL_INT, max_length=4)
SHORT_TEXT = ValueType(
    kind=ValueTypeKind.STRING, max_length=8, char_set=CharSet.ASCII_LETTERS
)


def _operation(
    operation_id,
    *,
    input_type=None,
    output_type=None,
    requirement_ids=("req.core",),
    errors=(),
):
    return OperationContract(
        id=operation_id,
        name=operation_id.replace(".", "_"),
        input_schema=(
            input_type.json_schema()
            if input_type is not None
            else {"type": "object"}
        ),
        output_schema=(
            output_type.json_schema()
            if output_type is not None
            else {"type": "object"}
        ),
        requirement_ids=requirement_ids,
        errors=errors,
        input_type=input_type,
        output_type=output_type,
    )


def _example(operation_id, argument, result, *, obligation_id=None):
    return ProofObligation(
        id=obligation_id or f"obl.{operation_id}.example",
        relation=ObligationRelation.EXAMPLE,
        subject_operation_id=operation_id,
        requirement_ids=("req.core",),
        example=ObligationExample(argument=argument, result=result),
    )


def _contract(operations, obligations=(), invariants=()):
    return ContractV2(
        id="contract.test",
        node_id="node.test",
        operations=operations,
        obligations=obligations,
        invariants=invariants,
    )


# --------------------------------------------------------------------------
# ValueType: the arity table
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "slot", "value"),
    [
        (ValueTypeKind.BOOLEAN, "minimum", 0),
        (ValueTypeKind.BOOLEAN, "members", ("a",)),
        (ValueTypeKind.INTEGER, "char_set", CharSet.ASCII_DIGITS),
        (ValueTypeKind.INTEGER, "max_length", 4),
        (ValueTypeKind.INTEGER, "element", BOOLEAN),
        (ValueTypeKind.STRING, "minimum", 0),
        (ValueTypeKind.STRING, "members", ("a",)),
        (ValueTypeKind.STRING, "record_fields", (RecordField("a", BOOLEAN),)),
        (ValueTypeKind.ENUM, "char_set", CharSet.ASCII_DIGITS),
        (ValueTypeKind.ENUM, "element", BOOLEAN),
        (ValueTypeKind.LIST, "minimum", 0),
        (ValueTypeKind.LIST, "members", ("a",)),
        (ValueTypeKind.RECORD, "element", BOOLEAN),
        (ValueTypeKind.RECORD, "max_length", 3),
        (ValueTypeKind.OPTIONAL, "max_length", 3),
        (ValueTypeKind.OPTIONAL, "members", ("a",)),
    ],
)
def test_every_value_type_kind_rejects_every_foreign_slot(kind, slot, value):
    base = {
        ValueTypeKind.ENUM: {"members": ("x",)},
        ValueTypeKind.LIST: {"element": BOOLEAN},
        ValueTypeKind.RECORD: {"record_fields": (RecordField("a", BOOLEAN),)},
        ValueTypeKind.OPTIONAL: {"element": BOOLEAN},
    }.get(kind, {})

    with pytest.raises(ModelValidationError, match="cannot carry"):
        ValueType(kind=kind, **base, **{slot: value})


@pytest.mark.parametrize(
    ("kind", "slot"),
    [
        (ValueTypeKind.ENUM, "members"),
        (ValueTypeKind.LIST, "element"),
        (ValueTypeKind.RECORD, "record_fields"),
        (ValueTypeKind.OPTIONAL, "element"),
    ],
)
def test_compound_value_type_kinds_require_their_operand(kind, slot):
    with pytest.raises(ModelValidationError, match="requires"):
        ValueType(kind=kind)


def test_value_type_bounds_must_be_coherent():
    with pytest.raises(ModelValidationError, match="minimum cannot exceed maximum"):
        ValueType(kind=ValueTypeKind.INTEGER, minimum=5, maximum=4)
    with pytest.raises(ModelValidationError, match="min_length cannot exceed"):
        ValueType(kind=ValueTypeKind.STRING, min_length=5, max_length=4)


def test_value_type_nesting_is_bounded():
    depth_four = ValueType(
        kind=ValueTypeKind.LIST,
        element=ValueType(
            kind=ValueTypeKind.LIST,
            element=ValueType(kind=ValueTypeKind.LIST, element=BOOLEAN),
        ),
    )
    assert depth_four.depth == 4

    with pytest.raises(ModelValidationError, match="depth"):
        ValueType(kind=ValueTypeKind.LIST, element=depth_four)


def test_optional_cannot_nest_an_optional():
    with pytest.raises(ModelValidationError, match="cannot nest an optional"):
        ValueType(
            kind=ValueTypeKind.OPTIONAL,
            element=ValueType(kind=ValueTypeKind.OPTIONAL, element=BOOLEAN),
        )


@pytest.mark.parametrize("name", ["", "1leading", "has-dash", "has.dot", "a" * 65])
def test_record_field_names_are_restricted_to_portable_identifiers(name):
    with pytest.raises(ModelValidationError, match="record_field.name"):
        RecordField(name=name, value_type=BOOLEAN)


def test_record_field_names_must_be_unique():
    with pytest.raises(ModelValidationError, match="duplicate names"):
        ValueType(
            kind=ValueTypeKind.RECORD,
            record_fields=(
                RecordField("value", BOOLEAN),
                RecordField("value", SMALL_INT),
            ),
        )


# --------------------------------------------------------------------------
# ValueType: the derived properties the gates depend on
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value_type", "sampleable"),
    [
        (BOOLEAN, True),
        (SMALL_INT, True),
        (UNBOUNDED_INT, False),
        (SHORT_TEXT, True),
        (ValueType(kind=ValueTypeKind.STRING), False),
        (INT_LIST, True),
        (ValueType(kind=ValueTypeKind.LIST, element=SMALL_INT), False),
        (ValueType(kind=ValueTypeKind.LIST, element=UNBOUNDED_INT, max_length=2), False),
        (ValueType(kind=ValueTypeKind.OPTIONAL, element=UNBOUNDED_INT), False),
        (ValueType(kind=ValueTypeKind.ENUM, members=("a", "b")), True),
        (
            ValueType(
                kind=ValueTypeKind.RECORD,
                record_fields=(
                    RecordField("ok", BOOLEAN),
                    RecordField("count", UNBOUNDED_INT),
                ),
            ),
            False,
        ),
    ],
)
def test_finite_sampleability_is_derived_from_the_bounds(value_type, sampleable):
    assert value_type.is_finitely_sampleable is sampleable


def test_cardinality_is_separate_from_sampleability():
    # A bounded string is drawable but nowhere near enumerable, which is why
    # the sample gate and an exhaustive proof tier ask different questions.
    wide = ValueType(
        kind=ValueTypeKind.STRING, max_length=64, char_set=CharSet.ASCII_PRINTABLE
    )
    assert wide.is_finitely_sampleable is True
    assert wide.cardinality_bound is None

    assert BOOLEAN.cardinality_bound == 2
    assert SMALL_INT.cardinality_bound == 10
    assert UNBOUNDED_INT.cardinality_bound is None
    assert ValueType(kind=ValueTypeKind.ENUM, members=("a", "b", "c")).cardinality_bound == 3
    assert ValueType(kind=ValueTypeKind.OPTIONAL, element=BOOLEAN).cardinality_bound == 3
    assert ValueType(
        kind=ValueTypeKind.RECORD,
        record_fields=(RecordField("a", BOOLEAN), RecordField("b", SMALL_INT)),
    ).cardinality_bound == 20


@pytest.mark.parametrize(
    ("value_type", "value", "accepted"),
    [
        (BOOLEAN, True, True),
        (BOOLEAN, 1, False),
        (SMALL_INT, 9, True),
        (SMALL_INT, 10, False),
        (SMALL_INT, True, False),
        (SHORT_TEXT, "abc", True),
        (SHORT_TEXT, "abc1", False),
        (SHORT_TEXT, "abcdefghi", False),
        (ValueType(kind=ValueTypeKind.ENUM, members=("a",)), "a", True),
        (ValueType(kind=ValueTypeKind.ENUM, members=("a",)), "b", False),
        (INT_LIST, [1, 2], True),
        (INT_LIST, [1, 20], False),
        (INT_LIST, [1, 2, 3, 4, 5], False),
        (ValueType(kind=ValueTypeKind.OPTIONAL, element=SMALL_INT), None, True),
        (ValueType(kind=ValueTypeKind.OPTIONAL, element=SMALL_INT), 3, True),
        (
            ValueType(
                kind=ValueTypeKind.RECORD, record_fields=(RecordField("ok", BOOLEAN),)
            ),
            {"ok": False},
            True,
        ),
        (
            ValueType(
                kind=ValueTypeKind.RECORD, record_fields=(RecordField("ok", BOOLEAN),)
            ),
            {"ok": False, "extra": 1},
            False,
        ),
        (
            ValueType(
                kind=ValueTypeKind.RECORD, record_fields=(RecordField("ok", BOOLEAN),)
            ),
            {},
            False,
        ),
    ],
)
def test_membership_is_decided_structurally(value_type, value, accepted):
    assert value_type.accepts(value) is accepted


def test_json_schema_is_a_projection_and_round_trips_through_serialization():
    record = ValueType(
        kind=ValueTypeKind.RECORD,
        record_fields=(
            RecordField("ok", BOOLEAN),
            RecordField("items", INT_LIST),
            RecordField("note", ValueType(kind=ValueTypeKind.OPTIONAL, element=SHORT_TEXT)),
        ),
    )

    assert record.json_schema() == {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "items": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0, "maximum": 9},
                "maxItems": 4,
            },
            "note": {
                "anyOf": [{"type": "string", "maxLength": 8}, {"type": "null"}]
            },
        },
        "required": ["ok", "items", "note"],
        "additionalProperties": False,
    }
    assert ValueType.from_dict(record.to_dict()) == record


def test_an_operation_schema_cannot_disagree_with_its_declared_type():
    with pytest.raises(ModelValidationError, match="JSON Schema projection"):
        OperationContract(
            id="op.mismatch",
            name="mismatch",
            input_schema={"type": "string"},
            output_schema=BOOLEAN.json_schema(),
            requirement_ids=("req.core",),
            input_type=SMALL_INT,
            output_type=BOOLEAN,
        )


# --------------------------------------------------------------------------
# ProofObligation: the arity table
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relation", "slot"),
    [
        (ObligationRelation.EXAMPLE, "witness_operation_id"),
        (ObligationRelation.EXAMPLE, "predicate_operation_id"),
        (ObligationRelation.EXAMPLE, "guard_operation_id"),
        (ObligationRelation.TOTAL, "witness_operation_id"),
        (ObligationRelation.TOTAL, "predicate_operation_id"),
        (ObligationRelation.ROUND_TRIP, "predicate_operation_id"),
        (ObligationRelation.ROUND_TRIP, "guard_operation_id"),
        (ObligationRelation.IDEMPOTENT, "witness_operation_id"),
        (ObligationRelation.IDEMPOTENT, "predicate_operation_id"),
        (ObligationRelation.IDEMPOTENT, "guard_operation_id"),
        (ObligationRelation.PRESERVES, "witness_operation_id"),
        (ObligationRelation.PRESERVES, "guard_operation_id"),
        (ObligationRelation.ESTABLISHES, "witness_operation_id"),
        (ObligationRelation.ESTABLISHES, "guard_operation_id"),
    ],
)
def test_every_relation_rejects_every_foreign_operand(relation, slot):
    base = {
        ObligationRelation.EXAMPLE: {
            "example": ObligationExample(argument=1, result=1)
        },
        ObligationRelation.ROUND_TRIP: {"witness_operation_id": "op.witness"},
        ObligationRelation.PRESERVES: {"predicate_operation_id": "op.predicate"},
        ObligationRelation.ESTABLISHES: {"predicate_operation_id": "op.predicate"},
    }.get(relation, {})

    with pytest.raises(ModelValidationError, match="cannot carry"):
        ProofObligation(
            id="obl.arity",
            relation=relation,
            subject_operation_id="op.subject",
            requirement_ids=("req.core",),
            sample_size=None if relation is ObligationRelation.EXAMPLE else 8,
            **base,
            **{slot: "op.stray"},
        )


@pytest.mark.parametrize(
    ("relation", "slot"),
    [
        (ObligationRelation.EXAMPLE, "example"),
        (ObligationRelation.ROUND_TRIP, "witness_operation_id"),
        (ObligationRelation.PRESERVES, "predicate_operation_id"),
        (ObligationRelation.ESTABLISHES, "predicate_operation_id"),
    ],
)
def test_relations_requiring_an_operand_reject_its_absence(relation, slot):
    with pytest.raises(ModelValidationError, match="requires"):
        ProofObligation(
            id="obl.missing",
            relation=relation,
            subject_operation_id="op.subject",
            requirement_ids=("req.core",),
            sample_size=None if relation is ObligationRelation.EXAMPLE else 8,
        )


def test_totality_may_omit_its_optional_guard():
    obligation = ProofObligation(
        id="obl.total",
        relation=ObligationRelation.TOTAL,
        subject_operation_id="op.subject",
        requirement_ids=("req.core",),
        tier=ObligationTier.PROOF,
    )

    assert obligation.guard_operation_id is None
    assert obligation.operand_operation_ids == ("op.subject",)


def test_example_obligations_are_one_sample_and_cannot_claim_more():
    with pytest.raises(ModelValidationError, match="cannot claim a proof tier"):
        ProofObligation(
            id="obl.e",
            relation=ObligationRelation.EXAMPLE,
            subject_operation_id="op.subject",
            requirement_ids=("req.core",),
            tier=ObligationTier.PROOF,
            example=ObligationExample(argument=1, result=1),
        )
    with pytest.raises(ModelValidationError, match="cannot carry a sample size"):
        ProofObligation(
            id="obl.e",
            relation=ObligationRelation.EXAMPLE,
            subject_operation_id="op.subject",
            requirement_ids=("req.core",),
            example=ObligationExample(argument=1, result=1),
            sample_size=4,
        )


def test_sample_and_proof_tiers_disagree_about_sample_size():
    with pytest.raises(ModelValidationError, match="require a sample size"):
        ProofObligation(
            id="obl.i",
            relation=ObligationRelation.IDEMPOTENT,
            subject_operation_id="op.subject",
            requirement_ids=("req.core",),
        )
    with pytest.raises(ModelValidationError, match="cannot carry a sample size"):
        ProofObligation(
            id="obl.i",
            relation=ObligationRelation.IDEMPOTENT,
            subject_operation_id="op.subject",
            requirement_ids=("req.core",),
            tier=ObligationTier.PROOF,
            sample_size=8,
        )


@pytest.mark.parametrize("size", [0, 4097])
def test_sample_size_is_bounded(size):
    with pytest.raises(ModelValidationError, match="sample_size"):
        ProofObligation(
            id="obl.i",
            relation=ObligationRelation.IDEMPOTENT,
            subject_operation_id="op.subject",
            requirement_ids=("req.core",),
            sample_size=size,
        )


def test_obligations_round_trip_through_serialization():
    obligation = ProofObligation(
        id="obl.round",
        relation=ObligationRelation.ROUND_TRIP,
        subject_operation_id="op.encode",
        witness_operation_id="op.decode",
        requirement_ids=("req.core",),
        tier=ObligationTier.PROOF,
        description="Decoding an encoded value returns it unchanged.",
    )

    assert ProofObligation.from_dict(obligation.to_dict()) == obligation


# --------------------------------------------------------------------------
# The contract-level checks that make an obligation mean something
# --------------------------------------------------------------------------


def test_an_operation_constrained_without_an_example_is_rejected_as_vacuous():
    normalize = _operation("op.normalize", input_type=INT_LIST, output_type=INT_LIST)
    idempotent = ProofObligation(
        id="obl.normalize.idempotent",
        relation=ObligationRelation.IDEMPOTENT,
        subject_operation_id="op.normalize",
        requirement_ids=("req.core",),
        sample_size=32,
    )

    with pytest.raises(ModelValidationError, match="unanchored operations"):
        _contract((normalize,), (idempotent,))

    # The identity function satisfies IDEMPOTENT; one ground example is what
    # rules it out, so the same contract is valid once anchored.
    contract = _contract(
        (normalize,),
        (idempotent, _example("op.normalize", [2, 1, 2], [1, 2])),
    )
    assert set(contract.obligation_index) == {
        "obl.normalize.idempotent",
        "obl.op.normalize.example",
    }


def test_a_predicate_or_guard_must_be_anchored_too_not_just_the_subject():
    # Anchoring only the subject leaves the claim trivialisable from the other
    # side: an always-true predicate satisfies ESTABLISHES and PRESERVES
    # whatever the subject does, and an always-false guard satisfies TOTAL by
    # excluding every case. Both are exactly as vacuous as an unanchored
    # subject, so the rule covers every operation an obligation names.
    render = _operation("op.render", input_type=SMALL_INT, output_type=SHORT_TEXT)
    predicate = _operation("op.lower", input_type=SHORT_TEXT, output_type=BOOLEAN)
    establishes = ProofObligation(
        id="obl.render.establishes",
        relation=ObligationRelation.ESTABLISHES,
        subject_operation_id="op.render",
        predicate_operation_id="op.lower",
        requirement_ids=("req.core",),
        sample_size=8,
    )
    subject_anchor = _example("op.render", 1, "b")

    with pytest.raises(ModelValidationError, match="op.lower"):
        _contract((render, predicate), (establishes, subject_anchor))

    predicate_anchor = _example(
        "op.lower", "b", True, obligation_id="obl.lower.example"
    )
    assert _contract(
        (render, predicate), (establishes, subject_anchor, predicate_anchor)
    ).obligations

    divide = _operation(
        "op.divide",
        input_type=SMALL_INT,
        output_type=SMALL_INT,
        errors=(ErrorContract(id="err.zero", code="ZERO", description="No zero."),),
    )
    guard = _operation("op.nonzero", input_type=SMALL_INT, output_type=BOOLEAN)
    total = ProofObligation(
        id="obl.divide.total",
        relation=ObligationRelation.TOTAL,
        subject_operation_id="op.divide",
        guard_operation_id="op.nonzero",
        requirement_ids=("req.core",),
        sample_size=8,
    )

    with pytest.raises(ModelValidationError, match="op.nonzero"):
        _contract((divide, guard), (total, _example("op.divide", 4, 2)))


def test_totality_of_an_operation_that_cannot_fail_is_rejected():
    infallible = _operation("op.pure", input_type=SMALL_INT, output_type=SMALL_INT)
    total = ProofObligation(
        id="obl.pure.total",
        relation=ObligationRelation.TOTAL,
        subject_operation_id="op.pure",
        requirement_ids=("req.core",),
        sample_size=16,
    )

    with pytest.raises(ModelValidationError, match="vacuously true"):
        _contract((infallible,), (total, _example("op.pure", 1, 1)))

    fallible = _operation(
        "op.pure",
        input_type=SMALL_INT,
        output_type=SMALL_INT,
        errors=(ErrorContract(id="err.range", code="RANGE", description="Out of range."),),
    )
    assert _contract((fallible,), (total, _example("op.pure", 1, 1))).obligations


def test_a_sample_tier_over_an_unbounded_domain_is_rejected():
    unbounded = _operation(
        "op.scale", input_type=UNBOUNDED_INT, output_type=UNBOUNDED_INT
    )
    sampled = ProofObligation(
        id="obl.scale.idempotent",
        relation=ObligationRelation.IDEMPOTENT,
        subject_operation_id="op.scale",
        requirement_ids=("req.core",),
        sample_size=16,
    )
    anchor = _example("op.scale", 0, 0)

    with pytest.raises(ModelValidationError, match="no generator can draw from"):
        _contract((unbounded,), (sampled, anchor))

    # The same claim is admissible when it stops pretending to be sampled.
    proved = ProofObligation(
        id="obl.scale.idempotent",
        relation=ObligationRelation.IDEMPOTENT,
        subject_operation_id="op.scale",
        requirement_ids=("req.core",),
        tier=ObligationTier.PROOF,
    )
    assert _contract((unbounded,), (proved, anchor)).obligations


def test_round_trip_requires_the_witness_to_invert_the_subject():
    encode = _operation("op.encode", input_type=SMALL_INT, output_type=SHORT_TEXT)
    wrong = _operation("op.decode", input_type=SHORT_TEXT, output_type=BOOLEAN)
    obligation = ProofObligation(
        id="obl.codec",
        relation=ObligationRelation.ROUND_TRIP,
        subject_operation_id="op.encode",
        witness_operation_id="op.decode",
        requirement_ids=("req.core",),
        sample_size=16,
    )
    anchors = (
        _example("op.encode", 1, "b"),
        _example("op.decode", "b", 1, obligation_id="obl.decode.example"),
    )

    with pytest.raises(ModelValidationError, match="return the subject's input"):
        _contract((encode, wrong), (obligation, *anchors))

    swapped = _operation("op.decode", input_type=BOOLEAN, output_type=SMALL_INT)
    with pytest.raises(ModelValidationError, match="accept the subject's output"):
        _contract((encode, swapped), (obligation, _example("op.encode", 1, "b")))

    right = _operation("op.decode", input_type=SHORT_TEXT, output_type=SMALL_INT)
    assert _contract((encode, right), (obligation, *anchors)).obligations


def test_preserves_and_establishes_require_a_boolean_predicate_over_the_right_domain():
    insert = _operation("op.insert", input_type=INT_LIST, output_type=INT_LIST)
    anchor = _example("op.insert", [2], [2])
    sorted_anchor = _example(
        "op.sorted", [2], True, obligation_id="obl.sorted.example"
    )
    not_boolean = _operation("op.sorted", input_type=INT_LIST, output_type=SMALL_INT)
    preserves = ProofObligation(
        id="obl.insert.preserves",
        relation=ObligationRelation.PRESERVES,
        subject_operation_id="op.insert",
        predicate_operation_id="op.sorted",
        requirement_ids=("req.core",),
        sample_size=16,
    )

    with pytest.raises(ModelValidationError, match="return a boolean"):
        _contract((insert, not_boolean), (preserves, anchor))

    wrong_domain = _operation("op.sorted", input_type=SMALL_INT, output_type=BOOLEAN)
    with pytest.raises(ModelValidationError, match="same type it judges"):
        _contract((insert, wrong_domain), (preserves, anchor))

    sorted_predicate = _operation("op.sorted", input_type=INT_LIST, output_type=BOOLEAN)
    assert _contract(
        (insert, sorted_predicate), (preserves, anchor, sorted_anchor)
    ).obligations

    # ESTABLISHES judges the *output*, so its predicate accepts the output type.
    render = _operation("op.render", input_type=SMALL_INT, output_type=SHORT_TEXT)
    establishes = ProofObligation(
        id="obl.render.establishes",
        relation=ObligationRelation.ESTABLISHES,
        subject_operation_id="op.render",
        predicate_operation_id="op.lowercase",
        requirement_ids=("req.core",),
        sample_size=16,
    )
    judges_input = _operation("op.lowercase", input_type=SMALL_INT, output_type=BOOLEAN)
    with pytest.raises(ModelValidationError, match="same type it judges"):
        _contract(
            (render, judges_input), (establishes, _example("op.render", 1, "b"))
        )

    judges_output = _operation(
        "op.lowercase", input_type=SHORT_TEXT, output_type=BOOLEAN
    )
    assert _contract(
        (render, judges_output),
        (
            establishes,
            _example("op.render", 1, "b"),
            _example("op.lowercase", "b", True, obligation_id="obl.lower.example"),
        ),
    ).obligations


def test_idempotence_and_preservation_require_an_endomorphism():
    render = _operation("op.render", input_type=SMALL_INT, output_type=SHORT_TEXT)
    obligation = ProofObligation(
        id="obl.render.idempotent",
        relation=ObligationRelation.IDEMPOTENT,
        subject_operation_id="op.render",
        requirement_ids=("req.core",),
        sample_size=16,
    )

    with pytest.raises(ModelValidationError, match="endomorphism"):
        _contract((render,), (obligation, _example("op.render", 1, "b")))


@pytest.mark.parametrize(
    ("value_type", "value", "fragment"),
    [
        (BOOLEAN, 1, "must be a boolean, got an integer"),
        (SMALL_INT, "3", "must be an integer, got a string"),
        (SMALL_INT, 42, "is 42, above the maximum of 9"),
        (SMALL_INT, -1, "is -1, below the minimum of 0"),
        (SHORT_TEXT, "waaaaaaaaay too long", "characters, over the maximum of 8"),
        (SHORT_TEXT, "he llo!", "outside the 'ascii_letters' character set"),
        (INT_LIST, [1, 2, 3, 4, 5], "has 5 items, over the maximum of 4"),
        (INT_LIST, [1, 42], "[1] is 42, above the maximum"),
        (INT_LIST, "nope", "must be a list, got a string"),
        (
            ValueType(kind=ValueTypeKind.ENUM, members=("a", "b")),
            "z",
            "must be one of ['a', 'b'], got 'z'",
        ),
        (
            ValueType(
                kind=ValueTypeKind.RECORD,
                record_fields=(
                    RecordField("ok", BOOLEAN),
                    RecordField("n", SMALL_INT),
                ),
            ),
            {"ok": True},
            "missing required fields: ['n']",
        ),
        (
            ValueType(
                kind=ValueTypeKind.RECORD, record_fields=(RecordField("ok", BOOLEAN),)
            ),
            {"ok": True, "extra": 1},
            "fields the type does not declare: ['extra']",
        ),
        (
            ValueType(
                kind=ValueTypeKind.RECORD, record_fields=(RecordField("ok", BOOLEAN),)
            ),
            {"ok": "yes"},
            "value.ok must be a boolean",
        ),
    ],
)
def test_a_rejection_says_why_and_where_not_merely_that(value_type, value, fragment):
    # accepts() answers yes or no, which is enough to reject and useless for
    # repair. This message is read by a person correcting a contract and by a
    # model retrying against the validator, and neither can act on "does not
    # inhabit the declared type".
    reason = value_type.explain(value)

    assert reason is not None
    assert fragment in reason
    assert value_type.accepts(value) is False


def test_an_accepted_value_explains_nothing():
    assert INT_LIST.explain([1, 2]) is None
    assert SHORT_TEXT.explain("abc") is None
    assert ValueType(kind=ValueTypeKind.OPTIONAL, element=SMALL_INT).explain(None) is None


def test_the_obligation_rejection_carries_that_reason_through():
    operation = _operation("op.clamp", input_type=SMALL_INT, output_type=SMALL_INT)

    with pytest.raises(ModelValidationError, match="above the maximum of 9"):
        _contract((operation,), (_example("op.clamp", 42, 9),))


def test_example_values_must_inhabit_the_declared_types():
    operation = _operation("op.clamp", input_type=SMALL_INT, output_type=SMALL_INT)

    with pytest.raises(ModelValidationError, match="argument does not inhabit"):
        _contract((operation,), (_example("op.clamp", 42, 9),))
    with pytest.raises(ModelValidationError, match="result does not inhabit"):
        _contract((operation,), (_example("op.clamp", 4, 42),))
    assert _contract((operation,), (_example("op.clamp", 4, 4),)).obligations


def test_obligations_cannot_reference_operations_outside_the_contract():
    operation = _operation("op.clamp", input_type=SMALL_INT, output_type=SMALL_INT)
    obligation = ProofObligation(
        id="obl.clamp.preserves",
        relation=ObligationRelation.PRESERVES,
        subject_operation_id="op.clamp",
        predicate_operation_id="op.absent",
        requirement_ids=("req.core",),
        sample_size=8,
    )

    with pytest.raises(ModelValidationError, match="unknown operation"):
        _contract((operation,), (obligation, _example("op.clamp", 1, 1)))


def test_an_obligation_cannot_trace_a_requirement_its_subject_does_not():
    operation = _operation(
        "op.clamp",
        input_type=SMALL_INT,
        output_type=SMALL_INT,
        requirement_ids=("req.core",),
    )
    obligation = ProofObligation(
        id="obl.clamp.example",
        relation=ObligationRelation.EXAMPLE,
        subject_operation_id="op.clamp",
        requirement_ids=("req.elsewhere",),
        example=ObligationExample(argument=1, result=1),
    )

    with pytest.raises(ModelValidationError, match="its subject does not"):
        _contract((operation,), (obligation,))


def test_behavior_ids_stay_globally_unique_across_all_three_kinds():
    operation = _operation("op.clamp", input_type=SMALL_INT, output_type=SMALL_INT)
    clashing = _example("op.clamp", 1, 1, obligation_id="op.clamp")

    with pytest.raises(ModelValidationError, match="globally unique"):
        _contract((operation,), (clashing,))


def test_invariants_cite_obligations_and_keep_their_prose():
    operation = _operation("op.clamp", input_type=SMALL_INT, output_type=SMALL_INT)
    anchor = _example("op.clamp", 1, 1)
    invariant = Invariant(
        id="inv.bounded",
        statement="A clamped value never leaves the permitted range.",
        requirement_ids=("req.core",),
        obligation_ids=("obl.op.clamp.example",),
    )

    contract = _contract((operation,), (anchor,), (invariant,))
    assert contract.invariants[0].statement == (
        "A clamped value never leaves the permitted range."
    )
    assert contract.invariants[0].obligation_ids == ("obl.op.clamp.example",)

    with pytest.raises(ModelValidationError, match="cites unknown obligations"):
        _contract(
            (operation,),
            (anchor,),
            (
                Invariant(
                    id="inv.bounded",
                    statement="Unlinked.",
                    requirement_ids=("req.core",),
                    obligation_ids=("obl.absent",),
                ),
            ),
        )


def test_obligation_requirements_are_traced_and_contracts_round_trip():
    operation = _operation("op.clamp", input_type=SMALL_INT, output_type=SMALL_INT)
    contract = _contract((operation,), (_example("op.clamp", 1, 1),))

    assert contract.traced_requirement_ids == frozenset({"req.core"})
    contract.validate_requirement_ids({"req.core"})
    with pytest.raises(ModelValidationError, match="unknown requirements"):
        contract.validate_requirement_ids({"req.other"})
    assert ContractV2.from_dict(contract.to_dict()) == contract


def test_untyped_contracts_remain_valid_so_the_typed_view_can_arrive_gradually():
    operation = OperationContract(
        id="op.legacy",
        name="legacy",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        requirement_ids=("req.core",),
    )

    contract = _contract((operation,), (_example("op.legacy", {"a": 1}, {"b": 2}),))

    assert contract.operations[0].input_type is None
    assert contract.obligations[0].example.argument == {"a": 1}
