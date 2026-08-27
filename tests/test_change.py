"""What a change costs, and why the blast radius stops where it does.

The claim these tests exist to hold is compositional: a node whose
*implementation* changed cannot affect its consumers, because the information
firewall means no consumer was ever shown that implementation. A node whose
*contract* changed invalidates every consumer, because the contract is exactly
what they were shown.

If that ever stops being true, "modular" is just a diagram.
"""

from dataclasses import replace

import pytest

from richbuild.change import compile_change
from richbuild.models import (
    AcceptanceScenario,
    ObligationExample,
    ObligationRelation,
    ObligationTier,
    OperationContract,
    ProjectSpec,
    ProofObligation,
    Requirement,
    ValueType,
    ValueTypeKind,
)
from richbuild.planner import plan_nextjs_architecture


def _scenario(identifier: str, requirement_id: str, statement: str):
    return AcceptanceScenario(
        id=identifier,
        title=f"{requirement_id} holds",
        given=("The application is available.",),
        when=("An operator uses it.",),
        then=("The outcome is observable.",),
        requirement_ids=(requirement_id,),
        oracle=(
            {"action": "open_requirement"},
            {
                "action": "assert_visible",
                "locator": {"kind": "text", "value": statement},
            },
        ),
    )


def _spec(*, checklist="A founder reviews the approved checklist.") -> ProjectSpec:
    return ProjectSpec(
        id="project.change",
        name="Change",
        goal="Ship a reviewed checklist.",
        audiences=("founders",),
        requirements=(
            Requirement(
                id="req.checklist",
                title="Review the checklist",
                statement=checklist,
            ),
            Requirement(
                id="req.export",
                title="Export the checklist",
                statement="A founder exports the checklist as a file.",
            ),
        ),
        acceptance_scenarios=(
            _scenario("scenario.checklist", "req.checklist", checklist),
            _scenario(
                "scenario.export",
                "req.export",
                "A founder exports the checklist as a file.",
            ),
        ),
    )


def _architecture(spec: ProjectSpec):
    return plan_nextjs_architecture(spec).architecture



def _narrow(architecture, node_id, keep):
    """Give one node responsibility for only some requirements.

    The planner allocates every requirement to every layer, which is honest
    for a layered design -- a feature really does cut through UI, domain and
    data -- and is exactly why layers alone buy no change locality. These
    fixtures build the separation the mechanism is meant to reward.
    """

    keep = set(keep)
    contracts = []
    for contract in architecture.contracts:
        if contract.node_id != node_id:
            contracts.append(contract)
            continue
        contracts.append(
            replace(
                contract,
                operations=tuple(
                    item
                    for item in contract.operations
                    if keep & set(item.requirement_ids)
                ),
                obligations=tuple(
                    item
                    for item in contract.obligations
                    if keep & set(item.requirement_ids)
                ),
            )
        )
    operation_ids = {
        item.id for contract in contracts for item in contract.operations
    }
    nodes = tuple(
        replace(
            node,
            requirement_ids=tuple(sorted(keep)),
            ports=tuple(
                port
                for port in node.ports
                if port.operation_id is None or port.operation_id in operation_ids
            ),
        )
        if node.id == node_id
        else node
        for node in architecture.nodes
    )
    return replace(architecture, nodes=nodes, contracts=tuple(contracts))


def _with_extra_operation(architecture, node_id="domain"):
    """Change one node's contract, leaving every other node untouched."""

    text = ValueType(kind=ValueTypeKind.STRING, max_length=64)
    operation = OperationContract(
        id="operation:domain:normalizeTitle",
        name="normalizeTitle",
        description="Normalize a title.",
        requirement_ids=("req.checklist",),
        input_schema=text.json_schema(),
        output_schema=text.json_schema(),
        input_type=text,
        output_type=text,
    )
    obligation = ProofObligation(
        id="obligation:domain:normalize:example",
        subject_operation_id=operation.id,
        relation=ObligationRelation.EXAMPLE,
        tier=ObligationTier.SAMPLE,
        requirement_ids=("req.checklist",),
        example=ObligationExample(argument="  x  ", result="x"),
    )
    contracts = tuple(
        replace(
            contract,
            operations=contract.operations + (operation,),
            obligations=contract.obligations + (obligation,),
        )
        if contract.node_id == node_id
        else contract
        for contract in architecture.contracts
    )
    return replace(architecture, contracts=contracts)


