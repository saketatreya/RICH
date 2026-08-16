import copy
import json

import pytest

from rich_v2.architect import (
    LAYER_NODE_IDS,
    ROOT_NODE_ID,
    ArchitectProposalError,
    architect_prompt,
    architect_response_schema,
    assemble,
    layers_of,
    requirement_allocation,
)
from rich_v2.compiler import compile_architecture
from rich_v2.interview import AdaptiveInterview, InterviewState
from rich_v2.models import ValueType, value_type_from_request, value_type_request_schema
from rich_v2.target_packs.typescript_obligations import compile_obligation_suite


TARGET_PACK = "nextjs-app-router"


def _project():
    return AdaptiveInterview(
        InterviewState(
            "project.tasks",
            "Tasks",
            answers={
                "goal": "A persistent team todo application",
                "audiences": ["small product teams"],
                "roles": ["Members manage tasks in their own team."],
                "data_policy": ["Tasks remain until explicitly deleted."],
                "capabilities": [
                    {
                        "id": "req.todo",
                        "title": "Manage tasks",
                        "statement": "A member can add a task and it remains after refresh.",
                    }
                ],
                "quality_constraints": [
                    {
                        "id": "req.a11y",
                        "title": "Accessible UI",
                        "statement": "Task actions satisfy keyboard accessibility.",
                    }
                ],
                "scenarios": [
                    {
                        "id": "scenario.todo",
                        "title": "Add task",
                        "when": ["A member adds Buy milk."],
                        "then": ["Buy milk remains after refresh."],
                        "requirement_ids": ["req.todo"],
                        "oracle": [
                            {"action": "navigate", "value": "/"},
                            {
                                "action": "assert_visible",
                                "locator": {"kind": "text", "value": "Buy milk"},
                            },
                        ],
                    },
                    {
                        "id": "scenario.a11y",
                        "title": "Keyboard access",
                        "when": ["A member uses only a keyboard."],
                        "then": ["They can add a task."],
                        "requirement_ids": ["req.a11y"],
                        "oracle": [
                            {"action": "navigate", "value": "/"},
                            {
                                "action": "assert_visible",
                                "locator": {"kind": "role", "value": "textbox"},
                            },
                        ],
                    },
                ],
            },
        )
    ).compile()


def _type(**overrides):
    """A value type in the shape the request schema demands: every slot present."""

    document = {
        "kind": None,
        "minimum": None,
        "maximum": None,
        "min_length": None,
        "max_length": None,
        "char_set": None,
        "members": [],
        "element": None,
        "record_fields": [],
    }
    document.update(overrides)
    return document


BOOLEAN = _type(kind="boolean")
TITLE = _type(kind="string", min_length=1, max_length=80, char_set="ascii_printable")
TASK = _type(
    kind="record",
    record_fields=[
        {"name": "title", "value_type": TITLE},
        {"name": "done", "value_type": BOOLEAN},
    ],
)
TASK_LIST = _type(kind="list", element=TASK, max_length=20)
MARKUP = _type(kind="string", max_length=4000, char_set="ascii_printable")


def _obligation(relation, operation, **overrides):
    document = {
        "relation": relation,
        "operation": operation,
        "witness": None,
        "predicate": None,
        "guard": None,
        "argument": None,
        "result": None,
        "sample_size": None,
    }
    document.update(overrides)
    return document


def _operation(name, input_type, output_type, *, errors=()):
    return {
        "name": name,
        "description": f"{name} operation.",
        "input_type": input_type,
        "output_type": output_type,
        "errors": list(errors),
    }


def _proposal():
    return {
        "rationale": "Task rules belong in the domain; the UI renders them.",
        "components": [
            {
                "layer": "domain",
                "purpose": "Task rules",
                "requirement_ids": ["req.todo"],
                "operations": [
                    _operation("normalizeTasks", TASK_LIST, TASK_LIST),
                    _operation("isNormalized", TASK_LIST, BOOLEAN),
                ],
                "obligations": [
                    _obligation(
                        "example",
                        "normalizeTasks",
                        argument=[{"title": "b", "done": False}],
                        result=[{"title": "b", "done": False}],
                    ),
                    _obligation(
                        "example",
                        "isNormalized",
                        argument=[
                            {"title": "b", "done": False},
                            {"title": "a", "done": False},
                        ],
                        result=False,
                    ),
                    _obligation("idempotent", "normalizeTasks", sample_size=64),
                    _obligation(
                        "establishes",
                        "normalizeTasks",
                        predicate="isNormalized",
                        sample_size=64,
                    ),
                ],
            },
            {
                "layer": "ui",
                "purpose": "Accessible task UI",
                "requirement_ids": ["req.todo", "req.a11y"],
                "operations": [_operation("renderTasks", TASK_LIST, MARKUP)],
                "obligations": [
                    _obligation("example", "renderTasks", argument=[], result="")
                ],
            },
        ],
    }


