from richbuild.coding import ApprovalWitness, DEFAULT_LIMITS, build_task_prompt
from richbuild.compiler import compile_architecture
from richbuild.interview import AdaptiveInterview, InterviewState
from richbuild.models import (
    Contract,
    EdgeKind,
    ObligationExample,
    ObligationRelation,
    ObligationTier,
    OperationContract,
    ProofObligation,
    ValueType,
    ValueTypeKind,
)
from richbuild.planner import _printable, plan_nextjs_architecture


def _project(*, external=False):
    statement = "A member can add a task and it remains after refresh."
    if external:
        statement += " Send an email through an external API."
    answers = {
        "goal": "A persistent team todo application",
        "audiences": ["small product teams"],
        "roles": ["Members can manage tasks only in their own team."],
        "capabilities": [
            {
                "id": "req.todo",
                "title": "Manage tasks",
                "statement": statement,
            }
        ],
        "data_policy": ["Tasks remain until explicitly deleted."],
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
                        "action": "fill",
                        "locator": {"kind": "label", "value": "New task"},
                        "value": "Buy milk",
                    },
                    {
                        "action": "click",
                        "locator": {
                            "kind": "role",
                            "value": "button",
                            "name": "Add task",
                        },
                    },
                    {"action": "reload"},
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
                "then": ["They can add and complete a task."],
                "requirement_ids": ["req.a11y"],
                "oracle": [
                    {"action": "navigate", "value": "/"},
                    {"action": "keyboard", "value": "Tab"},
                    {
                        "action": "assert_visible",
                        "locator": {"kind": "role", "value": "textbox"},
                    },
                ],
            },
        ],
    }
    if external:
        answers["integration_failure_policy"] = [
            "Queue email during provider outage and show pending state."
        ]
    return AdaptiveInterview(
        InterviewState("project.todo", "Todo", answers=answers)
    ).compile()


def test_web_plan_is_valid_and_keeps_approved_semantics_in_contracts():
    project = _project()

    proposal = plan_nextjs_architecture(project)
    architecture = proposal.architecture

    architecture.validate_against_project(project)
    assert architecture.target_pack == "nextjs-app-router"
    assert {"app", "web", "domain", "data", "postgres"} <= set(
        architecture.node_index
    )
    assert set(project.requirement_index) <= {
        requirement_id
        for contract in architecture.contracts
        for requirement_id in contract.traced_requirement_ids
    }
    serialized_contracts = str([contract.to_dict() for contract in architecture.contracts])
    assert "A member can add a task" in serialized_contracts
    assert "Buy milk remains after refresh" in serialized_contracts
    compiled = compile_architecture(architecture, project)
    assert "domain" in compiled.task_index["web"].dependency_ids
    assert "postgres" not in compiled.task_index


def test_external_requirement_adds_adapter_boundary_and_failure_policy():
    proposal = plan_nextjs_architecture(_project(external=True))
    architecture = proposal.architecture

    assert "adapters" in architecture.node_index
    assert any(
        edge.kind is EdgeKind.CAPABILITY and edge.target_node_id == "adapters"
        for edge in architecture.edges
    )
    assert not proposal.risks


def test_same_project_produces_identical_architecture_document():
    project = _project()

    assert (
        plan_nextjs_architecture(project).architecture.to_dict()
        == plan_nextjs_architecture(project).architecture.to_dict()
    )


def test_every_operation_carries_a_sampleable_type_and_a_ground_example():
    architecture = plan_nextjs_architecture(_project()).architecture

    for contract in architecture.contracts:
        anchored = {
            obligation.subject_operation_id
            for obligation in contract.obligations
            if obligation.relation is ObligationRelation.EXAMPLE
        }
        assert anchored == {operation.id for operation in contract.operations}, (
            f"{contract.id} leaves an operation with no ground example"
        )
        for operation in contract.operations:
            assert operation.input_type is not None
            assert operation.output_type is not None
            # A domain a generator cannot draw from admits no sample-tier
            # obligation at all, so this is the precondition for the whole
            # property rung.
            assert operation.input_type.is_finitely_sampleable
            assert operation.output_type.is_finitely_sampleable
            # The schema is a derived view; disagreement is rejected at
            # construction, so this is the invariant that keeps it that way.
            assert operation.input_type.json_schema() == operation.input_schema


def test_examples_inhabit_the_types_they_illustrate_even_for_awkward_prose():
    project = _project()

    architecture = plan_nextjs_architecture(project).architecture

    for contract in architecture.contracts:
        operations = contract.operation_index
        for obligation in contract.obligations:
            subject = operations[obligation.subject_operation_id]
            assert subject.input_type.accepts(obligation.example.argument)
            assert subject.output_type.accepts(obligation.example.result)