def _compile(before_spec, after_spec, before_arch=None, after_arch=None):
    return compile_change(
        before_spec=before_spec,
        after_spec=after_spec,
        before_architecture=before_arch or _architecture(before_spec),
        after_architecture=after_arch or _architecture(after_spec),
    )


def test_nothing_changed_means_nothing_is_stale():
    spec = _spec()
    architecture = _architecture(spec)

    change = _compile(spec, spec, architecture, architecture)

    assert change.requirements.is_empty
    assert change.stale == ()
    assert set(change.reusable) == {node.id for node in architecture.nodes}
    assert any("nothing is stale" in note.lower() for note in change.notes)


def test_amending_one_requirement_stales_only_what_owns_it():
    before = _spec()
    after = _spec(checklist="A founder reviews and signs the approved checklist.")
    # domain answers only for the checklist, so the export half is untouched.
    before_arch = _narrow(_architecture(before), "domain", {"req.checklist"})
    after_arch = _narrow(_architecture(after), "domain", {"req.checklist"})

    change = _compile(before, after, before_arch, after_arch)

    assert change.requirements.modified == ("req.checklist",)
    assert change.requirements.added == () and change.requirements.removed == ()
    assert "domain" in change.directly_stale


def test_a_change_a_component_does_not_answer_for_leaves_it_reusable():
    """The whole payoff: cost proportional to the change."""

    before = _spec()
    after = replace(
        before,
        requirements=tuple(
            replace(item, statement="A founder exports the checklist as a PDF.")
            if item.id == "req.export"
            else item
            for item in before.requirements
        ),
    )
    before_arch = _narrow(_architecture(before), "domain", {"req.checklist"})
    after_arch = _narrow(_architecture(after), "domain", {"req.checklist"})

    change = _compile(before, after, before_arch, after_arch)

    assert change.requirements.modified == ("req.export",)
    assert "domain" in change.reusable, (
        "a component that never answered for that requirement cannot be stale"
    )
    assert "domain" not in change.stale


def test_a_layered_allocation_buys_no_change_locality():
    """Recorded because it is the honest limit of the current planner, not a
    defect in this compiler: when every layer owns every requirement, every
    amendment stales the whole application. Modularity under change is a
    property of the allocation, and layers alone do not provide it."""

    before = _spec()
    after = _spec(checklist="A founder reviews and signs the approved checklist.")

    change = _compile(before, after)

    every_node = {node.id for node in _architecture(after).nodes}
    assert set(change.directly_stale) == every_node
    assert change.reusable == ()


def test_a_change_that_leaves_the_contract_alone_never_reaches_a_consumer():
    """The firewall, cashed in.

    A node's owned paths are not part of its promise, so changing them makes
    that node stale and tells its consumers nothing -- they were never shown
    what it writes, only what it guarantees.
    """

    spec = _spec()
    before_arch = _architecture(spec)
    after_arch = replace(
        before_arch,
        nodes=tuple(
            replace(node, owned_paths=node.owned_paths + ("packages/extra",))
            if node.id == "domain"
            else node
            for node in before_arch.nodes
        ),
    )

    change = _compile(spec, spec, before_arch, after_arch)

    assert "domain" in change.directly_stale, "it writes somewhere new"
    assert change.contract_changed == (), "its promise is word for word the same"
    assert change.consumers_stale == (), (
        "no consumer may go stale while every contract is identical"
    )
    assert "web" in change.reusable and "app" in change.reusable
    assert any("never shown an implementation" in note for note in change.notes)


