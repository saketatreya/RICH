from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from richbuild.models import AcceptanceScenario, ProjectSpec, Requirement
from richbuild.planner import plan_nextjs_architecture
import richbuild.target_packs.nextjs as nextjs_target
from richbuild.target_packs.nextjs import (
    DestinationNotEmptyError,
    InvalidTargetPackConfig,
    NextJsTargetPack,
    NextJsTargetPackConfig,
    TargetPackError,
    _pnpm_lockfile,
)


def _pack(
    project_name: str = "founder-os", package_scope: str | None = None
) -> NextJsTargetPack:
    return NextJsTargetPack(
        NextJsTargetPackConfig(
            project_name=project_name,
            package_scope=package_scope,
        )
    )


def _approved_project() -> ProjectSpec:
    return ProjectSpec(
        id="project.ownership",
        name="Owned application",
        goal="Persist approved records in a database.",
        audiences=("operators",),
        requirements=(
            Requirement(
                id="req.records",
                title="Store records",
                statement="An operator persists an approved record.",
            ),
        ),
        acceptance_scenarios=(
            AcceptanceScenario(
                id="scenario.records",
                title="Record persists",
                given=("The application is available.",),
                when=("An operator stores a record.",),
                then=("The record remains available.",),
                requirement_ids=("req.records",),
                oracle=(
                    {"action": "open_requirement"},
                    {
                        "action": "assert_visible",
                        "locator": {
                            "kind": "text",
                            "value": "An operator persists an approved record.",
                        },
                    },
                ),
            ),
        ),
    )


def _approved_pack(*, architecture=None) -> NextJsTargetPack:
    project = _approved_project()
    approved_architecture = (
        architecture or plan_nextjs_architecture(project).architecture
    )
    return NextJsTargetPack(
        NextJsTargetPackConfig(
            project_name="owned-application",
            project_spec=project,
            architecture=approved_architecture,
        )
    )