def _without(document, layer, key, value):
    """Return a copy of the proposal with one field of one layer replaced."""

    mutated = copy.deepcopy(document)
    for component in mutated["components"]:
        if component["layer"] == layer:
            component[key] = value
    return mutated


# --------------------------------------------------------------------------
# The model chooses semantics; everything structural is derived
# --------------------------------------------------------------------------


def test_a_proposal_becomes_a_validated_architecture_the_compiler_accepts():
    project = _project()

    architecture = assemble(project, _proposal(), target_pack=TARGET_PACK)

    architecture.validate_against_project(project)
    assert layers_of(architecture) == ("domain", "ui")
    assert {node.id for node in architecture.nodes} == {"app", "domain", "web"}
    plan = compile_architecture(architecture, project)
    assert plan.task_index["web"].dependency_ids == ("domain",)
    assert plan.task_index["domain"].dependency_ids == ()


def test_requirements_are_allocated_rather_than_copied_everywhere():
    architecture = assemble(_project(), _proposal(), target_pack=TARGET_PACK)

    # The whole point of asking a model. The deterministic planner gives every
    # requirement to every layer, which is not a decomposition.
    assert requirement_allocation(architecture) == {
        "req.a11y": ("web",),
        "req.todo": ("domain", "web"),
    }


def test_the_composition_root_stays_one_operation_however_large_the_product():
    architecture = assemble(_project(), _proposal(), target_pack=TARGET_PACK)

    root = architecture.node_index[ROOT_NODE_ID]
    contract = architecture.contract_index[root.contract_id]
    # The root is responsible for the product, so it traces every requirement;
    # it stays one operation so its contract does not grow with the product.
    assert set(root.requirement_ids) == {"req.todo", "req.a11y"}
    assert [operation.name for operation in contract.operations] == [
        "composeApplication"
    ]


def test_identifiers_paths_and_edges_are_derived_not_proposed():
    proposal = _proposal()
    architecture = assemble(_project(), proposal, target_pack=TARGET_PACK)

    serialized = json.dumps(proposal)
    assert "owned_paths" not in serialized
    assert "packages/domain" not in serialized
    assert architecture.node_index["domain"].owned_paths == (
        "packages/contracts",
        "packages/domain",
    )
    assert architecture.node_index["web"].owned_paths == ("apps/web", "packages/ui")
    assert {edge.id for edge in architecture.edges} == {
        "contains:app:domain",
        "contains:app:web",
        "capability:web:domain",
    }


def test_a_boundary_port_carries_the_type_the_provider_accepts():
    architecture = assemble(_project(), _proposal(), target_pack=TARGET_PACK)

    web = architecture.node_index["web"]
    domain = architecture.node_index["domain"]
    outbound = web.port_index["web.out.domain"]
    inbound = domain.port_index["domain.in"]

    # A boundary is described by what the consumer must hand over, not by what
    # it returns to someone else -- and the compiler checks the two agree.
    assert outbound.schema == inbound.schema
    assert outbound.operation_id is None


def test_quality_constraints_reach_the_layer_that_serves_them_as_invariants():
    architecture = assemble(_project(), _proposal(), target_pack=TARGET_PACK)

    web = architecture.contract_index["contract:web"]
    domain = architecture.contract_index["contract:domain"]
    assert [invariant.statement for invariant in web.invariants] == [
        "Task actions satisfy keyboard accessibility."
    ]
    # req.a11y was never allocated to the domain, so it leaves no trace there.
    assert domain.invariants == ()


# --------------------------------------------------------------------------
# The obligations are the payoff: they must survive into runnable checks
# --------------------------------------------------------------------------


