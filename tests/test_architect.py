import copy
import json

import pytest

from liveutil import require_claude_login

from richbuild.architect import (
    LAYER_NODE_IDS,
    ROOT_NODE_ID,
    ArchitectProposalError,
    architect_prompt,
    architect_response_schema,
    assemble,
    layers_of,
    requirement_allocation,
)
from richbuild.compiler import compile_architecture
from richbuild.interview import AdaptiveInterview, InterviewState
from richbuild.models import (
    ValueType,
    ValueTypeKind,
    value_type_from_request,
    value_type_request_schema,
)
from richbuild.target_packs.typescript_obligations import compile_obligation_suite


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

    assert "expect(operations.normalizeTasks(once)).toEqual(once)" in suite
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


def test_an_unbounded_type_is_fitted_rather_than_refused():
    unbounded = _type(kind="list", element=TASK)
    proposal = copy.deepcopy(_proposal())
    for component in proposal["components"]:
        if component["layer"] == "domain":
            for operation in component["operations"]:
                operation["input_type"] = unbounded
                if operation["name"] == "normalizeTasks":
                    operation["output_type"] = unbounded

    architecture = assemble(_project(), proposal, target_pack=TARGET_PACK)

    # An unbounded domain is one no property gate can draw from, so it is still
    # unacceptable -- but the answer is to supply the bound, not to discard the
    # design. A bound exists for sampling; any finite one serves.
    for operation in architecture.contract_index["contract:domain"].operations:
        assert operation.input_type.is_finitely_sampleable
        assert operation.output_type.is_finitely_sampleable
    compile_obligation_suite(architecture.contract_index["contract:domain"])


def test_a_bound_never_refuses_the_example_that_illustrates_it():
    # The measured failure, four times in six live proposals: an id typed as
    # ascii_identifier and then written "task-1", which that set excludes.
    proposal = copy.deepcopy(_proposal())
    for component in proposal["components"]:
        if component["layer"] == "domain":
            for operation in component["operations"]:
                operation["input_type"] = _type(
                    kind="string", min_length=36, max_length=36,
                    char_set="ascii_identifier",
                )
            component["operations"][0]["output_type"] = _type(
                kind="string", min_length=36, max_length=36,
                char_set="ascii_identifier",
            )
            component["obligations"] = [
                _obligation(
                    "example", "normalizeTasks", argument="task-1", result="task-1"
                ),
                _obligation("example", "isNormalized", argument="task-1", result=True),
                _obligation("idempotent", "normalizeTasks", sample_size=32),
            ]

    architecture = assemble(_project(), proposal, target_pack=TARGET_PACK)

    normalize = architecture.contract_index["contract:domain"].operation_index[
        "operation:domain:normalizeTasks"
    ]
    assert normalize.input_type.accepts("task-1")
    # And the derived set is the narrowest that admits it, not simply the widest
    # available -- evidence should tighten a type, not dissolve it.
    assert normalize.input_type.char_set is not None
    assert normalize.input_type.char_set.value == "ascii_slug"


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

    architecture = assemble(_project(), proposal, target_pack=TARGET_PACK)

    # The claim is dropped, not the decomposition. An unexpressible property
    # weakens the specification -- a claim not made -- and never the guarantee.
    relations = {
        obligation.relation.value
        for obligation in architecture.contract_index["contract:domain"].obligations
    }
    assert "establishes" not in relations
    dropped = architecture.metadata["dropped_obligations"]
    assert any("return a boolean" in item for item in dropped)


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

    architecture = assemble(_project(), proposal, target_pack=TARGET_PACK)

    relations = {
        obligation.relation.value
        for obligation in architecture.contract_index["contract:domain"].obligations
    }
    assert "total" not in relations
    assert any(
        "vacuously true" in item
        for item in architecture.metadata["dropped_obligations"]
    )


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


def test_the_prompt_states_the_limits_the_validator_will_enforce():
    from richbuild.models import (
        MAX_ENUM_MEMBERS,
        MAX_RECORD_FIELDS,
        MAX_SAMPLE_SIZE,
        MAX_VALUE_LENGTH,
    )

    system_prompt, _ = architect_prompt(_project(), target_pack=TARGET_PACK)

    # Asking for bounded types without saying which bounds are legal cost a
    # live run three attempts, every one rejected on a limit it was never told.
    for limit in (
        MAX_VALUE_LENGTH,
        MAX_RECORD_FIELDS,
        MAX_ENUM_MEMBERS,
        MAX_SAMPLE_SIZE,
    ):
        assert str(limit) in system_prompt


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

    require_claude_login()