def test_amending_a_requirement_does_change_the_contract_that_states_it():
    """The complement, and worth naming: the built-in planner writes each
    requirement's statement into the operation that serves it, so amending the
    sentence is a contract change and consumers are right to be stale."""

    before = _spec()
    after = _spec(checklist="A founder reviews and signs the approved checklist.")

    change = _compile(before, after)

    assert "domain" in change.contract_changed


def test_a_contract_change_invalidates_every_consumer_transitively():
    """The contract is exactly what a consumer was shown, so changing it
    changes what they were told."""

    spec = _spec()
    before_arch = _architecture(spec)
    after_arch = _with_extra_operation(before_arch, node_id="domain")

    change = _compile(spec, spec, before_arch, after_arch)

    assert change.contract_changed == ("domain",)
    consumers = {
        edge.source_node_id
        for edge in after_arch.edges
        if edge.target_node_id == "domain"
    }
    assert consumers, "the fixture must have something depending on domain"
    assert consumers <= set(change.stale)
    assert "domain" not in change.consumers_stale, "it is the cause, not a consequence"
    assert "domain" not in change.reusable


def test_a_new_requirement_and_a_new_component_are_both_stale():
    before = _spec()
    after = replace(
        before,
        requirements=before.requirements
        + (
            Requirement(
                id="req.archive",
                title="Archive the checklist",
                statement="A founder archives a completed checklist.",
            ),
        ),
        acceptance_scenarios=before.acceptance_scenarios
        + (
            _scenario(
                "scenario.archive",
                "req.archive",
                "A founder archives a completed checklist.",
            ),
        ),
    )

    change = _compile(before, after)

    assert change.requirements.added == ("req.archive",)
    assert change.stale, "a new requirement has to be built by something"


def test_changing_only_the_oracle_counts_as_changing_the_requirement():
    """A scenario is part of what a requirement means: the same sentence
    checked a different way is a different claim."""

    before = _spec()
    rewritten = tuple(
        replace(scenario, then=("The outcome is announced to the reader.",))
        if scenario.id == "scenario.checklist"
        else scenario
        for scenario in before.acceptance_scenarios
    )
    after = replace(before, acceptance_scenarios=rewritten)

    change = _compile(before, after)

    assert change.requirements.modified == ("req.checklist",)
    assert change.stale


def test_reallocating_a_requirement_stales_the_node_whose_job_changed():
    """Textually identical requirements, different responsibility. A component
    asked to answer for a different set of things is stale even when every
    sentence it owns is unchanged."""

    spec = _spec()
    before_arch = _architecture(spec)
    after_arch = _narrow(before_arch, "domain", {"req.checklist"})

    change = _compile(spec, spec, before_arch, after_arch)

    assert "domain" in change.stale
    assert "domain" in change.directly_stale or "domain" in change.contract_changed


@pytest.mark.parametrize("field_name", ["directly_stale", "contract_changed", "reusable"])
def test_every_set_is_sorted_so_a_plan_is_diffable(field_name):
    before = _spec()
    after = _spec(checklist="A founder reviews the signed checklist.")

    values = getattr(_compile(before, after), field_name)

    assert list(values) == sorted(values)


def test_a_node_is_never_both_stale_and_reusable():
    spec = _spec()
    before_arch = _architecture(spec)
    after_arch = _with_extra_operation(before_arch)

    change = _compile(spec, spec, before_arch, after_arch)

    assert not set(change.stale) & set(change.reusable)


def test_every_plan_says_that_verification_still_runs():
    """The one thing a reader must not conclude from a small stale set."""

    spec = _spec()

    change = _compile(spec, spec, _architecture(spec), _architecture(spec))

    assert any("not reusing a verdict" in note for note in change.notes)