def test_proposed_obligations_compile_into_checks_a_wrong_answer_would_fail():
    architecture = assemble(_project(), _proposal(), target_pack=TARGET_PACK)

    suite = compile_obligation_suite(architecture.contract_index["contract:domain"])

    assert "operations.normalizeTasks(once), once" in suite
    assert "operations.isNormalized(operations.normalizeTasks(value))" in suite
    # Sampled over a domain the generator can actually draw from, which is only
    # true because the architect was required to bound every string and list.
    assert ", 64)" in suite
    for operation in architecture.contract_index["contract:domain"].operations:
        assert operation.input_type.is_finitely_sampleable


def test_a_missing_sample_size_is_defaulted_rather_than_rejected():
    proposal = copy.deepcopy(_proposal())
    for component in proposal["components"]:
        for obligation in component["obligations"]:
            obligation["sample_size"] = None

    architecture = assemble(_project(), proposal, target_pack=TARGET_PACK)

    sampled = [
        obligation
        for obligation in architecture.contract_index["contract:domain"].obligations
        if obligation.sample_size is not None
    ]
    # A sample count is a knob, not a judgement, so omitting it should not cost
    # a model attempt.
    assert sampled and all(obligation.sample_size == 64 for obligation in sampled)


# --------------------------------------------------------------------------
# Every way a proposal can be wrong, and the message that comes back
# --------------------------------------------------------------------------


def test_an_unallocated_requirement_is_refused():
    proposal = _without(_proposal(), "ui", "requirement_ids", ["req.todo"])

    with pytest.raises(ArchitectProposalError, match="req.a11y") as caught:
        assemble(_project(), proposal, target_pack=TARGET_PACK)

    assert "unallocated" in str(caught.value)


def test_an_invented_requirement_is_refused():
    proposal = _without(_proposal(), "domain", "requirement_ids", ["req.invented"])

    with pytest.raises(ArchitectProposalError, match="unknown requirements"):
        assemble(_project(), proposal, target_pack=TARGET_PACK)


def test_a_property_with_no_example_to_anchor_it_is_refused():
    proposal = copy.deepcopy(_proposal())
    for component in proposal["components"]:
        if component["layer"] == "domain":
            component["obligations"] = [
                _obligation("idempotent", "normalizeTasks", sample_size=64)
            ]

    with pytest.raises(ArchitectProposalError, match="example obligation"):
        assemble(_project(), proposal, target_pack=TARGET_PACK)


def test_an_unbounded_type_cannot_back_a_sampled_claim():
    unbounded = _type(kind="list", element=TASK)
    proposal = copy.deepcopy(_proposal())
    for component in proposal["components"]:
        if component["layer"] == "domain":
            for operation in component["operations"]:
                operation["input_type"] = unbounded
                if operation["name"] == "normalizeTasks":
                    operation["output_type"] = unbounded

    with pytest.raises(ArchitectProposalError, match="no generator can draw from"):
        assemble(_project(), proposal, target_pack=TARGET_PACK)


def test_an_example_that_does_not_inhabit_its_type_is_refused():
    proposal = copy.deepcopy(_proposal())
    for component in proposal["components"]:
        if component["layer"] == "domain":
            component["obligations"][0]["argument"] = "not a task list"

    with pytest.raises(ArchitectProposalError, match="does not inhabit"):
        assemble(_project(), proposal, target_pack=TARGET_PACK)


def test_a_predicate_that_does_not_return_a_boolean_is_refused():
    proposal = copy.deepcopy(_proposal())
    for component in proposal["components"]:
        if component["layer"] == "domain":
            component["operations"][1]["output_type"] = TITLE
            component["obligations"][1]["result"] = "b"

    with pytest.raises(ArchitectProposalError, match="return a boolean"):
        assemble(_project(), proposal, target_pack=TARGET_PACK)


def test_an_obligation_naming_an_operation_that_does_not_exist_is_refused():
    proposal = copy.deepcopy(_proposal())
    for component in proposal["components"]:
        if component["layer"] == "domain":
            component["obligations"][3]["predicate"] = "isTotallyMadeUp"

    with pytest.raises(ArchitectProposalError, match="isTotallyMadeUp") as caught:
        assemble(_project(), proposal, target_pack=TARGET_PACK)

    # The message lists what does exist, so a repair attempt has what it needs.
    assert "normalizeTasks" in str(caught.value)