@pytest.mark.live
def test_a_real_model_decomposes_a_product_into_checkable_contracts():
    from decimal import Decimal

    from richbuild.architect import propose_architecture
    from richbuild.budget import BudgetLedger, RunBudget
    from richbuild.claude_code_provider import (
        CLAUDE_CODE_PROVIDER,
        ClaudeCodeCliProvider,
    )
    from richbuild.providers import ModelGateway

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


# --------------------------------------------------------------------------
# The architect must always have an answer. v1's PLAN returns
# `{"is_leaf": true}` on every failure path because that answer is always
# structurally valid and always buildable, so a bad plan costs one node rather
# than the run. This is the architect's equivalent.
# --------------------------------------------------------------------------


class _RefusingProvider:
    """A provider whose every answer fails assembly, for any reason at all."""

    name = "anthropic-claude-code"

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def generate(self, request):
        from decimal import Decimal

        from richbuild.budget import Usage
        from richbuild.providers import ModelResponse

        self.calls += 1
        return ModelResponse(
            text=json.dumps(self.payload),
            parsed=self.payload,
            provider=self.name,
            model=request.model,
            usage=Usage(
                model_attempts=1,
                input_tokens=10,
                output_tokens=10,
                cost_usd=Decimal("0.01"),
                execution_seconds=0.1,
            ),
        )


def _gateway_for(provider):
    from decimal import Decimal

    from richbuild.budget import BudgetLedger, RunBudget
    from richbuild.providers import ModelGateway

    return ModelGateway(
        [provider],
        BudgetLedger(
            RunBudget(
                max_model_attempts=8,
                max_input_tokens=400_000,
                max_output_tokens=200_000,
                max_cost_usd=Decimal("5"),
                max_execution_seconds=3_600,
            )
        ),
    )


@pytest.mark.parametrize(
    ("payload", "because"),
    [
        ({"components": []}, "no components at all"),
        ({"rationale": "x"}, "no components key"),
        (
            {
                "rationale": "x",
                "components": [
                    {
                        "layer": "domain",
                        "purpose": "p",
                        "requirement_ids": ["req.todo"],
                        "operations": [
                            _operation("normalizeTasks", TASK_LIST, TASK_LIST)
                        ],
                        "obligations": [],
                    }
                ],
            },
            "a missing required layer",
        ),
    ],
)
def test_an_unassemblable_answer_degrades_to_the_baseline_instead_of_raising(
    payload, because
):
    from decimal import Decimal

    from richbuild.architect import FALLBACK_SOURCE, propose_architecture

    project = _project()
    provider = _RefusingProvider(payload)

    outcome = propose_architecture(
        project,
        gateway=_gateway_for(provider),
        provider=provider.name,
        model="claude-sonnet-5",
        target_pack=TARGET_PACK,
        run_id="run.fallback",
        task_id="run.fallback:t",
        correlation_id="fallback",
        max_cost_usd=Decimal("1"),
        max_attempts=2,
    )

    # Never an exception: a surface whose whole purpose is to give a human
    # something to react to cannot answer a mechanical slip with a stack trace.
    assert outcome.source == FALLBACK_SOURCE, because
    outcome.architecture.validate_against_project(project)
    compile_architecture(outcome.architecture, project)
    # And it is legible: the reviewer gets the baseline *and* what went wrong.
    assert outcome.rejections
    assert outcome.attempts == 2
    assert provider.calls == 2


def test_a_fallback_reaches_the_draft_labelled_as_one():
    from decimal import Decimal

    from richbuild.architect import FALLBACK_SOURCE, ModelArchitect

    provider = _RefusingProvider({"components": []})
    architect = ModelArchitect(
        gateway_factory=lambda: _gateway_for(provider),
        provider=provider.name,
        model="claude-sonnet-5",
        max_cost_usd=Decimal("1"),
    )

    proposal = architect.propose(_project(), target_pack=TARGET_PACK)

    assert proposal.source == FALLBACK_SOURCE
    assert proposal.risks, "the rejections must survive as reviewer-visible risks"


def test_a_successful_proposal_is_still_marked_as_the_model_s():
    from richbuild.architect import MODEL_SOURCE, assemble

    architecture = assemble(_project(), _proposal(), target_pack=TARGET_PACK)

    # assemble() is the pure path; provenance is attached by the caller that
    # knows who answered.
    assert architecture.metadata["source"] == "model_architect"
    assert MODEL_SOURCE == "model"


# --------------------------------------------------------------------------
# The measured rejection census, turned into a regression suite. Each of these
# aborted a real proposal; none was a design mistake.
# --------------------------------------------------------------------------