def test_scaffold_contains_strict_full_stack_workspace(tmp_path):
    destination = tmp_path / "generated"
    manifest = _pack().scaffold(destination)

    expected_paths = {
        ".env.example",
        ".github/workflows/ci.yml",
        ".rich/target-pack.json",
        "apps/web/eslint.config.mjs",
        "apps/web/next.config.mjs",
        "apps/web/src/app/api/health/route.ts",
        "apps/web/src/app/layout.tsx",
        "apps/web/src/app/page.tsx",
        "docker-compose.yml",
        "packages/adapters/src/index.ts",
        "packages/contracts/src/index.ts",
        "packages/db/drizzle.config.ts",
        "packages/db/migrations/0000_initial.sql",
        "packages/domain/src/index.ts",
        "packages/ui/src/button.tsx",
        "playwright.config.ts",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "tests/e2e/rich-acceptance-reporter.ts",
        "tests/e2e/smoke.spec.ts",
        "tests/unit/domain.test.ts",
        "vitest.config.ts",
    }
    actual_paths = {
        str(path.relative_to(destination))
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert expected_paths <= actual_paths

    root_package = json.loads((destination / "package.json").read_text())
    assert root_package["packageManager"] == "pnpm@10.34.5"
    assert root_package["scripts"]["ci"].endswith("pnpm run test:e2e")
    assert root_package["scripts"]["audit"] == (
        "pnpm audit --audit-level=moderate"
    )
    assert root_package["pnpm"]["overrides"] == {
        "@esbuild-kit/core-utils>esbuild": "0.25.12",
        "minimatch": "10.2.5",
        "postcss": "8.5.23",
        "sharp": "0.35.3",
    }
    assert root_package["devDependencies"]["typescript"] == "5.9.3"
    assert all(
        not version.startswith(("^", "~", "*"))
        for version in root_package["devDependencies"].values()
    )
    next_config = (destination / "apps/web/next.config.mjs").read_text()
    assert (
        'outputFileTracingRoot: fileURLToPath(new URL(".", import.meta.url))'
        in next_config
    )

    web_package = json.loads((destination / "apps/web/package.json").read_text())
    assert web_package["dependencies"]["next"] == "16.2.12"
    assert web_package["dependencies"]["@founder-os/domain"] == "workspace:*"
    package_tsconfig = json.loads(
        (destination / "packages/domain/tsconfig.json").read_text()
    )
    assert package_tsconfig["compilerOptions"]["noEmit"] is True
    assert "emitDeclarationOnly" not in package_tsconfig["compilerOptions"]
    assert manifest.project_name == "founder-os"
    assert manifest.package_scope == "@founder-os"
    assert manifest.schema_version == "rich.target-pack-manifest/v2"
    assert manifest.target_pack_version == "1.3.0"


def test_manifest_is_deterministic_and_accounts_for_every_content_file(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = _pack(package_scope="@studio").scaffold(first)
    second_manifest = _pack(package_scope="@studio").scaffold(second)

    assert first_manifest == second_manifest
    assert (first / ".rich/target-pack.json").read_bytes() == (
        second / ".rich/target-pack.json"
    ).read_bytes()

    manifest_document = json.loads((first / ".rich/target-pack.json").read_text())
    assert manifest_document["content_digest"] == first_manifest.content_digest
    listed = {entry["path"]: entry for entry in manifest_document["files"]}
    actual = {
        str(path.relative_to(first)): path
        for path in first.rglob("*")
        if path.is_file() and path.relative_to(first) != Path(".rich/target-pack.json")
    }
    assert listed.keys() == actual.keys()
    for relative, path in actual.items():
        content = path.read_bytes()
        assert listed[relative]["size"] == len(content)
        assert listed[relative]["sha256"] == (
            f"sha256:{hashlib.sha256(content).hexdigest()}"
        )
    assert listed["apps/web/next-env.d.ts"]["mutable"] is True
    assert listed["apps/web/next-env.d.ts"]["managed_by"] == "nextjs"
    assert all(
        entry["mutable"] is False
        for path, entry in listed.items()
        if path != "apps/web/next-env.d.ts"
    )


def test_ci_uses_frozen_lockfile_and_checks_supply_chain_and_invariants(tmp_path):
    destination = tmp_path / "generated"
    _pack().scaffold(destination)

    workflow = (destination / ".github/workflows/ci.yml").read_text()
    install = (
        "      - run: pnpm install --frozen-lockfile --ignore-scripts\n"
    )
    audit = "      - run: pnpm run audit\n"
    verify = "      - run: pnpm run verify:manifest\n"
    pipeline = "      - run: pnpm run ci\n"
    clean_tree = (
        "      - run: git diff --exit-code -- . "
        "':(exclude)apps/web/next-env.d.ts'\n"
    )

    assert install in workflow
    assert "--no-frozen-lockfile" not in workflow
    assert workflow.index(install) < workflow.index(audit)
    assert workflow.index(audit) < workflow.index(verify)
    assert workflow.index(verify) < workflow.index(pipeline)
    assert workflow.rindex(verify) > workflow.index(pipeline)
    assert workflow.index(clean_tree) > workflow.rindex(verify)

    lockfile = (destination / "pnpm-lock.yaml").read_text()
    assert "lockfileVersion: '9.0'" in lockfile
    assert "'@esbuild-kit/core-utils>esbuild': 0.25.12" in lockfile
    assert "minimatch: 10.2.5" in lockfile
    assert "postcss: 8.5.23" in lockfile
    assert "sharp: 0.35.3" in lockfile
    assert "@founder-os/domain" in lockfile


def test_lockfile_matches_optional_workspace_importers_and_approved_intent():
    lockfile = _pnpm_lockfile(
        "@product",
        include_data=False,
        include_adapters=False,
        include_intent=True,
    )

    assert "@rich-template" not in lockfile
    assert "\n  packages/db:" not in lockfile
    assert "\n  packages/adapters:" not in lockfile
    assert "'@product/adapters':" not in lockfile
    assert (
        "\n  packages/domain:\n"
        "    dependencies:\n"
        "      '@product/contracts':\n"
        "        specifier: workspace:*\n"
        "        version: link:../contracts\n"
    ) in lockfile


@pytest.mark.skipif(shutil.which("node") is None, reason="node is unavailable")
def test_manifest_verifier_allows_next_managed_file_but_rejects_source_drift(
    tmp_path,
):
    destination = tmp_path / "generated"
    _pack().scaffold(destination)
    command = ["node", ".rich/verify-manifest.mjs"]

    clean = subprocess.run(
        command,
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
    )
    assert clean.returncode == 0, clean.stderr

    next_env = destination / "apps/web/next-env.d.ts"
    valid_next_rewrite = (
        '/// <reference types="next" />\n'
        '/// <reference types="next/image-types/global" />\n'
        'import "./.next/types/routes.d.ts";\n'
        "\n"
        "// NOTE: This file should not be edited\n"
        "// see https://nextjs.org/docs/app/api-reference/config/typescript "
        "for more information.\n"
    )
    next_env.write_text(valid_next_rewrite)
    managed = subprocess.run(
        command,
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
    )
    assert managed.returncode == 0, managed.stderr

    for hostile in (
        '/// <reference path="../../attacker.d.ts" />\n',
        "declare global { interface Window { compromised: true } }\n",
    ):
        next_env.write_text(
            '/// <reference types="next" />\n'
            '/// <reference types="next/image-types/global" />\n'
            + hostile
        )
        rejected = subprocess.run(
            command,
            cwd=destination,
            check=False,
            capture_output=True,
            text=True,
        )
        assert rejected.returncode != 0
        assert "unsupported mutable content" in rejected.stderr
    next_env.write_text(valid_next_rewrite)

    manifest_path = destination / ".rich/target-pack.json"
    original_manifest = manifest_path.read_text()
    manifest_document = json.loads(original_manifest)
    page_entry = next(
        entry
        for entry in manifest_document["files"]
        if entry["path"] == "apps/web/src/app/page.tsx"
    )
    page_entry["mutable"] = True
    page_entry["managed_by"] = "nextjs"
    manifest_path.write_text(json.dumps(manifest_document))
    forged = subprocess.run(
        command,
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
    )
    assert forged.returncode != 0
    assert "apps/web/src/app/page.tsx: invalid mutable declaration" in forged.stderr
    manifest_path.write_text(original_manifest)

    (destination / "apps/web/src/app/page.tsx").write_text("export default 1;\n")
    changed = subprocess.run(
        command,
        cwd=destination,
        check=False,
        capture_output=True,
        text=True,
    )
    assert changed.returncode != 0
    assert "apps/web/src/app/page.tsx: generated content changed" in changed.stderr


def test_existing_empty_destination_is_committed_without_staging_debris(tmp_path):
    destination = tmp_path / "empty"
    destination.mkdir()

    _pack().scaffold(destination)

    assert (destination / "package.json").is_file()
    assert not list(tmp_path.glob(".empty.rich-staging-*"))


def test_unmanaged_destination_is_rejected_without_touching_content(tmp_path):
    destination = tmp_path / "owned"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("user content")

    with pytest.raises(DestinationNotEmptyError, match="unmanaged"):
        _pack().scaffold(destination)

    assert sentinel.read_text() == "user content"
    assert list(destination.iterdir()) == [sentinel]
    assert not list(tmp_path.glob(".owned.rich-staging-*"))


@pytest.mark.parametrize(
    "project_name",
    [
        "../escape",
        "Uppercase",
        "contains space",
        "-leading",
        "trailing-",
        "contains/slash",
        "node_modules",
        "a" * 64,
        "",
    ],
)
def test_unsafe_project_names_are_rejected(project_name):
    with pytest.raises(InvalidTargetPackConfig, match="project_name"):
        NextJsTargetPackConfig(project_name=project_name)


@pytest.mark.parametrize(
    "scope",
    ["studio", "@Upper", "@two words", "@bad/", "@-leading", "@trailing-", "@a_b"],
)
def test_unsafe_package_scopes_are_rejected(scope):
    with pytest.raises(InvalidTargetPackConfig, match="package_scope"):
        NextJsTargetPackConfig(project_name="safe-project", package_scope=scope)


def test_symlink_destination_is_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    destination = tmp_path / "linked"
    destination.symlink_to(actual, target_is_directory=True)

    with pytest.raises(DestinationNotEmptyError, match="symbolic link"):
        _pack().scaffold(destination)

    assert list(actual.iterdir()) == []


def test_approved_architecture_must_own_every_rendered_component_path():
    valid = _approved_pack()
    architecture = valid.config.architecture
    assert architecture is not None
    nodes = tuple(
        replace(node, owned_paths=("packages/ui",))
        if node.id == "web"
        else node
        for node in architecture.nodes
    )
    insufficient = replace(architecture, nodes=nodes)

    with pytest.raises(
        TargetPackError,
        match=r"does not own rendered paths: .*'apps/web/",
    ):
        _approved_pack(architecture=insufficient).render_files()


def test_approved_oracle_compiles_to_protected_observable_browser_steps():
    files = _approved_pack().render_files()
    scenario_path = next(
        path
        for path in files
        if path.startswith("tests/e2e/scenarios/")
    )
    scenario_test = files[scenario_path].decode()
    playwright_config = files["playwright.config.ts"].decode()
    reporter = files[
        "tests/e2e/rich-acceptance-reporter.ts"
    ].decode()

    assert 'type: "rich.acceptance-scenario"' in scenario_test
    assert 'await page.goto("/capabilities/' in scenario_test
    assert "An operator persists an approved record." in scenario_test
    assert ".toBeVisible()" in scenario_test
    assert "passedScenarioIds" in reporter
    assert "RICH_ACCEPTANCE_COVERAGE " in reporter
    assert "readFileSync(contextFile" in playwright_config
    assert (
        "delete process.env.RICH_ACCEPTANCE_CONTEXT_FILE"
        in playwright_config
    )
    assert "unlinkSync(contextFile)" in playwright_config
    assert 'env: { RICH_ACCEPTANCE_CONTEXT_FILE: "" }' in playwright_config


def test_resource_node_owned_paths_do_not_authorize_generated_source():
    valid = _approved_pack()
    architecture = valid.config.architecture
    assert architecture is not None
    nodes = tuple(
        replace(node, owned_paths=())
        if node.id == "data"
        else replace(node, owned_paths=("packages/db",))
        if node.id == "postgres"
        else node
        for node in architecture.nodes
    )
    forged = replace(architecture, nodes=nodes)

    with pytest.raises(
        TargetPackError,
        match=r"does not own rendered paths: .*'packages/db/",
    ):
        _approved_pack(architecture=forged).render_files()


def test_architecture_ownership_rejects_glob_syntax():
    valid = _approved_pack()
    architecture = valid.config.architecture
    assert architecture is not None
    nodes = tuple(
        replace(node, owned_paths=("apps/*", "packages/ui"))
        if node.id == "web"
        else node
        for node in architecture.nodes
    )
    globbed = replace(architecture, nodes=nodes)

    with pytest.raises(TargetPackError, match="cannot use glob syntax"):
        _approved_pack(architecture=globbed).render_files()


def test_infrastructure_allowlist_is_exact_and_rejects_traversal(monkeypatch):
    original = nextjs_target._intent_files

    def add_unapproved_test(*args, **kwargs):
        files = original(*args, **kwargs)
        files["tests/unit/requirements/not-approved.test.ts"] = "export {};\n"
        return files

    monkeypatch.setattr(nextjs_target, "_intent_files", add_unapproved_test)
    with pytest.raises(
        TargetPackError,
        match="tests/unit/requirements/not-approved.test.ts",
    ):
        _approved_pack().render_files()

    def add_traversal(*args, **kwargs):
        files = original(*args, **kwargs)
        files["../escape.ts"] = "export {};\n"
        return files

    monkeypatch.setattr(nextjs_target, "_intent_files", add_traversal)
    with pytest.raises(TargetPackError, match="escapes the scaffold"):
        _approved_pack().render_files()


def _architecture_with_obligations():
    """The planner's own architecture, plus one claim beyond a ground example.

    Built by extension rather than replacement: the domain node's ports name
    the planner's operations, so swapping its contract out would break the
    architecture before the pack ever saw it.
    """

    from richbuild.models import (
        ObligationExample,
        ObligationRelation,
        ObligationTier,
        OperationContract,
        ProofObligation,
        ValueType,
        ValueTypeKind,
    )

    project = _approved_project()
    base = plan_nextjs_architecture(project).architecture
    text = ValueType(kind=ValueTypeKind.STRING, max_length=64)
    normalize = OperationContract(
        id="operation:domain:normalizeTitle",
        name="normalizeTitle",
        description="Normalize a record title.",
        requirement_ids=("req.records",),
        input_schema=text.json_schema(),
        output_schema=text.json_schema(),
        input_type=text,
        output_type=text,
    )
    extra = (
        ProofObligation(
            id="obligation:domain:normalize:example",
            subject_operation_id=normalize.id,
            relation=ObligationRelation.EXAMPLE,
            tier=ObligationTier.SAMPLE,
            requirement_ids=("req.records",),
            example=ObligationExample(argument="  Record  ", result="Record"),
        ),
        ProofObligation(
            id="obligation:domain:normalize:idempotent",
            subject_operation_id=normalize.id,
            relation=ObligationRelation.IDEMPOTENT,
            tier=ObligationTier.SAMPLE,
            requirement_ids=("req.records",),
            sample_size=16,
        ),
    )
    contracts = tuple(
        replace(
            contract,
            operations=contract.operations + (normalize,),
            obligations=contract.obligations + extra,
        )
        if contract.node_id == "domain"
        else contract
        for contract in base.contracts
    )
    return project, replace(base, contracts=contracts)


def test_a_contract_with_obligations_scaffolds_a_runnable_property_gate(tmp_path):
    project, architecture = _architecture_with_obligations()
    pack = NextJsTargetPack(
        NextJsTargetPackConfig(
            project_name="owned-application",
            project_spec=project,
            architecture=architecture,
        )
    )
    manifest = pack.scaffold(tmp_path / "workspace")
    paths = {item.path for item in manifest.files}

    assert "tests/properties/rich-value-generator.ts" in paths
    # One interface per component, beside its own source.
    interfaces = {p for p in paths if p.endswith("operations-contract.ts")}
    assert interfaces, "each component with claims gets its own pinned surface"
    assert "packages/contracts/src/operations.ts" not in paths, (
        "no single shared module: that made every node's implementation depend "
        "on every other node's contract"
    )
    suites = [p for p in paths if p.startswith("tests/properties/") and p.endswith(".test.ts")]
    assert suites, "the declared obligation must become a suite"

    domain_interface = "packages/contracts/src/operations-contract.ts"
    assert domain_interface in interfaces
    interface = (tmp_path / "workspace" / domain_interface).read_text()
    assert "export interface Operations" in interface
    assert "normalizeTitle" in interface
    domain = (tmp_path / "workspace/tests/properties/contract-domain.test.ts").read_text()
    assert "obligation:domain:normalize:idempotent" in domain
    assert "casesFor(" in domain, "a sampled relation draws its own cases"
    assert "operations.normalizeTitle" in domain

    scripts = json.loads((tmp_path / "workspace/package.json").read_text())["scripts"]
    assert "test:properties" in scripts


def test_an_architecture_that_claims_nothing_scaffolds_no_property_gate(tmp_path):
    """A property run over an empty directory passes, and a passing check that
    checked nothing is the failure this design exists to avoid."""

    project, architecture = _architecture_with_obligations()
    silent = replace(
        architecture,
        contracts=tuple(
            replace(contract, obligations=())
            for contract in architecture.contracts
        ),
    )
    manifest = NextJsTargetPack(
        NextJsTargetPackConfig(
            project_name="owned-application",
            project_spec=project,
            architecture=silent,
        )
    ).scaffold(tmp_path / "workspace")

    paths = {item.path for item in manifest.files}
    assert not [p for p in paths if p.startswith("tests/properties/")]
    assert not [p for p in paths if p.endswith("operations-contract.ts")]
    assert "packages/contracts/src/operations.ts" not in paths