def test_totality_over_an_operation_that_declares_no_errors_is_refused():
    proposal = copy.deepcopy(_proposal())
    for component in proposal["components"]:
        if component["layer"] == "domain":
            component["obligations"].append(
                _obligation("total", "normalizeTasks", sample_size=32)
            )

    with pytest.raises(ArchitectProposalError, match="vacuously true"):
        assemble(_project(), proposal, target_pack=TARGET_PACK)


@pytest.mark.parametrize(
    ("mutation", "pattern"),
    [
        ({"components": []}, "no components"),
        ({"components": "not-a-list"}, "components array"),
        ({}, "components array"),
    ],
)
def test_a_structurally_broken_answer_is_refused(mutation, pattern):
    document = {"rationale": "x"}
    document.update(mutation)

    with pytest.raises(ArchitectProposalError, match=pattern):
        assemble(_project(), document, target_pack=TARGET_PACK)


def test_a_missing_required_layer_is_refused():
    proposal = copy.deepcopy(_proposal())
    proposal["components"] = [
        component
        for component in proposal["components"]
        if component["layer"] != "domain"
    ]

    with pytest.raises(ArchitectProposalError, match="omitted required layers"):
        assemble(_project(), proposal, target_pack=TARGET_PACK)


def test_an_unknown_layer_is_refused():
    proposal = copy.deepcopy(_proposal())
    proposal["components"][0]["layer"] = "microservice"

    with pytest.raises(ArchitectProposalError, match="unknown layer"):
        assemble(_project(), proposal, target_pack=TARGET_PACK)


def test_a_duplicated_layer_is_refused():
    proposal = copy.deepcopy(_proposal())
    proposal["components"].append(copy.deepcopy(proposal["components"][0]))

    with pytest.raises(ArchitectProposalError, match="more than once"):
        assemble(_project(), proposal, target_pack=TARGET_PACK)


@pytest.mark.parametrize("name", ["Normalize", "normalize_tasks", "", "1st", "n" * 65])
def test_an_unusable_operation_name_is_refused(name):
    proposal = copy.deepcopy(_proposal())
    proposal["components"][0]["operations"][0]["name"] = name

    with pytest.raises(ArchitectProposalError, match="lowerCamelCase"):
        assemble(_project(), proposal, target_pack=TARGET_PACK)


def test_a_component_with_no_operations_is_refused():
    proposal = _without(_proposal(), "ui", "operations", [])

    with pytest.raises(ArchitectProposalError, match="no operations"):
        assemble(_project(), proposal, target_pack=TARGET_PACK)


def test_duplicate_operation_names_within_a_component_are_refused():
    proposal = copy.deepcopy(_proposal())
    proposal["components"][0]["operations"][1]["name"] = "normalizeTasks"

    with pytest.raises(ArchitectProposalError, match="duplicate operation names"):
        assemble(_project(), proposal, target_pack=TARGET_PACK)


# --------------------------------------------------------------------------
# The request schema is what makes the typed vocabulary askable at all
# --------------------------------------------------------------------------


def test_the_type_request_schema_is_unrolled_rather_than_recursive():
    schema = json.dumps(architect_response_schema())

    # Structured-output decoders reject recursive schemas, so a $ref back to
    # the type would make the whole vocabulary impossible to ask for.
    assert "$ref" not in schema
    assert "$defs" not in schema
    assert "definitions" not in schema


def test_the_innermost_type_level_offers_only_scalars_so_unrolling_terminates():
    innermost = value_type_request_schema(1)

    assert innermost["properties"]["kind"]["enum"] == [
        "boolean",
        "integer",
        "string",
        "enum",
    ]
    assert "element" not in innermost["properties"]
    assert "record_fields" not in innermost["properties"]


def test_only_the_kind_is_required_so_an_answer_stays_small():
    innermost = value_type_request_schema(1)
    outer = value_type_request_schema(3)

    # Requiring every slot at every level was measured to cost 164 bytes for a
    # single boolean instead of 19, and a decomposition carrying a dozen nested
    # types overran its output reservation on that alone.
    assert innermost["required"] == ["kind"]
    assert outer["required"] == ["kind"]
    # Still a closed language: an answer may omit a slot, never invent one.
    assert innermost["additionalProperties"] is False
    assert outer["additionalProperties"] is False