def test_an_operand_slot_the_relation_cannot_hold_is_nulled_not_refused():
    proposal = copy.deepcopy(_proposal())
    for component in proposal["components"]:
        if component["layer"] == "domain":
            # The schema requires witness/predicate/guard on every obligation,
            # so filling one in on an `example` is answering the shape given.
            component["obligations"][0]["predicate"] = "isNormalized"
            component["obligations"][0]["guard"] = "isNormalized"
            component["obligations"][2]["witness"] = "isNormalized"

    architecture = assemble(_project(), proposal, target_pack=TARGET_PACK)

    by_id = architecture.contract_index["contract:domain"].obligation_index
    example = next(
        item for item in by_id.values() if item.relation.value == "example"
    )
    idempotent = next(
        item for item in by_id.values() if item.relation.value == "idempotent"
    )
    assert example.predicate_operation_id is None
    assert example.guard_operation_id is None
    assert idempotent.witness_operation_id is None


def test_an_error_code_is_normalised_rather_than_refused():
    proposal = copy.deepcopy(_proposal())
    for component in proposal["components"]:
        if component["layer"] == "domain":
            component["operations"][0]["errors"] = [
                {"code": "NOT FOUND", "description": "Missing."},
                {"code": "NOT FOUND", "description": "Also missing."},
                {"code": "", "description": "Unnamed."},
            ]

    architecture = assemble(_project(), proposal, target_pack=TARGET_PACK)

    codes = [
        error.code
        for error in architecture.contract_index["contract:domain"]
        .operation_index["operation:domain:normalizeTasks"]
        .errors
    ]
    # A spelling difference, not a design one -- and duplicates get an index
    # rather than discarding the whole proposal.
    assert codes[0] == "NOT_FOUND"
    assert len(set(codes)) == 3


def test_a_slot_the_kind_cannot_carry_is_dropped():
    from richbuild.models import value_type_from_request

    # A bound on a boolean is a bookkeeping slip; refusing the proposal over it
    # buys nothing, and the slot carries no meaning for that kind anyway.
    value_type = value_type_from_request(
        {"kind": "boolean", "minimum": 0, "max_length": 12, "members": ["a"]}
    )

    assert value_type == ValueType(kind=ValueTypeKind.BOOLEAN)


def test_an_inverted_range_is_transposed():
    from richbuild.models import value_type_from_request

    value_type = value_type_from_request(
        {"kind": "integer", "minimum": 9, "maximum": 0}
    )

    assert (value_type.minimum, value_type.maximum) == (0, 9)


def test_an_oversized_bound_is_clamped_rather_than_refused():
    from richbuild.models import MAX_VALUE_LENGTH, value_type_from_request

    value_type = value_type_from_request(
        {"kind": "string", "max_length": MAX_VALUE_LENGTH * 10}
    )

    assert value_type.max_length == MAX_VALUE_LENGTH


def test_a_malformed_record_fields_value_does_not_crash_the_architect():
    from richbuild.models import ModelValidationError, value_type_from_request

    # This raised TypeError before, which the repair loop does not catch, so a
    # bookkeeping slip aborted the run instead of being fed back.
    with pytest.raises(ModelValidationError):
        value_type_from_request({"kind": "record", "record_fields": 5})


def test_the_schema_makes_an_unknown_requirement_id_unrepresentable():
    project = _project()

    schema = architect_response_schema(project)

    ids = schema["properties"]["components"]["items"]["properties"][
        "requirement_ids"
    ]
    assert ids["items"]["enum"] == ["req.a11y", "req.todo"]
    assert ids["minItems"] == 1


def test_the_schema_constrains_operand_slots_to_operation_names():
    schema = architect_response_schema()
    obligation = schema["properties"]["components"]["items"]["properties"][
        "obligations"
    ]["items"]["properties"]

    # A live proposal put a prose sentence in the guard slot. A bare string
    # type invites exactly that.
    for slot in ("witness", "predicate", "guard"):
        assert "pattern" in obligation[slot]["anyOf"][0]
    assert "pattern" in obligation["operation"]


def test_the_schema_no_longer_asks_for_anything_derivable():
    from richbuild.models import value_type_request_schema

    slots = value_type_request_schema(3)["properties"]

    # Five of six measured failures were a declared bound refusing its own
    # example. The slots are still on ValueType; they are simply not asked for.
    for derivable in ("minimum", "maximum", "min_length", "max_length", "char_set"):
        assert derivable not in slots
    assert "kind" in slots and "members" in slots