def test_prose_outside_the_declared_character_set_is_coerced_not_dropped():
    # Product prose routinely carries curly quotes and dashes; an example that
    # did not inhabit its own type would fail contract validation outright.
    assert _printable("It’s ready — now") == "It?s ready ? now"
    assert _printable("") == "?"


def test_quality_constraints_become_invariants_that_cite_their_obligation():
    project = _project()

    architecture = plan_nextjs_architecture(project).architecture

    contract = architecture.contract_index["contract:domain"]
    statements = {invariant.statement for invariant in contract.invariants}
    assert "Task actions satisfy keyboard accessibility." in statements
    # Functional requirements describe a path through the system, not a
    # property of it, so they stay operations only.
    assert "A member can add a task and it remains after refresh." not in statements

    obligations = contract.obligation_index
    for invariant in contract.invariants:
        assert invariant.obligation_ids
        for obligation_id in invariant.obligation_ids:
            assert obligation_id in obligations


def test_obligations_do_not_widen_requirement_tracing():
    project = _project()

    architecture = plan_nextjs_architecture(project).architecture

    architecture.validate_against_project(project)
    for contract in architecture.contracts:
        assert contract.traced_requirement_ids <= set(project.requirement_index)


def test_a_contract_slice_carries_the_typed_view_without_the_derived_schema():
    architecture = plan_nextjs_architecture(_project()).architecture
    contract = architecture.contract_index["contract:domain"]

    projection = contract.projection({"req.a11y"})

    assert [operation["id"] for operation in projection["operations"]] == [
        "operation:domain:req_a11y"
    ]
    assert [obligation["id"] for obligation in projection["obligations"]] == [
        "obligation:domain:req_a11y:example"
    ]
    assert [invariant["id"] for invariant in projection["invariants"]] == [
        "invariant:domain:req_a11y"
    ]
    operation = projection["operations"][0]
    assert "input_type" in operation and "input_schema" not in operation
    assert "output_type" in operation and "output_schema" not in operation
    assert "metadata" not in projection
    # The full document stays lossless; only the view drops what it can derive.
    assert "input_schema" in contract.to_dict()["operations"][0]


def test_the_slice_keeps_operations_a_retained_obligation_names():
    subject = OperationContract(
        id="op.encode",
        name="encode",
        input_schema={"type": "boolean"},
        output_schema={"type": "boolean"},
        requirement_ids=("req.one",),
        input_type=ValueType(kind=ValueTypeKind.BOOLEAN),
        output_type=ValueType(kind=ValueTypeKind.BOOLEAN),
    )
    witness = OperationContract(
        id="op.decode",
        name="decode",
        input_schema={"type": "boolean"},
        output_schema={"type": "boolean"},
        requirement_ids=("req.two",),
        input_type=ValueType(kind=ValueTypeKind.BOOLEAN),
        output_type=ValueType(kind=ValueTypeKind.BOOLEAN),
    )
    contract = Contract(
        id="contract.codec",
        node_id="node.codec",
        operations=(subject, witness),
        obligations=(
            ProofObligation(
                id="obl.codec",
                relation=ObligationRelation.ROUND_TRIP,
                subject_operation_id="op.encode",
                witness_operation_id="op.decode",
                requirement_ids=("req.one",),
                tier=ObligationTier.PROOF,
            ),
            ProofObligation(
                id="obl.encode.example",
                relation=ObligationRelation.EXAMPLE,
                subject_operation_id="op.encode",
                requirement_ids=("req.one",),
                example=ObligationExample(argument=True, result=False),
            ),
            ProofObligation(
                id="obl.decode.example",
                relation=ObligationRelation.EXAMPLE,
                subject_operation_id="op.decode",
                requirement_ids=("req.two",),
                example=ObligationExample(argument=False, result=True),
            ),
        ),
    )

    projection = contract.projection({"req.one"})

    # op.decode serves a requirement outside the slice but is named by a
    # retained obligation, so dropping it would leave that obligation dangling.
    assert {operation["id"] for operation in projection["operations"]} == {
        "op.encode",
        "op.decode",
    }


def test_types_and_obligations_stay_inside_the_bounded_task_prompt(tmp_path):
    project = _project()
    architecture = plan_nextjs_architecture(project).architecture
    compiled = compile_architecture(architecture, project)
    approval = ApprovalWitness(
        project.id, project.revision, architecture.id, architecture.revision
    )

    for task in compiled.task_index.values():
        bundle = build_task_prompt(
            workspace=tmp_path,
            project=project,
            architecture=architecture,
            task=task,
            approval=approval,
        )
        assert bundle.prompt_bytes <= DEFAULT_LIMITS.max_prompt_bytes
        assert "input_type" in bundle.user_prompt
        assert "obligations" in bundle.user_prompt
