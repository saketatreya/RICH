import json
import shutil
import subprocess

import pytest

from richbuild.models import (
    CharSet,
    Contract,
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
from richbuild.target_packs.typescript_obligations import (
    GENERATOR_PATH,
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
    return Contract(
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

    assert "await operations.clamp(4)" in suite
    assert "async () => {" in suite
    assert "expect(actual).toEqual(2)" in suite, (
        "a failing check has to say what it got, not just that it was false"
    )
    # A single ground fact needs no case list -- and an import of the case
    # drawer it never calls would fail the typecheck gate this file runs beside.
    assert "casesFor" not in suite
    assert 'from "./rich-value-generator"' not in suite, (
        "an example suite draws no cases and compares no structures, so it "
        "imports nothing from the generator -- not even an empty import"
    )


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

    assert "const once = await operations.normalize(value)" in suite
    assert "expect(await operations.normalize(once)).toEqual(once)" in suite
    assert (
        'import { casesFor, type ValueType } from "./rich-value-generator";'
        in suite
    ), "cases are still drawn; equality is now vitest's, so it prints a diff"
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
            (
                obligation,
                _example("op.encode", "encode", 1, "b"),
                _example("op.decode", "decode", "b", 1),
            ),
        )
    )

    assert "await operations.decode(await operations.encode(value))" in suite
    assert "expect(restored).toEqual(value)" in suite


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
            (
                obligation,
                _example("op.insert", "insert", [1], [1]),
                _example("op.sorted", "isSorted", [1], True),
            ),
        )
    )

    # p x -> p (f x): a case where the hypothesis is false says nothing, so it
    # is skipped rather than counted as a pass of the interesting claim.
    assert "if (!(await operations.isSorted(value))) continue;" in suite
    assert (
        "expect(await operations.isSorted(await operations.insert(value))).toBe(true)"
        in suite
    )


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
            (
                obligation,
                _example("op.render", "render", 1, "b"),
                _example("op.lower", "isLower", "b", True),
            ),
        )
    )

    assert (
        "expect(await operations.isLower(await operations.render(value))).toBe(true)"
        in suite
    )
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

    guard_anchor = _example("op.nonzero", "isNonZero", 1, True)

    plain = compile_obligation_suite(_contract((subject, guard), (unguarded, anchor)))
    # One value that reads the same whether the operation answered or
    # rejected: what it threw, or null.
    assert "expect(await thrown(() => operations.divide(value))).toBeNull()" in plain
    assert "import { casesFor, thrown, type ValueType }" in plain
    assert "isNonZero" not in plain

    fenced = compile_obligation_suite(
        _contract((subject, guard), (guarded, anchor, guard_anchor))
    )
    assert "if (!(await operations.isNonZero(value))) continue;" in fenced


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
    right = Contract(
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

    # Async-tolerant uniformly: a component that reaches a database answers
    # with promises, one that does not need not, and the gate awaits both.
    assert (
        "shape(input: { flag: boolean, count: number, note: string | null }): "
        '"red" | "green" | "blue" | Promise<"red" | "green" | "blue">;' in rendered
    )
    assert "listing(input: Array<number>): boolean | Promise<boolean>;" in rendered
    assert "Operations" in rendered and "implement" in rendered.lower()
    assert "export type Decimal" not in rendered, "no decimal, no brand"


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
        "import { casesFor, equals, normalize, thrown, type ValueType } "
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
        'const money = { kind: "decimal", precision: 5, scale: 2 } as ValueType;\n'
        "const normalizeChecks = [\n"
        '  normalize(money, "01.50"),\n'
        '  normalize(money, "-0.0"),\n'
        '  normalize(money, "-12.30"),\n'
        '  normalize(money, "100"),\n'
        '  normalize({ kind: "list", element: money } as ValueType, ["1.50", "2"]),\n'
        '  normalize({ kind: "record", record_fields: [{ name: "amount", value_type: money }] } as ValueType,\n'
        '    { amount: "2.0", extra: "2.0" }),\n'
        '  normalize({ kind: "string" } as ValueType, "01.50"),\n'
        "];\n"
        "const thrownChecks = [\n"
        "  (await thrown(() => 1)) === null,\n"
        '  (await thrown(() => { throw new Error("boom"); })) instanceof Error,\n'
        '  (await thrown(async () => { throw new Error("later"); })) instanceof Error,\n'
        "  (await thrown(async () => 1)) === null,\n"
        "];\n"
        "console.log(JSON.stringify({ out, equalityChecks, normalizeChecks, thrownChecks }));\n"
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
            "slug",
            ValueType(
                kind=ValueTypeKind.STRING,
                min_length=1,
                max_length=12,
                char_set=CharSet.ASCII_SLUG,
            ),
        ),
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
        ("identifier", ValueType(kind=ValueTypeKind.IDENTIFIER, entity="Todo")),
        ("timestamp", ValueType(kind=ValueTypeKind.TIMESTAMP)),
        ("date", ValueType(kind=ValueTypeKind.DATE)),
        ("decimal", ValueType(kind=ValueTypeKind.DECIMAL, precision=7, scale=3)),
        ("money", ValueType(kind=ValueTypeKind.DECIMAL, precision=2, scale=2)),
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
    if label == "decimal":
        # Both signs, both spellings, never a float: the sampler must reach the
        # corners the checker admits, or the gate tests a narrower domain than
        # the contract declares.
        assert any(value.startswith("-") for value in values)
        assert any("." in value for value in values)
        assert any("." not in value for value in values)
        assert all(isinstance(value, str) for value in values)


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

    drawn = ProofObligation(
        id="obl.clamp.idempotent",
        relation=ObligationRelation.IDEMPOTENT,
        subject_operation_id="op.clamp",
        requirement_ids=("req.core",),
        sample_size=4,
    )
    suite = compile_obligation_suite(
        _contract((operation,), (_example("op.clamp", "clamp", 1, 1), drawn))
    )

    assert suite_import in suite
    assert "do not edit" in suite
    assert "do not edit" in VALUE_GENERATOR_SOURCE