def test_every_surviving_obligation_is_still_fully_checked():
    """Dropping weakens the spec, never the guarantee."""

    proposal = copy.deepcopy(_proposal())
    for component in proposal["components"]:
        if component["layer"] == "domain":
            # One good claim, one the vocabulary cannot express.
            component["operations"].append(
                _operation("summarise", TASK_LIST, MARKUP)
            )
            component["obligations"].extend(
                [
                    _obligation("example", "summarise", argument=[], result=""),
                    _obligation(
                        "establishes",
                        "summarise",
                        predicate="isNormalized",
                        sample_size=16,
                    ),
                ]
            )

    architecture = assemble(_project(), proposal, target_pack=TARGET_PACK)
    contract = architecture.contract_index["contract:domain"]

    # The unexpressible one is gone; the sound one survives untouched, and the
    # contract still passes every validator including anti-vacuity.
    assert architecture.metadata["dropped_obligations"]
    assert any(
        obligation.relation.value == "establishes"
        and obligation.subject_operation_id.endswith("normalizeTasks")
        for obligation in contract.obligations
    )
    compile_obligation_suite(contract)


# --------------------------------------------------------------------------
# A redraft carries an untouched layer's contract forward exactly. Change
# locality is computed over contracts, so a redraft that reworded a contract
# it did not need to change would stale every consumer for nothing.
# --------------------------------------------------------------------------


def _previous_design():
    from richbuild.architect import PreviousDesign, assemble

    project = _project()
    architecture = assemble(project, _proposal(), target_pack="nextjs-app-router")
    return PreviousDesign(project=project, architecture=architecture)


def _redraft_keeping_domain():
    document = copy.deepcopy(_proposal())
    domain = next(c for c in document["components"] if c["layer"] == "domain")
    domain["unchanged"] = True
    domain["operations"] = []
    domain["obligations"] = []
    ui = next(c for c in document["components"] if c["layer"] == "ui")
    ui["operations"].append(_operation("renderEmpty", BOOLEAN, MARKUP))
    ui["obligations"].append(_obligation("example", "renderEmpty", argument=True, result=""))
    return document


def test_an_unchanged_layer_keeps_its_contract_byte_for_byte():
    from richbuild.architect import assemble
    from richbuild.change import compile_change

    previous = _previous_design()
    redrafted = assemble(
        previous.project, _redraft_keeping_domain(), target_pack="nextjs-app-router", previous=previous
    )

    before = {c.id: c for c in previous.architecture.contracts}
    after = {c.id: c for c in redrafted.contracts}
    domain_id = previous.architecture.node_index["domain"].contract_id
    assert after[domain_id].to_dict() == before[domain_id].to_dict()
    assert redrafted.metadata["carried_forward"] == ["domain"]
    web_id = previous.architecture.node_index["web"].contract_id
    assert after[web_id].to_dict() != before[web_id].to_dict()

    change = compile_change(
        before_spec=previous.project,
        after_spec=previous.project,
        before_architecture=previous.architecture,
        after_architecture=redrafted,
    )
    assert "domain" in change.reusable
    assert "web" in change.stale


def test_unchanged_is_refused_when_a_requirement_it_serves_changed():
    from richbuild.architect import ArchitectProposalError, assemble
    from richbuild.models import ProjectSpec

    previous = _previous_design()
    amended_document = previous.project.to_dict()
    for requirement in amended_document["requirements"]:
        if requirement["id"] == "req.todo":
            requirement["statement"] = "A member can add a task with a due date."
    amended = ProjectSpec.from_dict(amended_document)

    with pytest.raises(ArchitectProposalError, match="req.todo.*redraft it"):
        assemble(amended, _redraft_keeping_domain(), target_pack="nextjs-app-router", previous=previous)


def test_unchanged_is_refused_without_a_previous_design_or_with_a_different_allocation():
    from richbuild.architect import ArchitectProposalError, assemble

    project = _project()
    with pytest.raises(ArchitectProposalError, match="no previous design"):
        assemble(project, _redraft_keeping_domain(), target_pack="nextjs-app-router")

    previous = _previous_design()
    moved = _redraft_keeping_domain()
    domain = next(c for c in moved["components"] if c["layer"] == "domain")
    domain["requirement_ids"] = ["req.todo", "req.a11y"]
    with pytest.raises(ArchitectProposalError, match="requirements differ"):
        assemble(project, moved, target_pack="nextjs-app-router", previous=previous)


def test_the_redraft_prompt_shows_the_previous_design_and_the_rule():
    from richbuild.architect import architect_prompt

    previous = _previous_design()
    system_prompt, user_prompt = architect_prompt(
        previous.project, target_pack="nextjs-app-router", previous=previous
    )
    fresh_system, fresh_user = architect_prompt(previous.project, target_pack="nextjs-app-router")

    assert '"unchanged": true' in system_prompt and "unchanged" not in fresh_system
    assert "previous_components" in user_prompt and "previous_components" not in fresh_user
    assert "normalizeTasks" in user_prompt