def test_a_minimal_answer_decodes_exactly_like_a_verbose_one():
    verbose = value_type_from_request(TASK_LIST)
    minimal = value_type_from_request(
        {
            "kind": "list",
            "max_length": 20,
            "element": {
                "kind": "record",
                "record_fields": [
                    {
                        "name": "title",
                        "value_type": {
                            "kind": "string",
                            "min_length": 1,
                            "max_length": 80,
                            "char_set": "ascii_printable",
                        },
                    },
                    {"name": "done", "value_type": {"kind": "boolean"}},
                ],
            },
        }
    )

    assert minimal == verbose


def test_a_scalar_answer_carrying_empty_compound_slots_still_decodes():
    # The request schema requires every slot at every level so a decoder never
    # has to choose which keys to emit; a boolean therefore arrives with an
    # empty record_fields, which ValueType rightly refuses.
    value_type = value_type_from_request(BOOLEAN)

    assert value_type == ValueType.from_dict({"kind": "boolean"})


def test_the_prompt_states_the_rules_that_are_checked_mechanically():
    project = _project()

    system_prompt, user_prompt = architect_prompt(project, target_pack=TARGET_PACK)

    for layer in LAYER_NODE_IDS:
        assert layer in system_prompt
    # A model that is not told the anti-vacuity rule will not guess it, and the
    # rejection it gets back is expensive.
    assert "identity function" in system_prompt
    assert "req.todo" in user_prompt
    assert project.goal in user_prompt


def test_a_repair_instruction_carries_the_rejection_verbatim():
    project = _project()

    _, user_prompt = architect_prompt(
        project,
        target_pack=TARGET_PACK,
        repair="component 'domain' claims unknown requirements: ['req.oops']",
    )

    assert "req.oops" in user_prompt
    assert "change nothing else" in user_prompt


# --------------------------------------------------------------------------
# Live: a real model decomposing a real product
# --------------------------------------------------------------------------


def _require_login():
    import shutil
    from pathlib import Path

    if shutil.which("claude") is None:
        pytest.skip("live test; the `claude` CLI is not on PATH")
    if not (Path.home() / ".claude" / ".credentials.json").exists():
        pytest.skip("live test; run `claude` once to log in first")


@pytest.mark.live
def test_a_real_model_decomposes_a_product_into_checkable_contracts():
    from decimal import Decimal

    from rich_v2.architect import propose_architecture
    from rich_v2.budget import BudgetLedger, RunBudget
    from rich_v2.claude_code_provider import (
        CLAUDE_CODE_PROVIDER,
        ClaudeCodeCliProvider,
    )
    from rich_v2.providers import ModelGateway

    _require_login()
    project = _project()
    gateway = ModelGateway(
        [ClaudeCodeCliProvider()],
        BudgetLedger(
            RunBudget(
                max_model_attempts=6,
                max_input_tokens=400_000,
                max_output_tokens=200_000,
                max_cost_usd=Decimal("5"),
                max_execution_seconds=3_600,
            )
        ),
    )

    outcome = propose_architecture(
        project,
        gateway=gateway,
        provider=CLAUDE_CODE_PROVIDER,
        model="claude-sonnet-5",
        target_pack=TARGET_PACK,
        run_id="run.architect-live",
        task_id="run.architect-live:architect",
        correlation_id="architect-live",
        max_cost_usd=Decimal("1"),
    )

    architecture = outcome.architecture
    architecture.validate_against_project(project)
    compile_architecture(architecture, project)

    # It has to decompose, not restate. Every requirement served, and at least
    # one of them not smeared across every layer.
    allocation = requirement_allocation(architecture)
    assert set(allocation) == {"req.todo", "req.a11y"}
    assert "domain" in layers_of(architecture)

    # And the contracts have to be checkable, which is the whole reason to ask
    # a model instead of a template.
    contracts = [
        contract
        for contract in architecture.contracts
        if contract.node_id != ROOT_NODE_ID
    ]
    assert contracts
    for contract in contracts:
        for operation in contract.operations:
            assert operation.input_type is not None
            assert operation.output_type is not None
        # Anti-vacuity is enforced by the contract constructor, so reaching
        # here already means every constrained operation has a ground example.
        compile_obligation_suite(contract)

    relations = {
        obligation.relation.value
        for contract in contracts
        for obligation in contract.obligations
    }
    assert "example" in relations
    assert outcome.usage.cost_usd > Decimal("0")