# --------------------------------------------------------------------------
# The whole point: a compiled suite must fail a wrong implementation.
# Run it for real under the pinned Node, with a vitest shim rather than an
# install, so the generated file executes exactly as written.
# --------------------------------------------------------------------------


LIST_OF_INT = ValueType(
    kind=ValueTypeKind.LIST,
    element=ValueType(kind=ValueTypeKind.INTEGER, minimum=-20, maximum=20),
    max_length=6,
)
ENCODED = ValueType(
    kind=ValueTypeKind.STRING, max_length=64, char_set=CharSet.ASCII_PRINTABLE
)


def _sorting_contract():
    operations = (
        _operation("op.sort", "sortList", input_type=LIST_OF_INT, output_type=LIST_OF_INT),
        _operation("op.sorted", "isSorted", input_type=LIST_OF_INT, output_type=BOOLEAN),
        _operation("op.encode", "encodeList", input_type=LIST_OF_INT, output_type=ENCODED),
        _operation("op.decode", "decodeList", input_type=ENCODED, output_type=LIST_OF_INT),
    )
    obligations = (
        _example("op.sort", "sortList", [3, 1, 2], [1, 2, 3]),
        # A negative anchor, deliberately: an always-true predicate is what
        # makes ESTABLISHES vacuous, and only a case it must reject rules it out.
        _example("op.sorted", "isSorted", [2, 1], False),
        _example("op.encode", "encodeList", [1, 2], "1,2"),
        _example("op.decode", "decodeList", "1,2", [1, 2]),
        ProofObligation(
            id="obl.sort.idempotent",
            relation=ObligationRelation.IDEMPOTENT,
            subject_operation_id="op.sort",
            requirement_ids=("req.core",),
            sample_size=200,
        ),
        ProofObligation(
            id="obl.sort.establishes",
            relation=ObligationRelation.ESTABLISHES,
            subject_operation_id="op.sort",
            predicate_operation_id="op.sorted",
            requirement_ids=("req.core",),
            sample_size=200,
        ),
        ProofObligation(
            id="obl.codec.round_trip",
            relation=ObligationRelation.ROUND_TRIP,
            subject_operation_id="op.encode",
            witness_operation_id="op.decode",
            requirement_ids=("req.core",),
            sample_size=200,
        ),
    )
    return _contract(operations, obligations)


CORRECT_OPERATIONS = """\
export const operations = {
  // Async on purpose, beside synchronous siblings: the interface allows
  // either, and the gate has to await this one and not choke on the others.
  async sortList(input: number[]): Promise<number[]> {
    return [...input].sort((a, b) => a - b);
  },
  isSorted(input: number[]): boolean {
    return input.every((value, index) => index === 0 || input[index - 1] <= value);
  },
  encodeList(input: number[]): string {
    return input.join(",");
  },
  decodeList(input: string): number[] {
    return input === "" ? [] : input.split(",").map((part) => Number(part));
  },
};
"""

