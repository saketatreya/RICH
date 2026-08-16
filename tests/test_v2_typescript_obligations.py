import json
import shutil
import subprocess

import pytest

from rich_v2.models import (
    CharSet,
    ContractV2,
    ErrorContract,
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
from rich_v2.target_packs.typescript_obligations import (
    GENERATOR_PATH,
    OPERATIONS_MODULE,
    VALUE_GENERATOR_SOURCE,
    ObligationCompileError,
    compile_obligation_suite,
    compile_operations_interface,
)


BOOLEAN = ValueType(kind=ValueTypeKind.BOOLEAN)
SMALL_INT = ValueType(kind=ValueTypeKind.INTEGER, minimum=-5, maximum=5)
WORD = ValueType(
    kind=ValueTypeKind.STRING,
    min_length=1,
    max_length=6,
    char_set=CharSet.ASCII_LETTERS,
)
INT_LIST = ValueType(kind=ValueTypeKind.LIST, element=SMALL_INT, max_length=4)
CHOICE = ValueType(kind=ValueTypeKind.ENUM, members=("red", "green", "blue"))
RECORD = ValueType(
    kind=ValueTypeKind.RECORD,
    record_fields=(
        RecordField("flag", BOOLEAN),
        RecordField("count", SMALL_INT),
        RecordField("note", ValueType(kind=ValueTypeKind.OPTIONAL, element=WORD)),
    ),
)


def _operation(operation_id, name, *, input_type, output_type, errors=()):
    return OperationContract(
        id=operation_id,
        name=name,
        input_schema=input_type.json_schema(),
        output_schema=output_type.json_schema(),
        requirement_ids=("req.core",),
        errors=errors,
        input_type=input_type,
        output_type=output_type,
    )


def _example(operation_id, name, argument, result):
    return ProofObligation(
        id=f"obl.{name}.example",
        relation=ObligationRelation.EXAMPLE,
        subject_operation_id=operation_id,
        requirement_ids=("req.core",),
        example=ObligationExample(argument=argument, result=result),
    )


def _contract(operations, obligations):
    return ContractV2(
        id="contract.props",
        node_id="node.props",
        operations=operations,
        obligations=obligations,
    )


# --------------------------------------------------------------------------
# Each relation compiles to code that could actually disagree with the model
# --------------------------------------------------------------------------


def test_example_compiles_to_a_single_ground_assertion():
    operation = _operation(
        "op.clamp", "clamp", input_type=SMALL_INT, output_type=SMALL_INT
    )

    suite = compile_obligation_suite(
        _contract((operation,), (_example("op.clamp", "clamp", 4, 2),))
    )

    assert "operations.clamp(4)" in suite
    assert "equals(actual, 2)" in suite
    # A single ground fact needs no case list -- and an import of the case
    # drawer it never calls would fail the typecheck gate this file runs beside.
    assert "casesFor" not in suite
    assert 'import { equals } from "./rich-value-generator";' in suite


def test_idempotence_applies_the_operation_twice():
    operation = _operation(
        "op.normalize", "normalize", input_type=INT_LIST, output_type=INT_LIST
    )
    obligation = ProofObligation(
        id="obl.normalize.idempotent",
        relation=ObligationRelation.IDEMPOTENT,
        subject_operation_id="op.normalize",
        requirement_ids=("req.core",),
        sample_size=32,
    )

    suite = compile_obligation_suite(
        _contract(
            (operation,),
            (obligation, _example("op.normalize", "normalize", [2, 1], [1, 2])),
        )
    )

    assert "const once = operations.normalize(value)" in suite
    assert "equals(operations.normalize(once), once)" in suite
    assert (
        'import { casesFor, equals, type ValueType } from "./rich-value-generator";'
        in suite
    )
    assert '"obl.normalize.idempotent"' in suite
    assert ", 32)" in suite


def test_round_trip_pipes_the_subject_through_its_witness():
    subject = _operation("op.encode", "encode", input_type=SMALL_INT, output_type=WORD)
    witness = _operation("op.decode", "decode", input_type=WORD, output_type=SMALL_INT)
    obligation = ProofObligation(
        id="obl.codec",
        relation=ObligationRelation.ROUND_TRIP,
        subject_operation_id="op.encode",
        witness_operation_id="op.decode",
        requirement_ids=("req.core",),
        sample_size=16,
    )

    suite = compile_obligation_suite(
        _contract(
            (subject, witness),
            (obligation, _example("op.encode", "encode", 1, "b")),
        )
    )

    assert "operations.decode(operations.encode(value))" in suite
    assert "equals(restored, value)" in suite


def test_preservation_skips_cases_the_predicate_rejects():
    subject = _operation(
        "op.insert", "insert", input_type=INT_LIST, output_type=INT_LIST
    )
    predicate = _operation(
        "op.sorted", "isSorted", input_type=INT_LIST, output_type=BOOLEAN
    )
    obligation = ProofObligation(
        id="obl.insert.preserves",
        relation=ObligationRelation.PRESERVES,
        subject_operation_id="op.insert",
        predicate_operation_id="op.sorted",
        requirement_ids=("req.core",),
        sample_size=64,
    )

    suite = compile_obligation_suite(
        _contract(
            (subject, predicate),
            (obligation, _example("op.insert", "insert", [1], [1])),
        )
    )

    # p x -> p (f x): a case where the hypothesis is false says nothing, so it
    # is skipped rather than counted as a pass of the interesting claim.
    assert "if (!operations.isSorted(value)) continue;" in suite
    assert "expect(operations.isSorted(operations.insert(value))).toBe(true)" in suite


def test_establishment_judges_the_output_unconditionally():
    subject = _operation("op.render", "render", input_type=SMALL_INT, output_type=WORD)
    predicate = _operation(
        "op.lower", "isLower", input_type=WORD, output_type=BOOLEAN
    )
    obligation = ProofObligation(
        id="obl.render.establishes",
        relation=ObligationRelation.ESTABLISHES,
        subject_operation_id="op.render",
        predicate_operation_id="op.lower",
        requirement_ids=("req.core",),
        sample_size=8,
    )

    suite = compile_obligation_suite(
        _contract(
            (subject, predicate),
            (obligation, _example("op.render", "render", 1, "b")),
        )
    )

    assert "expect(operations.isLower(operations.render(value))).toBe(true)" in suite
    assert "continue;" not in suite


def test_totality_asserts_no_throw_and_honours_its_guard():
    subject = _operation(
        "op.divide",
        "divide",
        input_type=SMALL_INT,
        output_type=SMALL_INT,
        errors=(ErrorContract(id="err.zero", code="ZERO", description="Divide by zero."),),
    )
    guard = _operation(
        "op.nonzero", "isNonZero", input_type=SMALL_INT, output_type=BOOLEAN
    )
    anchor = _example("op.divide", "divide", 4, 2)
    unguarded = ProofObligation(
        id="obl.divide.total",
        relation=ObligationRelation.TOTAL,
        subject_operation_id="op.divide",
        requirement_ids=("req.core",),
        sample_size=8,
    )
    guarded = ProofObligation(
        id="obl.divide.total",
        relation=ObligationRelation.TOTAL,
        subject_operation_id="op.divide",
        guard_operation_id="op.nonzero",
        requirement_ids=("req.core",),
        sample_size=8,
    )

    plain = compile_obligation_suite(_contract((subject, guard), (unguarded, anchor)))
    assert "expect(() => operations.divide(value)).not.toThrow()" in plain
    assert "isNonZero" not in plain

    fenced = compile_obligation_suite(_contract((subject, guard), (guarded, anchor)))
    assert "if (!operations.isNonZero(value)) continue;" in fenced


# --------------------------------------------------------------------------
# Refusals: a property gate that passes without checking is worse than none
# --------------------------------------------------------------------------


def test_a_proof_tier_obligation_is_refused_rather_than_downgraded():
    operation = _operation(
        "op.normalize", "normalize", input_type=INT_LIST, output_type=INT_LIST
    )
    proved = ProofObligation(
        id="obl.normalize.idempotent",
        relation=ObligationRelation.IDEMPOTENT,
        subject_operation_id="op.normalize",
        requirement_ids=("req.core",),
        tier=ObligationTier.PROOF,
    )

    with pytest.raises(ObligationCompileError, match="whole domain"):
        compile_obligation_suite(
            _contract(
                (operation,),
                (proved, _example("op.normalize", "normalize", [1], [1])),
            )
        )


def test_a_contract_with_no_obligations_is_refused():
    operation = _operation(
        "op.clamp", "clamp", input_type=SMALL_INT, output_type=SMALL_INT
    )

    with pytest.raises(ObligationCompileError, match="without checking anything"):
        compile_obligation_suite(_contract((operation,), ()))


def test_an_untyped_operation_cannot_back_a_sampled_claim():
    # The refusal lands one layer earlier than the compiler: a contract cannot
    # even be constructed with a sampled claim over an undeclared domain. The
    # compiler keeps its own check as defence in depth, but this is the test
    # that matters, because it means no such contract reaches a target pack.
    operation = OperationContract(
        id="op.legacy",
        name="legacy",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        requirement_ids=("req.core",),
    )
    obligation = ProofObligation(
        id="obl.legacy.idempotent",
        relation=ObligationRelation.IDEMPOTENT,
        subject_operation_id="op.legacy",
        requirement_ids=("req.core",),
        sample_size=4,
    )

    with pytest.raises(ModelValidationError, match="no generator can draw from"):
        _contract(
            (operation,),
            (obligation, _example("op.legacy", "legacy", {"a": 1}, {"b": 2})),
        )


def test_the_pinned_surface_rejects_two_operations_that_disagree_about_a_name():
    left = _contract(
        (_operation("op.a", "shared", input_type=SMALL_INT, output_type=SMALL_INT),),
        (_example("op.a", "shared", 1, 1),),
    )
    right = ContractV2(
        id="contract.other",
        node_id="node.other",
        operations=(
            _operation("op.b", "shared", input_type=WORD, output_type=WORD),
        ),
        obligations=(
            ProofObligation(
                id="obl.b.example",
                relation=ObligationRelation.EXAMPLE,
                subject_operation_id="op.b",
                requirement_ids=("req.core",),
                example=ObligationExample(argument="a", result="a"),
            ),
        ),
    )

    with pytest.raises(ObligationCompileError, match="disagree about"):
        compile_operations_interface((left, right))


def test_the_pinned_surface_renders_every_type_kind():
    operations = (
        _operation("op.r", "shape", input_type=RECORD, output_type=CHOICE),
        _operation("op.l", "listing", input_type=INT_LIST, output_type=BOOLEAN),
    )

    rendered = compile_operations_interface(
        (
            _contract(
                operations,
                (
                    _example("op.r", "shape", {"flag": True, "count": 0, "note": None}, "red"),
                    _example("op.l", "listing", [1], True),
                ),
            ),
        )
    )

    assert (
        "shape(input: { flag: boolean, count: number, note: string | null }): "
        '"red" | "green" | "blue";' in rendered
    )
    assert "listing(input: Array<number>): boolean;" in rendered
    assert OPERATIONS_MODULE in rendered


# --------------------------------------------------------------------------
# The generator is the one file that decides what a type means at runtime.
# Run it, and check its answers against the Python definition of the same type.
# --------------------------------------------------------------------------


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("live test; the pinned Node toolchain is not on PATH")
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if not version.startswith("v22."):
        pytest.skip(f"live test; needs Node 22.x, found {version}")
    return node


def _draw(node, tmp_path, cases):
    """Run the real generator and return what TypeScript actually produced."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "rich-value-generator.ts").write_text(VALUE_GENERATOR_SOURCE)
    driver = tmp_path / "driver.ts"
    # Raw Node ESM needs the extension; vitest resolves the suite's
    # extensionless import itself, which is why only the driver differs.
    driver.write_text(
        "import { casesFor, equals, type ValueType } "
        'from "./rich-value-generator.ts";\n'
        f"const requested = {json.dumps(cases)};\n"
        "const out = requested.map((item: any) => ({\n"
        "  id: item.id,\n"
        "  values: casesFor(item.id, item.type as ValueType, item.count),\n"
        "}));\n"
        "const equalityChecks = [\n"
        "  equals({ a: [1, 2] }, { a: [1, 2] }),\n"
        "  equals({ a: [1, 2] }, { a: [2, 1] }),\n"
        "  equals(null, null),\n"
        "  equals(0, false),\n"
        '  equals({ a: 1, b: 2 }, { b: 2, a: 1 }),\n'
        "  equals([1], [1, 1]),\n"
        "];\n"
        "console.log(JSON.stringify({ out, equalityChecks }));\n"
    )
    result = subprocess.run(
        [node, "--experimental-strip-types", str(driver)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.live
@pytest.mark.parametrize(
    ("label", "value_type"),
    [
        ("boolean", BOOLEAN),
        ("integer", SMALL_INT),
        ("string", WORD),
        ("enum", CHOICE),
        ("list", INT_LIST),
        ("optional", ValueType(kind=ValueTypeKind.OPTIONAL, element=WORD)),
        ("record", RECORD),
        (
            "unicode",
            ValueType(
                kind=ValueTypeKind.STRING,
                max_length=5,
                char_set=CharSet.UNICODE_SAMPLE,
            ),
        ),
        (
            "nested",
            ValueType(
                kind=ValueTypeKind.LIST,
                element=ValueType(
                    kind=ValueTypeKind.RECORD,
                    record_fields=(RecordField("inner", INT_LIST),),
                ),
                max_length=3,
            ),
        ),
    ],
)
def test_generated_values_inhabit_the_type_python_validated(
    label, value_type, tmp_path
):
    node = _node()

    drawn = _draw(
        node,
        tmp_path,
        [{"id": f"obl.{label}", "type": value_type.to_dict(), "count": 200}],
    )

    values = drawn["out"][0]["values"]
    assert len(values) == 200
    # Two independent implementations of "what inhabits this type": the
    # TypeScript sampler that produced these and the Python checker that
    # validates contract examples. If they ever drift, a property gate is
    # testing a different domain than the contract declares.
    for value in values:
        assert value_type.accepts(value), (label, value)
    if label == "unicode":
        # Pin the bug this cross-check found: JavaScript indexes strings by
        # UTF-16 code unit, so sampling an astral character emitted a lone
        # surrogate. Deleting the emoji from the alphabet would make the check
        # above pass again while removing the very hazard it exercises.
        assert any(
            any(ord(character) > 0xFFFF for character in value)
            for value in values
        )


@pytest.mark.live
def test_the_same_obligation_id_always_draws_the_same_cases(tmp_path):
    node = _node()
    request = [
        {"id": "obl.stable", "type": RECORD.to_dict(), "count": 40},
        {"id": "obl.other", "type": RECORD.to_dict(), "count": 40},
    ]

    first = _draw(node, tmp_path, request)
    second = _draw(node, tmp_path / "again", request)

    assert first["out"] == second["out"]
    # A failure has to be reproducible from the id alone, which is only
    # meaningful if a different id explores somewhere else.
    assert first["out"][0]["values"] != first["out"][1]["values"]


@pytest.mark.live
def test_structural_equality_matches_the_python_notion_of_the_same_value(tmp_path):
    node = _node()

    drawn = _draw(node, tmp_path, [{"id": "obl.eq", "type": BOOLEAN.to_dict(), "count": 1}])

    # Key order must not matter, element order must, and 0 is not false.
    assert drawn["equalityChecks"] == [True, False, True, False, True, False]


def test_the_generator_source_is_shipped_where_the_suite_imports_it():
    assert GENERATOR_PATH.endswith("rich-value-generator.ts")
    suite_import = './rich-value-generator"'
    operation = _operation(
        "op.clamp", "clamp", input_type=SMALL_INT, output_type=SMALL_INT
    )

    suite = compile_obligation_suite(
        _contract((operation,), (_example("op.clamp", "clamp", 1, 1),))
    )

    assert suite_import in suite
    assert "do not edit" in suite
    assert "do not edit" in VALUE_GENERATOR_SOURCE