# Each of these is wrong in exactly one way, to show which claim catches it.
WRONG_OPERATIONS = {
    # Identity satisfies IDEMPOTENT perfectly. Nothing but the ground example
    # and the predicate claim can tell it apart from a real sort.
    "sort_is_identity": CORRECT_OPERATIONS.replace(
        "return [...input].sort((a, b) => a - b);", "return [...input];"
    ),
    # An always-true predicate would make ESTABLISHES pass for any subject.
    "predicate_always_true": CORRECT_OPERATIONS.replace(
        "return input.every((value, index) => index === 0 || input[index - 1] <= value);",
        "return true;",
    ),
    # Loses information, so the witness cannot recover the input.
    "encode_drops_data": CORRECT_OPERATIONS.replace(
        'return input.join(",");', 'return input.slice(0, 1).join(",");'
    ),
    # Sorts, but not stably against duplicates in a way the example pins.
    "sort_reverses": CORRECT_OPERATIONS.replace(
        "return [...input].sort((a, b) => a - b);",
        "return [...input].sort((a, b) => b - a);",
    ),
}


_VITEST_SHIM = """\
const failures = [];
const passes = [];
let suite = null;

export function describe(name, body) {
  suite = name;
  body();
  void runQueued();
}

// Bodies are async now -- every call in a compiled suite is awaited -- so
// they are queued and run one after another once the suite has registered.
const queued = [];

export function it(name, body) {
  queued.push(async () => {
    try {
      await body();
      passes.push(name);
    } catch (error) {
      failures.push({ name, message: String(error && error.message ? error.message : error) });
    }
  });
}

async function runQueued() {
  for (const run of queued) await run();
}

function fail(message) {
  throw new Error(message);
}

function deepEqual(left, right) {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length
      && left.every((item, index) => deepEqual(item, right[index]));
  }
  if (typeof left === "object" && typeof right === "object" && left && right) {
    const lk = Object.keys(left).sort();
    const rk = Object.keys(right).sort();
    return lk.length === rk.length
      && lk.every((key, index) => key === rk[index])
      && lk.every((key) => deepEqual(left[key], right[key]));
  }
  return false;
}

export function expect(actual) {
  const matchers = {
    // Structural, and it reports both sides -- which is the entire reason the
    // generated assertions moved off a collapsed boolean.
    toEqual(expected) {
      if (!deepEqual(actual, expected)) {
        fail(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
      }
    },
    toBe(expected) {
      if (!Object.is(actual, expected)) {
        fail(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
      }
    },
    toBeNull() {
      if (actual !== null) fail(`expected null, got ${String(actual)}`);
    },
    toThrow() {
      let threw = false;
      try { actual(); } catch { threw = true; }
      if (!threw) fail("expected a throw");
    },
    not: {
      toBe(expected) {
        if (Object.is(actual, expected)) fail("expected a difference");
      },
      toEqual(expected) {
        if (deepEqual(actual, expected)) fail("expected a difference");
      },
      toThrow() {
        try { actual(); } catch (error) { fail(`threw: ${error}`); }
      },
    },
  };
  return matchers;
}

process.on("exit", () => {
  process.stdout.write(JSON.stringify({ suite, passes, failures }) + "\\n");
});
"""

_TS_RESOLVER = """\
export async function resolve(specifier, context, next) {
  if (specifier.startsWith(".") && !/\\.[a-z]+$/.test(specifier)) {
    try {
      return await next(specifier + ".ts", context);
    } catch {
      // fall through to the default resolution below
    }
  }
  return next(specifier, context);
}
"""

_REGISTER = """\
import { register } from "node:module";
import { pathToFileURL } from "node:url";

register("./ts-resolve.mjs", pathToFileURL("./"));
"""


def _run_suite(node, root, contract, operations_source):
    """Execute a compiled suite against one implementation, unmodified."""

    (root / "tests" / "properties").mkdir(parents=True)
    (root / "packages" / "domain" / "src").mkdir(parents=True)
    (root / "node_modules" / "vitest").mkdir(parents=True)

    (root / "tests/properties/rich-value-generator.ts").write_text(
        VALUE_GENERATOR_SOURCE
    )
    (root / "tests/properties/obligations.test.ts").write_text(
        compile_obligation_suite(contract)
    )
    (root / "packages/domain/src/operations.ts").write_text(operations_source)
    # A shim rather than an install: the suite has to run exactly as generated,
    # and pulling 2 GiB of packages to learn whether an assertion fires would
    # tell us nothing extra.
    (root / "node_modules/vitest/package.json").write_text(
        json.dumps({"name": "vitest", "version": "0.0.0", "type": "module", "main": "index.js"})
    )
    (root / "node_modules/vitest/index.js").write_text(_VITEST_SHIM)
    # Node ESM will not resolve an extensionless relative import; vitest does.
    # A resolve hook supplies that instead of editing the generated import.
    (root / "ts-resolve.mjs").write_text(_TS_RESOLVER)
    (root / "register.mjs").write_text(_REGISTER)

    result = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "--import",
            "./register.mjs",
            "tests/properties/obligations.test.ts",
        ],
        capture_output=True,
        text=True,
        cwd=root,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _failed_obligations(report):
    return {failure["name"].split(":")[0] for failure in report["failures"]}


@pytest.mark.live
def test_a_correct_implementation_satisfies_every_compiled_obligation(tmp_path):
    node = _node()

    report = _run_suite(node, tmp_path, _sorting_contract(), CORRECT_OPERATIONS)

    assert report["failures"] == [], report["failures"]
    assert len(report["passes"]) == 7


@pytest.mark.live
@pytest.mark.parametrize(
    ("defect", "expected"),
    [
        # Identity is idempotent, so IDEMPOTENT does not fire. The ground
        # example and the predicate claim are what catch it -- which is the
        # anti-vacuity rule paying for itself in running code.
        (
            "sort_is_identity",
            {"obl.sortList.example", "obl.sort.establishes"},
        ),
        ("predicate_always_true", {"obl.isSorted.example"}),
        (
            "encode_drops_data",
            {"obl.encodeList.example", "obl.codec.round_trip"},
        ),
        ("sort_reverses", {"obl.sortList.example", "obl.sort.establishes"}),
    ],
)
def test_each_defect_is_caught_by_the_obligation_that_should_catch_it(
    defect, expected, tmp_path
):
    node = _node()

    report = _run_suite(
        node, tmp_path, _sorting_contract(), WRONG_OPERATIONS[defect]
    )

    assert _failed_obligations(report) == expected, report["failures"]


@pytest.mark.live
def test_idempotence_alone_cannot_tell_identity_from_a_real_sort(tmp_path):
    """The empirical case for requiring a ground example alongside a property."""

    node = _node()
    contract = _contract(
        (
            _operation(
                "op.sort", "sortList", input_type=LIST_OF_INT, output_type=LIST_OF_INT
            ),
        ),
        (
            _example("op.sort", "sortList", [3, 1, 2], [1, 2, 3]),
            ProofObligation(
                id="obl.sort.idempotent",
                relation=ObligationRelation.IDEMPOTENT,
                subject_operation_id="op.sort",
                requirement_ids=("req.core",),
                sample_size=200,
            ),
        ),
    )

    report = _run_suite(
        node, tmp_path, contract, WRONG_OPERATIONS["sort_is_identity"]
    )

    # 200 random cases, and the property is perfectly satisfied by a function
    # that does nothing. Only the single ground fact fails.
    assert "obl.sort.idempotent" in {
        name.split(":")[0] for name in report["passes"]
    }
    assert _failed_obligations(report) == {"obl.sortList.example"}


# --------------------------------------------------------------------------
# The persistence kinds: rendered, sampled, and compared after normalisation
# --------------------------------------------------------------------------


MONEY = ValueType(kind=ValueTypeKind.DECIMAL, precision=12, scale=2)
LINE = ValueType(
    kind=ValueTypeKind.RECORD,
    record_fields=(
        RecordField("id", ValueType(kind=ValueTypeKind.IDENTIFIER, entity="Order")),
        RecordField("at", ValueType(kind=ValueTypeKind.TIMESTAMP)),
        RecordField("on", ValueType(kind=ValueTypeKind.DATE)),
        RecordField("amount", MONEY),
    ),
)


def test_the_pinned_surface_renders_the_persistence_kinds_and_brands_the_decimal():
    operations = (
        _operation("op.total", "total", input_type=LINE, output_type=MONEY),
        _operation("op.name", "nameOf", input_type=LINE, output_type=WORD),
    )

    rendered = compile_operations_interface(
        (
            _contract(
                operations,
                (
                    _example(
                        "op.total",
                        "total",
                        {"id": "o-1", "at": "2026-08-29T12:00:00Z", "on": "2026-08-29", "amount": "1.50"},
                        "1.50",
                    ),
                ),
            ),
        )
    )

    # An identifier, an instant and a date are strings with a grammar the gate
    # checks; only the decimal is branded, because a plain string there would
    # invite a float on the way in.
    assert (
        "total(input: { id: string, at: string, on: string, amount: Decimal }): "
        "Decimal | Promise<Decimal>;" in rendered
    )
    assert "export type Decimal = string & { readonly __rich_decimal: never };" in rendered
    assert "never a float" in rendered
    assert "synchronously or with a promise" in rendered


def test_a_type_that_carries_a_decimal_is_compared_after_normalisation():
    subject = _operation("op.round", "roundUp", input_type=MONEY, output_type=MONEY)
    codec = (
        _operation("op.enc", "encodeLine", input_type=LINE, output_type=WORD),
        _operation("op.dec", "decodeLine", input_type=WORD, output_type=LINE),
    )
    obligations = (
        _example("op.round", "roundUp", "1.50", "2"),
        ProofObligation(
            id="obl.round.idempotent",
            relation=ObligationRelation.IDEMPOTENT,
            subject_operation_id="op.round",
            requirement_ids=("req.core",),
            sample_size=8,
        ),
        _example("op.enc", "encodeLine", {"id": "o", "at": "2026-08-29T12:00:00Z", "on": "2026-08-29", "amount": "1"}, "o"),
        _example("op.dec", "decodeLine", "o", {"id": "o", "at": "2026-08-29T12:00:00Z", "on": "2026-08-29", "amount": "1"}),
        ProofObligation(
            id="obl.line.round_trip",
            relation=ObligationRelation.ROUND_TRIP,
            subject_operation_id="op.enc",
            witness_operation_id="op.dec",
            requirement_ids=("req.core",),
            sample_size=8,
        ),
    )

    suite = compile_obligation_suite(_contract((subject, *codec), obligations))

    # "1.5" and "1.50" are one value, so an implementation is right to answer
    # either; the assertion binds the type it compares under and normalises
    # both sides. The round trip compares the *input* type, which is the one
    # the witness must give back.
    assert (
        'const shape = {"kind": "decimal", "precision": 12, "scale": 2} as ValueType;'
        in suite
    )
    assert 'expect(normalize(shape, actual)).toEqual(normalize(shape, "2"))' in suite
    assert (
        "expect(normalize(shape, await operations.roundUp(once)))"
        ".toEqual(normalize(shape, once))" in suite
    )
    assert "expect(normalize(shape, restored)).toEqual(normalize(shape, value))" in suite
    assert '"record_fields": [{"name": "id"' in suite
    assert "import { casesFor, normalize, type ValueType }" in suite
    # And a decimal-free suite is byte-for-byte free of it.
    plain = compile_obligation_suite(
        _contract(
            (_operation("op.clamp", "clamp", input_type=SMALL_INT, output_type=SMALL_INT),),
            (_example("op.clamp", "clamp", 1, 1),),
        )
    )
    assert "normalize" not in plain and "shape" not in plain


def test_an_operation_named_like_a_generator_helper_does_not_import_it():
    # `operations.normalize(` and `operations.equals(` are the model's; only
    # a bare call pulls the generator's function in.
    operation = _operation("op.n", "normalize", input_type=SMALL_INT, output_type=SMALL_INT)
    suite = compile_obligation_suite(
        _contract((operation,), (_example("op.n", "normalize", 1, 1),))
    )
    assert 'from "./rich-value-generator"' not in suite, suite
    assert "await operations.normalize(1)" in suite


@pytest.mark.live
def test_normalisation_and_totality_helpers_answer_as_the_suite_assumes(tmp_path):
    node = _node()

    drawn = _draw(node, tmp_path, [{"id": "obl.eq", "type": BOOLEAN.to_dict(), "count": 1}])

    # Leading and trailing zeros go, "-0" is "0", a list and a declared record
    # field are normalised inside, an undeclared field and a plain string are
    # left exactly as they were.
    assert drawn["normalizeChecks"] == [
        "1.5",
        "0",
        "-12.3",
        "100",
        ["1.5", "2"],
        {"amount": "2", "extra": "2.0"},
        "01.50",
    ]
    # Sync or async, answered or rejected: the same one value.
    assert drawn["thrownChecks"] == [True, True, True, True]
