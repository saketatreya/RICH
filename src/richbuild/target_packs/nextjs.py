"""Deterministic Next.js App Router monorepo target pack.

The target pack is intentionally a pure local scaffold generator. It does not
install dependencies, contact a registry, initialize git, or execute generated
code. A complete tree is written to a staging directory and atomically renamed
into place after all content and its manifest have been flushed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import Mapping

from ._nextjs_lock import PNPM_LOCK_TEMPLATE

from ..models import (
    AcceptanceAction,
    AcceptanceScenario,
    AcceptanceStep,
    ArchitectureSpec,
    BrowserLocator,
    BrowserLocatorKind,
    NodeKind,
    ProjectSpec,
)
from .typescript_obligations import (
    GENERATOR_PATH,
    operations_interface_path,
    ObligationCompileError,
    VALUE_GENERATOR_SOURCE,
    compile_obligation_suite,
    compile_operations_interface,
)


class TargetPackError(RuntimeError):
    """Base class for target-pack validation and scaffold errors."""


class InvalidTargetPackConfig(TargetPackError, ValueError):
    """A project or package name cannot safely be used in generated files."""


class DestinationNotEmptyError(TargetPackError):
    """The requested destination contains files not owned by this operation."""


_PROJECT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PACKAGE_SCOPE = re.compile(r"^@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_RESERVED_PROJECT_NAMES = {
    "node_modules",
    "favicon.ico",
    "package.json",
    "pnpm-lock.yaml",
}
_MANIFEST_PATH = ".rich/target-pack.json"
_PNPM_VERSION = "10.34.5"
_POSTCSS_OVERRIDE = "8.5.23"
_SHARP_OVERRIDE = "0.35.3"
_MINIMATCH_OVERRIDE = "10.2.5"
_ESBUILD_KIT_OVERRIDE = "0.25.12"
_LOCK_SCOPE_PLACEHOLDER = "@rich-template"

# The pack's version, stamped into every manifest it writes. One constant: the
# manifest default and the pack had drifted a minor version apart.
TARGET_PACK_VERSION = "1.3.0"
_MUTABLE_GENERATED_FILES = {
    # Next.js owns this declaration shim and may rewrite it during dev/build as its
    # generated type entrypoints evolve.  Every other rendered source is invariant.
    "apps/web/next-env.d.ts": "nextjs",
}
# These are target-pack control files, not application-component source.  Keep this
# as exact paths rather than directory prefixes or glob patterns: adding a new
# infrastructure file must be an explicit, reviewable change to this allowlist.
_TARGET_PACK_INFRASTRUCTURE_PATHS = frozenset(
    {
        ".env.example",
        ".github/workflows/ci.yml",
        ".gitignore",
        ".rich/product-intent.json",
        ".rich/target-pack.json",
        ".rich/verify-manifest.mjs",
        "README.md",
        "docker-compose.yml",
        "package.json",
        "playwright.config.ts",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "tests/e2e/rich-acceptance-reporter.ts",
        "tests/e2e/smoke.spec.ts",
        "tests/unit/domain.test.ts",
        "tsconfig.base.json",
        "vitest.config.ts",
    }
)
_GLOB_METACHARACTERS = frozenset("*?[]{}")


def _validate_project_name(value: str) -> None:
    if (
        not isinstance(value, str)
        or not _PROJECT_NAME.fullmatch(value)
        or value in _RESERVED_PROJECT_NAMES
    ):
        raise InvalidTargetPackConfig(
            "project_name must be a lowercase DNS-safe name of 1-63 characters"
        )


def _validate_package_scope(value: str) -> None:
    if not isinstance(value, str) or not _PACKAGE_SCOPE.fullmatch(value):
        raise InvalidTargetPackConfig(
            "package_scope must be an npm scope such as '@acme' using only "
            "lowercase letters, digits, and interior hyphens"
        )


@dataclass(frozen=True, slots=True)
class NextJsTargetPackConfig:
    """Stable inputs to the deterministic scaffold."""

    project_name: str
    package_scope: str | None = None
    project_spec: ProjectSpec | None = None
    architecture: ArchitectureSpec | None = None

    def __post_init__(self) -> None:
        _validate_project_name(self.project_name)
        if self.package_scope is not None:
            _validate_package_scope(self.package_scope)
        if (self.project_spec is None) != (self.architecture is None):
            raise InvalidTargetPackConfig(
                "project_spec and architecture must be supplied together"
            )
        if self.project_spec is not None and self.architecture is not None:
            if not isinstance(self.project_spec, ProjectSpec) or not isinstance(
                self.architecture, ArchitectureSpec
            ):
                raise InvalidTargetPackConfig(
                    "project_spec and architecture must be validated models"
                )
            if self.architecture.target_pack != "nextjs-app-router":
                raise InvalidTargetPackConfig(
                    "architecture target_pack must be 'nextjs-app-router'"
                )
            self.architecture.validate_against_project(self.project_spec)

    @property
    def scope(self) -> str:
        return self.package_scope or f"@{self.project_name}"


@dataclass(frozen=True, slots=True)
class ManifestFile:
    """Digest metadata for one generated file."""

    path: str
    size: int
    sha256: str
    mutable: bool = False
    managed_by: str | None = None

    def as_dict(self) -> dict[str, object]:
        document: dict[str, object] = {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "mutable": self.mutable,
        }
        if self.managed_by is not None:
            document["managed_by"] = self.managed_by
        return document


@dataclass(frozen=True, slots=True)
class ScaffoldManifest:
    """Content-addressed description of one rendered target-pack tree."""

    project_name: str
    package_scope: str
    content_digest: str
    files: tuple[ManifestFile, ...]
    schema_version: str = "rich.target-pack-manifest/v2"
    target_pack: str = "nextjs-app-router"
    target_pack_version: str = TARGET_PACK_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "target_pack": self.target_pack,
            "target_pack_version": self.target_pack_version,
            "project_name": self.project_name,
            "package_scope": self.package_scope,
            "content_digest": self.content_digest,
            "files": [entry.as_dict() for entry in self.files],
        }

    def to_json(self) -> str:
        return _json(self.as_dict())


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _package_json(
    name: str,
    *,
    scripts: Mapping[str, str] | None = None,
    dependencies: Mapping[str, str] | None = None,
    dev_dependencies: Mapping[str, str] | None = None,
    peer_dependencies: Mapping[str, str] | None = None,
    extra: Mapping[str, object] | None = None,
) -> str:
    document: dict[str, object] = {
        "name": name,
        "version": "0.0.0",
        "private": True,
        "type": "module",
    }
    if scripts:
        document["scripts"] = dict(scripts)
    if dependencies:
        document["dependencies"] = dict(dependencies)
    if dev_dependencies:
        document["devDependencies"] = dict(dev_dependencies)
    if peer_dependencies:
        document["peerDependencies"] = dict(peer_dependencies)
    if extra:
        document.update(extra)
    return _json(document)


def _package_tsconfig(*, jsx: bool = False) -> str:
    compiler_options: dict[str, object] = {
        "noEmit": True,
        "rootDir": "src",
    }
    if jsx:
        compiler_options["jsx"] = "react-jsx"
    return _json(
        {
            "extends": "../../tsconfig.base.json",
            "compilerOptions": compiler_options,
            "include": ["src/**/*.ts", "src/**/*.tsx"],
        }
    )


def _safe_generated_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
    ):
        raise TargetPackError(f"generated path escapes the scaffold: {value!r}")
    return path


def _approved_infrastructure_paths(
    project: ProjectSpec,
    architecture: ArchitectureSpec | None = None,
) -> frozenset[str]:
    """Return the exact target-pack paths authorized by an approved project."""

    _property_paths = (
        tuple(_property_files(architecture)) if architecture is not None else ()
    )

    requirement_routes = _route_segments(
        tuple(requirement.id for requirement in project.requirements)
    )
    scenario_routes = _route_segments(
        tuple(scenario.id for scenario in project.acceptance_scenarios)
    )
    generated_tests = {
        (
            "tests/unit/requirements/"
            f"{requirement_routes[requirement.id].replace('-', '_')}.test.ts"
        )
        for requirement in project.requirements
    }
    generated_tests.update(
        f"tests/e2e/scenarios/{scenario_routes[scenario.id]}.spec.ts"
        for scenario in project.acceptance_scenarios
    )
    return frozenset(
        (
            *_TARGET_PACK_INFRASTRUCTURE_PATHS,
            *generated_tests,
            *_property_paths,
        )
    )


def _validate_rendered_path_ownership(
    paths: Mapping[str, bytes],
    *,
    project: ProjectSpec,
    architecture: ArchitectureSpec,
) -> None:
    """Fail closed when rendered source exceeds approved architecture ownership."""

    owned_roots: list[PurePosixPath] = []
    for node in architecture.nodes:
        for value in node.owned_paths:
            root = _safe_generated_path(value)
            if any(character in value for character in _GLOB_METACHARACTERS):
                raise TargetPackError(
                    f"architecture owned path cannot use glob syntax: {value!r}"
                )
            if node.kind is not NodeKind.RESOURCE:
                owned_roots.append(root)

    infrastructure = _approved_infrastructure_paths(project, architecture)
    unauthorized: list[str] = []
    for value in paths:
        path = _safe_generated_path(value)
        if value in infrastructure:
            continue
        if any(path == root or root in path.parents for root in owned_roots):
            continue
        unauthorized.append(value)

    if unauthorized:
        raise TargetPackError(
            "approved architecture does not own rendered paths: "
            + ", ".join(repr(path) for path in sorted(unauthorized))
        )


def _without_lock_importer(lockfile: str, importer: str) -> str:
    """Remove one optional workspace importer from the pinned lock snapshot."""

    marker = f"\n  {importer}:"
    start = lockfile.find(marker)
    if start < 0:
        raise TargetPackError(f"lock snapshot is missing importer {importer!r}")
    following = re.search(r"\n  \S", lockfile[start + len(marker) :])
    packages = lockfile.find("\npackages:\n", start + len(marker))
    if following is None:
        end = packages
    else:
        end = start + len(marker) + following.start()
        if packages >= 0:
            end = min(end, packages)
    if end < 0:
        raise TargetPackError(f"lock snapshot importer {importer!r} is unterminated")
    return f"{lockfile[:start]}{lockfile[end:]}"


def _pnpm_lockfile(
    scope: str,
    *,
    include_data: bool = True,
    include_adapters: bool = True,
    include_intent: bool = False,
) -> str:
    """Render a frozen lockfile matching the selected deterministic workspace."""

    lockfile = PNPM_LOCK_TEMPLATE
    if not include_adapters:
        adapter_dependency = (
            f"      '{_LOCK_SCOPE_PLACEHOLDER}/adapters':\n"
            "        specifier: workspace:*\n"
            "        version: link:../../packages/adapters\n"
        )
        if lockfile.count(adapter_dependency) != 1:
            raise TargetPackError("lock snapshot adapter dependency is invalid")
        lockfile = lockfile.replace(adapter_dependency, "", 1)
        lockfile = _without_lock_importer(lockfile, "packages/adapters")
    if not include_data:
        lockfile = _without_lock_importer(lockfile, "packages/db")
    if include_intent:
        domain_importer = "\n  packages/domain: {}\n"
        domain_with_contract = (
            "\n  packages/domain:\n"
            "    dependencies:\n"
            f"      '{_LOCK_SCOPE_PLACEHOLDER}/contracts':\n"
            "        specifier: workspace:*\n"
            "        version: link:../contracts\n"
        )
        if lockfile.count(domain_importer) != 1:
            raise TargetPackError("lock snapshot domain importer is invalid")
        lockfile = lockfile.replace(domain_importer, domain_with_contract, 1)
    if lockfile.count(_LOCK_SCOPE_PLACEHOLDER) == 0:
        raise TargetPackError("lock snapshot has no package-scope placeholders")
    return lockfile.replace(_LOCK_SCOPE_PLACEHOLDER, scope)


def _route_segments(values: tuple[str, ...]) -> dict[str, str]:
    """Create stable, collision-resistant path segments from approved ids."""

    segments: dict[str, str] = {}
    for value in values:
        readable = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "item"
        readable = readable[:48].rstrip("-")
        suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        segments[value] = f"{readable}-{suffix}"
    return segments


def _typescript(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _playwright_locator(locator: BrowserLocator) -> str:
    value = _typescript(locator.value)
    if locator.kind is BrowserLocatorKind.ROLE:
        options = (
            ""
            if locator.name is None
            else ", "
            + _typescript({"name": locator.name, "exact": locator.exact})
        )
        return f"page.getByRole({value}{options})"
    if locator.kind is BrowserLocatorKind.LABEL:
        return (
            f"page.getByLabel({value}, "
            f"{{ exact: {_typescript(locator.exact)} }})"
        )
    if locator.kind is BrowserLocatorKind.TEXT:
        return (
            f"page.getByText({value}, "
            f"{{ exact: {_typescript(locator.exact)} }})"
        )
    if locator.kind is BrowserLocatorKind.PLACEHOLDER:
        return (
            f"page.getByPlaceholder({value}, "
            f"{{ exact: {_typescript(locator.exact)} }})"
        )
    if locator.kind is BrowserLocatorKind.TEST_ID:
        return f"page.getByTestId({value})"
    raise TargetPackError(f"unsupported browser locator {locator.kind.value!r}")


def _quoted(value: str | None) -> str:
    return f"\u2018{value or ''}\u2019"


def _describe_locator(locator: BrowserLocator | None) -> str:
    if locator is None:
        return ""
    if locator.kind is BrowserLocatorKind.ROLE:
        return (
            f"the {locator.value} named {_quoted(locator.name)}"
            if locator.name
            else f"the {locator.value}"
        )
    words = {
        BrowserLocatorKind.LABEL: "the field labelled",
        BrowserLocatorKind.TEXT: "the text",
        BrowserLocatorKind.TEST_ID: "the element with test id",
        BrowserLocatorKind.PLACEHOLDER: "the field with placeholder",
    }
    return f"{words[locator.kind]} {_quoted(locator.value)}"


def describe_step(step: AcceptanceStep) -> str:
    """One oracle step as the sentence a tester would say.

    The canvas renders the same sentence (web/src/components/intent/steps.ts)
    from the same data, and this titles the Playwright step, so a person who
    approved "Expect to see 'Buy milk'" reads exactly that in the failure.
    A fixture holds the two renderers to each other.
    """

    where = _describe_locator(step.locator)
    action = step.action
    if action is AcceptanceAction.OPEN_REQUIREMENT:
        return "Open the page for this requirement"
    if action is AcceptanceAction.NAVIGATE:
        return f"Open {_quoted(step.value)}"
    if action is AcceptanceAction.CLICK:
        return f"Click {where}"
    if action is AcceptanceAction.FILL:
        return f"Type {_quoted(step.value)} into {where}"
    if action is AcceptanceAction.PRESS:
        return f"Press {_quoted(step.value)} in {where}"
    if action is AcceptanceAction.KEYBOARD:
        return f"Press {_quoted(step.value)}"
    if action is AcceptanceAction.RELOAD:
        return "Reload the page"
    if action is AcceptanceAction.ASSERT_VISIBLE:
        return f"Expect to see {where}"
    if action is AcceptanceAction.ASSERT_FOCUSED:
        return f"Expect focus on {where}"
    if action is AcceptanceAction.ASSERT_TEXT:
        return f"Expect {where} to say {_quoted(step.value)}"
    if action is AcceptanceAction.ASSERT_VALUE:
        return f"Expect {where} to hold {_quoted(step.value)}"
    if action is AcceptanceAction.ASSERT_URL:
        return f"Expect the path to be {_quoted(step.value)}"
    raise TargetPackError(f"unsupported acceptance action {action.value!r}")


def _playwright_oracle(
    scenario: AcceptanceScenario,
    requirement_route: str,
) -> str:
    """Compile one approved, data-only browser oracle into protected test code."""

    statements: list[str] = []
    for index, step in enumerate(scenario.oracle, start=1):
        action = step.action
        locator = (
            _playwright_locator(step.locator)
            if step.locator is not None
            else None
        )
        value = _typescript(step.value) if step.value is not None else None
        if action is AcceptanceAction.OPEN_REQUIREMENT:
            statements.append(
                f"  await page.goto({_typescript(f'/capabilities/{requirement_route}')});"
            )
        elif action is AcceptanceAction.NAVIGATE:
            statements.append(f"  await page.goto({value});")
        elif action is AcceptanceAction.CLICK:
            statements.append(f"  await {locator}.click();")
        elif action is AcceptanceAction.FILL:
            statements.append(f"  await {locator}.fill({value});")
        elif action is AcceptanceAction.PRESS:
            statements.append(f"  await {locator}.press({value});")
        elif action is AcceptanceAction.KEYBOARD:
            statements.append(f"  await page.keyboard.press({value});")
        elif action is AcceptanceAction.RELOAD:
            statements.append("  await page.reload();")
        elif action is AcceptanceAction.ASSERT_VISIBLE:
            statements.append(f"  await expect({locator}).toBeVisible();")
        elif action is AcceptanceAction.ASSERT_FOCUSED:
            statements.append(f"  await expect({locator}).toBeFocused();")
        elif action is AcceptanceAction.ASSERT_TEXT:
            statements.append(f"  await expect({locator}).toHaveText({value});")
        elif action is AcceptanceAction.ASSERT_VALUE:
            statements.append(f"  await expect({locator}).toHaveValue({value});")
        elif action is AcceptanceAction.ASSERT_URL:
            statements.append(
                f"  await expect(page).toHaveURL(new URL({value}, page.url()).toString());"
            )
        else:  # pragma: no cover - enum exhaustiveness guard
            raise TargetPackError(
                f"unsupported acceptance action {action.value!r}"
            )
        # Each step is a named Playwright step, titled with the sentence the
        # person approved, so a failure names the step in their words.
        title = _typescript(f"{index} \u00b7 {describe_step(step)}")
        statements[-1] = (
            f"  await test.step({title}, async () => {{\n"
            f"  {statements[-1]}\n"
            "  });"
        )
    return "\n".join(statements)


def _intent_files(
    project: ProjectSpec,
    architecture: ArchitectureSpec,
    packages: Mapping[str, str],
    generated_project_name: str,
) -> dict[str, str]:
    """Render approved product semantics into executable, traceable source."""

    requirement_routes = _route_segments(
        tuple(requirement.id for requirement in project.requirements)
    )
    scenario_routes = _route_segments(
        tuple(scenario.id for scenario in project.acceptance_scenarios)
    )
    scenario_index = {
        requirement.id: [
            scenario
            for scenario in project.acceptance_scenarios
            if requirement.id in scenario.requirement_ids
        ]
        for requirement in project.requirements
    }
    capabilities = [
        {
            "requirementId": requirement.id,
            "title": requirement.title,
            "statement": requirement.statement,
            "kind": requirement.kind.value,
            "priority": requirement.priority.value,
            "route": f"/capabilities/{requirement_routes[requirement.id]}",
            "apiRoute": f"/api/capabilities/{requirement_routes[requirement.id]}",
            "scenarioIds": [
                scenario.id for scenario in scenario_index[requirement.id]
            ],
        }
        for requirement in project.requirements
    ]
    operations = [
        {
            "contractId": contract.id,
            "nodeId": contract.node_id,
            "operationId": operation.id,
            "name": operation.name,
            "description": operation.description,
            "requirementIds": list(operation.requirement_ids),
        }
        for contract in architecture.contracts
        for operation in contract.operations
    ]
    intent_document = {
        "schema_version": "rich.generated-intent/v1",
        "project_spec": project.to_dict(),
        "architecture": architecture.to_dict(),
        "requirement_routes": requirement_routes,
        "scenario_test_ids": scenario_routes,
    }
    files = {
        ".rich/product-intent.json": _json(intent_document),
        "packages/contracts/src/product-intent.ts": (
            'import { z } from "zod";\n'
            "\n"
            "export const capabilityResponseSchema = z.object({\n"
            "  requirementId: z.string().min(1),\n"
            "  title: z.string().min(1),\n"
            "  statement: z.string().min(1),\n"
            "  kind: z.enum([\"functional\", \"non_functional\", \"constraint\"]),\n"
            "  priority: z.enum([\"must\", \"should\", \"could\"]),\n"
            "  route: z.string().startsWith(\"/capabilities/\"),\n"
            "  apiRoute: z.string().startsWith(\"/api/capabilities/\"),\n"
            "  scenarioIds: z.array(z.string().min(1)).min(1),\n"
            "});\n"
            "\n"
            "export const architectureOperationSchema = z.object({\n"
            "  contractId: z.string().min(1),\n"
            "  nodeId: z.string().min(1),\n"
            "  operationId: z.string().min(1),\n"
            "  name: z.string().min(1),\n"
            "  description: z.string(),\n"
            "  requirementIds: z.array(z.string().min(1)).min(1),\n"
            "});\n"
            "\n"
            "export const approvedCapabilityContracts = "
            f"z.array(capabilityResponseSchema).parse({_typescript(capabilities)});\n"
            "export const approvedArchitectureOperations = "
            f"z.array(architectureOperationSchema).parse({_typescript(operations)});\n"
            "\n"
            "export type CapabilityResponse = z.infer<typeof capabilityResponseSchema>;\n"
            "export type ArchitectureOperation = z.infer<typeof architectureOperationSchema>;\n"
        ),
        "packages/domain/src/product-intent.ts": (
            f'import {{ approvedCapabilityContracts }} from "{packages["contracts"]}";\n'
            f'import type {{ CapabilityResponse }} from "{packages["contracts"]}";\n'
            "\n"
            "export const approvedRequirementIds = "
            f"{_typescript([requirement.id for requirement in project.requirements])} as const;\n"
            "export type ApprovedRequirementId = typeof approvedRequirementIds[number];\n"
            "\n"
            "export function executeApprovedCapability(\n"
            "  requirementId: ApprovedRequirementId,\n"
            "): CapabilityResponse {\n"
            "  const capability = approvedCapabilityContracts.find(\n"
            "    (candidate) => candidate.requirementId === requirementId,\n"
            "  );\n"
            "  if (!capability) {\n"
            "    throw new Error(`Unknown approved requirement: ${requirementId}`);\n"
            "  }\n"
            "  return capability;\n"
            "}\n"
        ),
        "apps/web/src/app/page.tsx": (
            f'import {{ buildWelcomeMessage }} from "{packages["domain"]}";\n'
            f'import {{ ButtonLink }} from "{packages["ui"]}";\n'
            "\n"
            f"const approvedCapabilities = {_typescript([{'title': item['title'], 'route': item['route']} for item in capabilities])};\n"
            "\n"
            "export default function HomePage() {\n"
            f"  const welcome = buildWelcomeMessage({_typescript(project.name)});\n"
            "  return (\n"
            '    <main className="shell">\n'
            '      <p className="eyebrow">Approved product intent</p>\n'
            f"      <h1>{generated_project_name}: {{welcome.message}}</h1>\n"
            f"      <p>{{{_typescript(project.goal)}}}</p>\n"
            '      <nav aria-label="Approved capabilities">\n'
            "        {approvedCapabilities.map((capability) => (\n"
            "          <ButtonLink key={capability.route} href={capability.route}>\n"
            "            {capability.title}\n"
            "          </ButtonLink>\n"
            "        ))}\n"
            "      </nav>\n"
            '      <ButtonLink href="/api/health">Health</ButtonLink>\n'
            "    </main>\n"
            "  );\n"
            "}\n"
        ),
    }
    for requirement in project.requirements:
        route = requirement_routes[requirement.id]
        scenarios = scenario_index[requirement.id]
        scenario_contracts = [
            {
                "id": scenario.id,
                "title": scenario.title,
                "given": list(scenario.given),
                "when": list(scenario.when),
                "then": list(scenario.then),
            }
            for scenario in scenarios
        ]
        files[f"apps/web/src/app/api/capabilities/{route}/route.ts"] = (
            f'import {{ capabilityResponseSchema }} from "{packages["contracts"]}";\n'
            f'import {{ executeApprovedCapability }} from "{packages["domain"]}";\n'
            "\n"
            "export function GET(): Response {\n"
            "  return Response.json(capabilityResponseSchema.parse(\n"
            f"    executeApprovedCapability({_typescript(requirement.id)}),\n"
            "  ));\n"
            "}\n"
        )
        files[f"apps/web/src/app/capabilities/{route}/page.tsx"] = (
            f'import {{ executeApprovedCapability }} from "{packages["domain"]}";\n'
            "\n"
            f"const approvedScenarios = {_typescript(scenario_contracts)};\n"
            "\n"
            "export default function CapabilityPage() {\n"
            f"  const capability = executeApprovedCapability({_typescript(requirement.id)});\n"
            "  return (\n"
            '    <main className="shell" data-requirement-id={capability.requirementId}>\n'
            f"      <p className=\"eyebrow\">{{{_typescript(requirement.priority.value)}}}</p>\n"
            "      <h1>{capability.title}</h1>\n"
            "      <p>{capability.statement}</p>\n"
            '      <h2>Approved scenarios</h2>\n'
            "      {approvedScenarios.map((scenario) => (\n"
            "        <section\n"
            "          key={scenario.id}\n"
            "          data-scenario-id={scenario.id}\n"
            "          data-testid={scenario.id}\n"
            "        >\n"
            "          <h3>{scenario.title}</h3>\n"
            '          <h4>Given</h4>\n'
            '          <ol data-testid="given">\n'
            "            {scenario.given.map((clause) => <li key={clause}>{clause}</li>)}\n"
            "          </ol>\n"
            '          <h4>When</h4>\n'
            '          <ol data-testid="when">\n'
            "            {scenario.when.map((clause) => <li key={clause}>{clause}</li>)}\n"
            "          </ol>\n"
            '          <h4>Then</h4>\n'
            '          <ol data-testid="then">\n'
            "            {scenario.then.map((clause) => <li key={clause}>{clause}</li>)}\n"
            "          </ol>\n"
            "        </section>\n"
            "      ))}\n"
            "    </main>\n"
            "  );\n"
            "}\n"
        )
        test_segment = route.replace("-", "_")
        files[f"tests/unit/requirements/{test_segment}.test.ts"] = (
            'import { describe, expect, it } from "vitest";\n'
            'import { executeApprovedCapability } from "../../../packages/domain/src/product-intent";\n'
            "\n"
            f"describe({_typescript(requirement.id)}, () => {{\n"
            f"  it({_typescript(f'traces {requirement.title} to approved scenarios')}, () => {{\n"
            f"    const result = executeApprovedCapability({_typescript(requirement.id)});\n"
            f"    expect(result.statement).toBe({_typescript(requirement.statement)});\n"
            f"    expect(result.scenarioIds).toEqual({_typescript([scenario.id for scenario in scenarios])});\n"
            "  });\n"
            "});\n"
        )
    for scenario in project.acceptance_scenarios:
        requirement_id = scenario.requirement_ids[0]
        route = requirement_routes[requirement_id]
        test_segment = scenario_routes[scenario.id]
        requirement = project.requirement_index[requirement_id]
        files[f"tests/e2e/scenarios/{test_segment}.spec.ts"] = (
            'import { expect, test } from "@playwright/test";\n'
            "\n"
            f"test({_typescript(scenario.title)}, {{\n"
            "  annotation: {\n"
            '    type: "rich.acceptance-scenario",\n'
            f"    description: {_typescript(scenario.id)},\n"
            "  },\n"
            "}, async ({ page }) => {\n"
            f"{_playwright_oracle(scenario, route)}\n"
            "});\n"
        )
    return files



def _property_files(architecture: ArchitectureSpec) -> dict[str, str]:
    """Render the proof obligations an architecture declares as a runnable gate.

    Emitted only when a contract actually declares obligations -- the
    deterministic planner declares one EXAMPLE per operation, the architect
    declares what it can express -- because a property gate over nothing would
    pass without checking anything, which is worse than not having one: it
    reads like assurance.

    Everything here is a protected input: the suites are compiled from the
    approved contract, so a worker that could edit them could edit the claim it
    is being held to.

    One interface and one implementation per component. A shared module would
    make every node's code depend on every other node's contract, and change
    locality computed over contracts would mean nothing.
    """

    owned = {node.id: node.owned_paths for node in architecture.nodes}
    files: dict[str, str] = {}
    suites: dict[str, str] = {}
    for contract in architecture.contracts:
        if not contract.obligations:
            continue
        paths = owned.get(contract.node_id, ())
        if not paths:
            continue
        try:
            source = compile_obligation_suite(contract, paths)
            interface = compile_operations_interface([contract], paths)
        except ObligationCompileError:
            # A contract whose claims cannot be rendered runs no property gate.
            # The architect drops unexpressible obligations before this point;
            # this is the belt to that suspenders, and it must not fail a
            # scaffold that is otherwise sound.
            continue
        suites[f"tests/properties/{_slug(contract.id)}.test.ts"] = source
        files[operations_interface_path(paths)] = interface
    if not suites:
        return {}
    return {GENERATOR_PATH: VALUE_GENERATOR_SOURCE, **files, **suites}


def _slug(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() else "-" for character in value.lower()
    )
    return "-".join(part for part in cleaned.split("-") if part) or "contract"


class NextJsTargetPack:
    """Render and atomically materialize the built-in web target pack."""

    target_pack_id = "nextjs-app-router"
    target_pack_version = TARGET_PACK_VERSION

    def __init__(self, config: NextJsTargetPackConfig):
        self.config = config

    def render_files(self) -> dict[str, bytes]:
        """Return the complete deterministic tree, excluding its manifest."""

        scope = self.config.scope
        project = self.config.project_name
        web_package = f"{scope}/web"
        packages = {
            kind: f"{scope}/{kind}"
            for kind in ("contracts", "domain", "db", "ui", "adapters")
        }
        architecture = self.config.architecture
        has_approved_intent = (
            self.config.project_spec is not None and architecture is not None
        )
        include_data = architecture is None or any(
            node.kind is NodeKind.DATA for node in architecture.nodes
        )
        include_adapters = architecture is None or any(
            node.kind is NodeKind.ADAPTER for node in architecture.nodes
        )
        enabled_package_kinds = ["contracts", "domain", "ui"]
        if include_data:
            enabled_package_kinds.append("db")
        if include_adapters:
            enabled_package_kinds.append("adapters")
        root_scripts = {
            "dev": f"pnpm --filter {web_package} dev",
            "build": "pnpm -r --if-present build",
            "typecheck": "pnpm -r --if-present typecheck",
            "lint": "pnpm -r --if-present lint",
            "test": "vitest run --configLoader runner tests/unit",
            "test:properties": (
                "vitest run --configLoader runner tests/properties "
                "--passWithNoTests"
            ),
            "test:watch": "vitest --configLoader runner",
            "test:e2e": "playwright test",
            "audit": "pnpm audit --audit-level=moderate",
            "verify:manifest": "node .rich/verify-manifest.mjs",
            "ci": (
                "pnpm run typecheck && pnpm run lint && pnpm run test "
                "&& pnpm run test:properties && pnpm run build "
                "&& pnpm run test:e2e"
            ),
        }
        if include_data:
            root_scripts.update(
                {
                    "db:generate": f"pnpm --filter {packages['db']} db:generate",
                    "db:migrate": f"pnpm --filter {packages['db']} db:migrate",
                    "db:seed": f"pnpm --filter {packages['db']} db:seed",
                }
            )

        root_package = _json(
            {
                "name": project,
                "version": "0.0.0",
                "private": True,
                "type": "module",
                "packageManager": f"pnpm@{_PNPM_VERSION}",
                "engines": {"node": ">=20.11.0", "pnpm": ">=10.0.0"},
                "scripts": root_scripts,
                "devDependencies": {
                    "@playwright/test": "1.62.0",
                    "@types/node": "22.20.1",
                    "typescript": "5.9.3",
                    "vitest": "4.1.10",
                },
                "pnpm": {
                    "overrides": {
                        # Next currently pins an older PostCSS release.  Keep both
                        # build-time native/image processing and CSS parsing on exact,
                        # reviewed versions until the direct dependency catches up.
                        "postcss": _POSTCSS_OVERRIDE,
                        "sharp": _SHARP_OVERRIDE,
                        # Keep development tooling off known-vulnerable transitive
                        # releases while upstream packages retain broad/old ranges.
                        "minimatch": _MINIMATCH_OVERRIDE,
                        "@esbuild-kit/core-utils>esbuild": _ESBUILD_KIT_OVERRIDE,
                    }
                },
            }
        )

        files: dict[str, str] = {
            ".env.example": (
                "DATABASE_URL=postgresql://rich:rich@localhost:5432/"
                f"{project}\n"
                "NEXT_PUBLIC_APP_NAME="
                f"{project}\n"
            ),
            ".gitignore": (
                "node_modules/\n"
                ".next/\n"
                "dist/\n"
                "coverage/\n"
                "playwright-report/\n"
                "test-results/\n"
                ".env\n"
                ".env.local\n"
                "*.tsbuildinfo\n"
            ),
            ".github/workflows/ci.yml": (
                "name: ci\n"
                "\n"
                "on:\n"
                "  pull_request:\n"
                "  push:\n"
                "    branches: [main]\n"
                "\n"
                "permissions:\n"
                "  contents: read\n"
                "\n"
                "jobs:\n"
                "  verify:\n"
                "    runs-on: ubuntu-latest\n"
                "    timeout-minutes: 20\n"
                "    env:\n"
                f"      DATABASE_URL: postgresql://rich:rich@localhost:5432/{project}\n"
                "    steps:\n"
                "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2\n"
                "        with:\n"
                "          persist-credentials: false\n"
                "      - uses: pnpm/action-setup@7088e561eb65bb68695d245aa206f005ef30921d # v4.1.0\n"
                "        with:\n"
                f"          version: {_PNPM_VERSION}\n"
                "          run_install: false\n"
                "      - uses: actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020 # v4.4.0\n"
                "        with:\n"
                "          node-version: 20\n"
                "          cache: pnpm\n"
                "          cache-dependency-path: pnpm-lock.yaml\n"
                "      - run: pnpm install --frozen-lockfile --ignore-scripts\n"
                "      - run: pnpm run audit\n"
                "      - run: pnpm run verify:manifest\n"
                "      - run: pnpm exec playwright install --with-deps chromium\n"
                "      - run: pnpm run ci\n"
                "      - run: pnpm run verify:manifest\n"
                "      - run: git diff --exit-code -- . "
                "':(exclude)apps/web/next-env.d.ts'\n"
            ),
            "README.md": (
                f"# {project}\n"
                "\n"
                "A RICH-generated Next.js App Router monorepo.\n"
                "\n"
                "## Local development\n"
                "\n"
                "1. Copy `.env.example` to `.env`.\n"
                "2. Run `docker compose up -d postgres`.\n"
                "3. Run `corepack enable && pnpm install --frozen-lockfile "
                "--ignore-scripts`.\n"
                "4. Run `pnpm db:migrate && pnpm dev`.\n"
                "\n"
                "Run the complete local verification pipeline with `pnpm ci`.\n"
                "Dependency changes must update and commit `pnpm-lock.yaml`.\n"
            ),
            ".rich/verify-manifest.mjs": (
                'import { createHash } from "node:crypto";\n'
                'import { lstat, readFile } from "node:fs/promises";\n'
                'import { dirname, resolve, sep } from "node:path";\n'
                'import { fileURLToPath } from "node:url";\n'
                "\n"
                "const root = resolve(dirname(fileURLToPath(import.meta.url)), \"..\");\n"
                'const manifestPath = resolve(root, ".rich/target-pack.json");\n'
                'const manifest = JSON.parse(await readFile(manifestPath, "utf8"));\n'
                'if (manifest.schema_version !== "rich.target-pack-manifest/v2") {\n'
                '  throw new Error("unsupported RICH target-pack manifest schema");\n'
                "}\n"
                "const failures = [];\n"
                "const allowedMutable = new Map([\n"
                '  ["apps/web/next-env.d.ts", "nextjs"],\n'
                "]);\n"
                "const nextReferences = new Set([\n"
                '  \'/// <reference types="next" />\',\n'
                '  \'/// <reference types="next/image-types/global" />\',\n'
                "]);\n"
                "const nextComments = new Set([\n"
                '  "// This file is generated by Next.js and should not be edited.",\n'
                '  "// NOTE: This file should not be edited",\n'
                '  "// see https://nextjs.org/docs/app/api-reference/config/typescript for more information.",\n'
                "]);\n"
                'const nextImport = /^import "\\.\\/\\.next\\/(?:dev\\/)?types\\/routes\\.d\\.ts";$/;\n'
                "const seenMutable = new Set();\n"
                "for (const entry of manifest.files) {\n"
                "  const absolute = resolve(root, entry.path);\n"
                "  if (!absolute.startsWith(`${root}${sep}`)) {\n"
                '    failures.push(`${entry.path}: path escapes project root`);\n'
                "    continue;\n"
                "  }\n"
                "  if (entry.mutable === true) {\n"
                "    if (allowedMutable.get(entry.path) !== entry.managed_by) {\n"
                '      failures.push(`${entry.path}: invalid mutable declaration`);\n'
                "    } else {\n"
                "      seenMutable.add(entry.path);\n"
                "      try {\n"
                "        const metadata = await lstat(absolute);\n"
                "        const content = await readFile(absolute);\n"
                "        if (!metadata.isFile() || content.byteLength > 4096) {\n"
                '          failures.push(`${entry.path}: invalid mutable file`);\n'
                "        } else {\n"
                '          const text = new TextDecoder("utf-8", { fatal: true }).decode(content);\n'
                "          const seenLines = new Set();\n"
                "          let importCount = 0;\n"
                "          let valid = true;\n"
                "          for (const rawLine of text.split(/\\r?\\n/)) {\n"
                "            const line = rawLine.trim();\n"
                "            if (!line) continue;\n"
                "            if (seenLines.has(line)) valid = false;\n"
                "            seenLines.add(line);\n"
                "            if (nextImport.test(line)) importCount += 1;\n"
                "            else if (!nextReferences.has(line) && !nextComments.has(line)) valid = false;\n"
                "          }\n"
                "          for (const reference of nextReferences) {\n"
                "            if (!seenLines.has(reference)) valid = false;\n"
                "          }\n"
                "          if (importCount > 1) valid = false;\n"
                "          if (!valid) failures.push(`${entry.path}: unsupported mutable content`);\n"
                "        }\n"
                "      } catch {\n"
                '        failures.push(`${entry.path}: unreadable mutable file`);\n'
                "      }\n"
                "    }\n"
                "    continue;\n"
                "  }\n"
                "  try {\n"
                "    const metadata = await lstat(absolute);\n"
                "    if (!metadata.isFile()) {\n"
                '      failures.push(`${entry.path}: not a regular file`);\n'
                "      continue;\n"
                "    }\n"
                "    const content = await readFile(absolute);\n"
                '    const digest = `sha256:${createHash("sha256").update(content).digest("hex")}`;\n'
                "    if (content.byteLength !== entry.size || digest !== entry.sha256) {\n"
                '      failures.push(`${entry.path}: generated content changed`);\n'
                "    }\n"
                "  } catch (error) {\n"
                '    failures.push(`${entry.path}: ${error?.code === "ENOENT" ? "missing" : "unreadable"}`);\n'
                "  }\n"
                "}\n"
                "for (const path of allowedMutable.keys()) {\n"
                "  if (!seenMutable.has(path)) {\n"
                '    failures.push(`${path}: missing mutable declaration`);\n'
                "  }\n"
                "}\n"
                "if (failures.length > 0) {\n"
                '  throw new Error(`RICH manifest verification failed\\n${failures.join("\\n")}`);\n'
                "}\n"
                'console.log("RICH manifest verification passed");\n'
            ),
            "package.json": root_package,
            "pnpm-lock.yaml": _pnpm_lockfile(
                scope,
                include_data=include_data,
                include_adapters=include_adapters,
                include_intent=has_approved_intent,
            ),
            "pnpm-workspace.yaml": "packages:\n  - apps/*\n  - packages/*\n",
            "tsconfig.base.json": _json(
                {
                    "compilerOptions": {
                        "target": "ES2022",
                        "lib": ["DOM", "DOM.Iterable", "ES2022"],
                        "module": "ESNext",
                        "moduleResolution": "Bundler",
                        "resolveJsonModule": True,
                        "allowJs": False,
                        "strict": True,
                        "noUncheckedIndexedAccess": True,
                        "exactOptionalPropertyTypes": True,
                        "useUnknownInCatchVariables": True,
                        "forceConsistentCasingInFileNames": True,
                        "isolatedModules": True,
                        "skipLibCheck": True,
                        "verbatimModuleSyntax": True,
                    }
                }
            ),
            "docker-compose.yml": (
                "services:\n"
                "  postgres:\n"
                "    image: postgres:16.4-alpine\n"
                "    environment:\n"
                "      POSTGRES_USER: rich\n"
                "      POSTGRES_PASSWORD: rich\n"
                f"      POSTGRES_DB: {project}\n"
                "    ports:\n"
                '      - "5432:5432"\n'
                "    volumes:\n"
                "      - postgres-data:/var/lib/postgresql/data\n"
                "    healthcheck:\n"
                '      test: ["CMD-SHELL", "pg_isready -U rich"]\n'
                "      interval: 2s\n"
                "      timeout: 2s\n"
                "      retries: 15\n"
                "\n"
                "volumes:\n"
                "  postgres-data:\n"
            ),
            "vitest.config.ts": (
                'import { defineConfig } from "vitest/config";\n'
                "\n"
                "export default defineConfig({\n"
                '  test: {\n    environment: "node",\n    include: ["tests/unit/**/*.test.ts", "tests/properties/**/*.test.ts"],\n  },\n'
                "});\n"
            ),
            "playwright.config.ts": (
                'import { defineConfig, devices } from "@playwright/test";\n'
                'import { readFileSync, unlinkSync } from "node:fs";\n'
                "\n"
                "const contextFile = process.env.RICH_ACCEPTANCE_CONTEXT_FILE;\n"
                "const acceptanceContext = contextFile\n"
                "  ? (() => {\n"
                "      delete process.env.RICH_ACCEPTANCE_CONTEXT_FILE;\n"
                '      const parsed = JSON.parse(readFileSync(contextFile, "utf8"));\n'
                "      unlinkSync(contextFile);\n"
                "      return parsed;\n"
                "    })()\n"
                "  : {\n"
                '      run_id: "standalone",\n'
                '      task_id: "standalone",\n'
                "      attempt: 1,\n"
                '      nonce: "0000000000000000000000000000000000000000000000000000000000000000",\n'
                "    };\n"
                "\n"
                "export default defineConfig({\n"
                '  testDir: "tests/e2e",\n'
                "  fullyParallel: false,\n"
                "  workers: 1,\n"
                "  retries: 0,\n"
                '  reporter: [["./tests/e2e/rich-acceptance-reporter.ts", { context: acceptanceContext }], ["list"]],\n'
                '  outputDir: "test-results/results",\n'
                '  use: {\n'
                '    baseURL: "http://127.0.0.1:3000",\n'
                '    trace: "retain-on-failure",\n'
                '    launchOptions: { args: ["--js-flags=--max-old-space-size=512"] },\n'
                "  },\n"
                "  projects: [{ name: \"chromium\", use: { ...devices[\"Desktop Chrome\"] } }],\n"
                "  webServer: {\n"
                f'    command: "pnpm --filter {web_package} start",\n'
                '    url: "http://127.0.0.1:3000",\n'
                '    env: { RICH_ACCEPTANCE_CONTEXT_FILE: "" },\n'
                "    reuseExistingServer: !process.env.CI,\n"
                "    timeout: 120_000,\n"
                "  },\n"
                "});\n"
            ),
            "tests/e2e/rich-acceptance-reporter.ts": (
                'import type { FullResult, Reporter, TestCase, TestResult, TestStep } from "@playwright/test/reporter";\n'
                "\n"
                'const annotationType = "rich.acceptance-scenario";\n'
                'const outputPrefix = "RICH_ACCEPTANCE_COVERAGE ";\n'
                'const failuresPrefix = "RICH_ACCEPTANCE_FAILURES ";\n'
                "\n"
                "interface ReporterOptions {\n"
                "  readonly context: {\n"
                "    readonly run_id: string;\n"
                "    readonly task_id: string;\n"
                "    readonly attempt: number;\n"
                "    readonly nonce: string;\n"
                "  };\n"
                "}\n"
                "\n"
                "class RichAcceptanceReporter implements Reporter {\n"
                "  private readonly passedScenarioIds = new Set<string>();\n"
                "  private readonly failures: Array<{ scenario_id: string; step: string; message: string }> = [];\n"
                "  private readonly context: ReporterOptions[\"context\"];\n"
                "\n"
                "  constructor(options: ReporterOptions) {\n"
                "    this.context = options.context;\n"
                "  }\n"
                "\n"
                "  onTestEnd(test: TestCase, result: TestResult): void {\n"
                '    if (result.status !== "passed" || test.expectedStatus !== "passed") return;\n'
                "    for (const annotation of test.annotations) {\n"
                "      if (annotation.type === annotationType && annotation.description) {\n"
                "        this.passedScenarioIds.add(annotation.description);\n"
                "      }\n"
                "    }\n"
                "  }\n"
                "\n"
                "  onStepEnd(test: TestCase, _result: TestResult, step: TestStep): void {\n"
                '    if (step.category !== "test.step" || !step.error) return;\n'
                "    const scenario = test.annotations.find((a) => a.type === annotationType)?.description ?? \"\";\n"
                '    const message = String(step.error.message ?? "").split("\\n")[0].slice(0, 500);\n'
                "    if (this.failures.some((f) => f.scenario_id === scenario && f.step === step.title)) return;\n"
                "    this.failures.push({ scenario_id: scenario, step: step.title, message });\n"
                "  }\n"
                "\n"
                "  onEnd(result: FullResult): void {\n"
                '    const scenarioIds = result.status === "passed"\n'
                "      ? [...this.passedScenarioIds].sort()\n"
                "      : [];\n"
                "    const report = {\n"
                '      schema_version: "rich.acceptance-coverage/v1",\n'
                "      context: this.context,\n"
                "      scenario_ids: scenarioIds,\n"
                "    };\n"
                "    process.stdout.write(`${outputPrefix}${JSON.stringify(report)}\\n`);\n"
                '    if (result.status !== "passed" && this.failures.length > 0) {\n'
                "      // A second line, only on failure: which step failed, in the words the\n"
                "      // person approved. The coverage line above is what the engine trusts.\n"
                "      const failed = {\n"
                '        schema_version: "rich.acceptance-failures/v1",\n'
                "        context: this.context,\n"
                "        failures: this.failures.slice(0, 40),\n"
                "      };\n"
                "      process.stdout.write(`${failuresPrefix}${JSON.stringify(failed)}\\n`);\n"
                "    }\n"
                "  }\n"
                "}\n"
                "\n"
                "export default RichAcceptanceReporter;\n"
            ),
            "tests/unit/domain.test.ts": (
                'import { describe, expect, it } from "vitest";\n'
                'import { buildWelcomeMessage } from "../../packages/domain/src/index";\n'
                "\n"
                'describe("buildWelcomeMessage", () => {\n'
                '  it("normalizes an empty name", () => {\n'
                '    expect(buildWelcomeMessage("  ").message).toBe("Welcome, builder.");\n'
                "  });\n"
                "});\n"
            ),
            "tests/e2e/smoke.spec.ts": (
                'import { expect, test } from "@playwright/test";\n'
                "\n"
                'test("renders the generated application", async ({ page }) => {\n'
                '  await page.goto("/");\n'
                '  await expect(page.getByRole("heading", { level: 1 })).toContainText("'
                f"{project}"
                '");\n'
                '  await expect(page.getByRole("link", { name: "Health" })).toBeVisible();\n'
                "});\n"
            ),
        }

        web_dependencies = {
            packages["contracts"]: "workspace:*",
            packages["domain"]: "workspace:*",
            packages["ui"]: "workspace:*",
            "next": "16.2.12",
            "react": "19.2.8",
            "react-dom": "19.2.8",
        }
        if include_adapters:
            web_dependencies[packages["adapters"]] = "workspace:*"
        files.update(
            {
                "apps/web/package.json": _package_json(
                    web_package,
                    scripts={
                        "dev": "next dev",
                        "build": "next build --webpack",
                        "start": "next start",
                        "typecheck": "tsc --noEmit --incremental false",
                        "lint": "eslint . --max-warnings=0",
                    },
                    dependencies=web_dependencies,
                    dev_dependencies={
                        "@types/react": "19.2.17",
                        "@types/react-dom": "19.2.3",
                        "eslint": "9.39.5",
                        "eslint-config-next": "16.2.12",
                    },
                ),
                "apps/web/tsconfig.json": _json(
                    {
                        "extends": "../../tsconfig.base.json",
                        "compilerOptions": {
                            "jsx": "preserve",
                            "noEmit": True,
                            "incremental": True,
                            "plugins": [{"name": "next"}],
                            "paths": {"@/*": ["./src/*"]},
                        },
                        "include": [
                            "next-env.d.ts",
                            ".next/types/**/*.ts",
                            "src/**/*.ts",
                            "src/**/*.tsx",
                        ],
                        "exclude": ["node_modules"],
                    }
                ),
                "apps/web/next-env.d.ts": (
                    '/// <reference types="next" />\n'
                    '/// <reference types="next/image-types/global" />\n'
                    "\n"
                    "// This file is generated by Next.js and should not be edited.\n"
                ),
                "apps/web/next.config.mjs": (
                    'import { fileURLToPath } from "node:url";\n'
                    "\n"
                    "/** @type {import('next').NextConfig} */\n"
                    "const nextConfig = {\n"
                    "  reactStrictMode: true,\n"
                    "  poweredByHeader: false,\n"
                    '  outputFileTracingRoot: fileURLToPath(new URL(".", import.meta.url)),\n'
                    "  experimental: {\n"
                    "    cpus: 1,\n"
                    "    workerThreads: false,\n"
                    "    webpackBuildWorker: false,\n"
                    "  },\n"
                    '  allowedDevOrigins: ["127.0.0.1"],\n'
                    "  transpilePackages: "
                    f"{json.dumps([packages[kind] for kind in enabled_package_kinds])},\n"
                    "};\n"
                    "\n"
                    "export default nextConfig;\n"
                ),
                "apps/web/eslint.config.mjs": (
                    'import { defineConfig, globalIgnores } from "eslint/config";\n'
                    'import nextVitals from "eslint-config-next/core-web-vitals";\n'
                    'import nextTypescript from "eslint-config-next/typescript";\n'
                    "\n"
                    "export default defineConfig([\n"
                    "  ...nextVitals,\n"
                    "  ...nextTypescript,\n"
                    "  {\n"
                    "    rules: {\n"
                    "      // An underscore prefix is how TypeScript says a\n"
                    "      // parameter is deliberately unused -- satisfying an\n"
                    "      // interface it does not need every argument of. The\n"
                    "      // pinned operations interface makes that common, and\n"
                    "      // without this the verifier fails idiomatic code.\n"
                    '      "@typescript-eslint/no-unused-vars": [\n'
                    '        "warn",\n'
                    "        {\n"
                    '          argsIgnorePattern: "^_",\n'
                    '          varsIgnorePattern: "^_",\n'
                    '          caughtErrorsIgnorePattern: "^_",\n'
                    "        },\n"
                    "      ],\n"
                    "    },\n"
                    "  },\n"
                    '  globalIgnores([".next/**", "out/**", "build/**", "next-env.d.ts"]),\n'
                    "]);\n"
                ),
                "apps/web/src/app/layout.tsx": (
                    'import type { Metadata } from "next";\n'
                    'import type { ReactNode } from "react";\n'
                    'import "./globals.css";\n'
                    "\n"
                    "export const metadata: Metadata = {\n"
                    f'  title: "{project}",\n'
                    '  description: "Generated by RICH",\n'
                    "};\n"
                    "\n"
                    "export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {\n"
                    "  return (\n"
                    '    <html lang="en">\n'
                    "      <body>{children}</body>\n"
                    "    </html>\n"
                    "  );\n"
                    "}\n"
                ),
                "apps/web/src/app/page.tsx": (
                    f'import {{ buildWelcomeMessage }} from "{packages["domain"]}";\n'
                    f'import {{ ButtonLink }} from "{packages["ui"]}";\n'
                    "\n"
                    "export default function HomePage() {\n"
                    f'  const welcome = buildWelcomeMessage("{project}");\n'
                    "  return (\n"
                    '    <main className="shell">\n'
                    '      <p className="eyebrow">RICH target pack</p>\n'
                    f"      <h1>{project}</h1>\n"
                    "      <p>{welcome.message}</p>\n"
                    '      <ButtonLink href="/api/health">Health</ButtonLink>\n'
                    "    </main>\n"
                    "  );\n"
                    "}\n"
                ),
                "apps/web/src/app/globals.css": (
                    ":root { color-scheme: light; font-family: ui-sans-serif, system-ui, sans-serif; }\n"
                    "* { box-sizing: border-box; }\n"
                    "body { margin: 0; background: #f6f7fb; color: #172033; }\n"
                    ".shell { max-width: 54rem; margin: 0 auto; padding: 8rem 1.5rem; }\n"
                    ".eyebrow { color: #5b55d6; font-weight: 700; letter-spacing: .08em; "
                    "text-transform: uppercase; }\n"
                    "h1 { font-size: clamp(2.5rem, 7vw, 5rem); margin: .25rem 0 1rem; }\n"
                    ".button { display: inline-block; margin-top: 1rem; padding: .75rem 1rem; "
                    "border-radius: .6rem; background: #27235c; color: white; text-decoration: none; }\n"
                    ".button:focus-visible { outline: 3px solid #817af5; outline-offset: 3px; }\n"
                ),
                "apps/web/src/app/api/health/route.ts": (
                    f'import {{ healthResponseSchema }} from "{packages["contracts"]}";\n'
                    "\n"
                    "export function GET(): Response {\n"
                    '  return Response.json(healthResponseSchema.parse({ status: "ok" }));\n'
                    "}\n"
                ),
            }
        )

        files.update(
            {
                "packages/contracts/package.json": _package_json(
                    packages["contracts"],
                    scripts={"typecheck": "tsc --noEmit"},
                    dependencies={"zod": "4.4.3"},
                    extra={
                        "exports": {".": "./src/index.ts"},
                        "types": "./src/index.ts",
                    },
                ),
                "packages/contracts/tsconfig.json": _package_tsconfig(),
                "packages/contracts/src/index.ts": (
                    'import { z } from "zod";\n'
                    "\n"
                    "export const healthResponseSchema = z.object({\n"
                    '  status: z.literal("ok"),\n'
                    "});\n"
                    "\n"
                    "export type HealthResponse = z.infer<typeof healthResponseSchema>;\n"
                    + (
                        '\nexport * from "./product-intent";\n'
                        if has_approved_intent
                        else ""
                    )
                ),
                "packages/domain/package.json": _package_json(
                    packages["domain"],
                    scripts={"typecheck": "tsc --noEmit"},
                    dependencies=(
                        {packages["contracts"]: "workspace:*"}
                        if has_approved_intent
                        else None
                    ),
                    extra={
                        "exports": {".": "./src/index.ts"},
                        "types": "./src/index.ts",
                    },
                ),
                "packages/domain/tsconfig.json": _package_tsconfig(),
                "packages/domain/src/index.ts": (
                    "export interface WelcomeMessage {\n"
                    "  readonly message: string;\n"
                    "}\n"
                    "\n"
                    "export function buildWelcomeMessage(name: string): WelcomeMessage {\n"
                    '  const normalized = name.trim() || "builder";\n'
                    '  return { message: `Welcome, ${normalized}.` };\n'
                    "}\n"
                    + (
                        '\nexport * from "./product-intent";\n'
                        if has_approved_intent
                        else ""
                    )
                ),
                "packages/adapters/package.json": _package_json(
                    packages["adapters"],
                    scripts={"typecheck": "tsc --noEmit"},
                    extra={
                        "exports": {".": "./src/index.ts"},
                        "types": "./src/index.ts",
                    },
                ),
                "packages/adapters/tsconfig.json": _package_tsconfig(),
                "packages/adapters/src/index.ts": (
                    "export interface Clock {\n"
                    "  now(): Date;\n"
                    "}\n"
                    "\n"
                    "export class SystemClock implements Clock {\n"
                    "  now(): Date {\n"
                    "    return new Date();\n"
                    "  }\n"
                    "}\n"
                ),
                "packages/ui/package.json": _package_json(
                    packages["ui"],
                    scripts={"typecheck": "tsc --noEmit"},
                    peer_dependencies={"react": "19.2.8"},
                    dev_dependencies={"@types/react": "19.2.17"},
                    extra={
                        "exports": {".": "./src/index.ts"},
                        "types": "./src/index.ts",
                    },
                ),
                "packages/ui/tsconfig.json": _package_tsconfig(jsx=True),
                "packages/ui/src/index.ts": 'export { ButtonLink } from "./button";\n',
                "packages/ui/src/button.tsx": (
                    'import type { ReactNode } from "react";\n'
                    "\n"
                    "export interface ButtonLinkProps {\n"
                    "  readonly href: string;\n"
                    "  readonly children: ReactNode;\n"
                    "}\n"
                    "\n"
                    "export function ButtonLink({ href, children }: ButtonLinkProps) {\n"
                    '  return <a className="button" href={href}>{children}</a>;\n'
                    "}\n"
                ),
            }
        )

        files.update(
            {
                "packages/db/package.json": _package_json(
                    packages["db"],
                    scripts={
                        "typecheck": "tsc --noEmit",
                        "db:generate": "drizzle-kit generate",
                        "db:migrate": "tsx src/migrate.ts",
                        "db:seed": "tsx src/seed.ts",
                    },
                    dependencies={
                        "drizzle-orm": "0.45.2",
                        "postgres": "3.4.9",
                    },
                    dev_dependencies={
                        "drizzle-kit": "0.31.10",
                        "tsx": "4.23.1",
                    },
                    extra={
                        "exports": {
                            ".": "./src/index.ts",
                            "./schema": "./src/schema.ts",
                        },
                        "types": "./src/index.ts",
                    },
                ),
                "packages/db/tsconfig.json": _package_tsconfig(),
                "packages/db/drizzle.config.ts": (
                    'import { defineConfig } from "drizzle-kit";\n'
                    "\n"
                    "export default defineConfig({\n"
                    '  dialect: "postgresql",\n'
                    '  schema: "./src/schema.ts",\n'
                    '  out: "./migrations",\n'
                    "  dbCredentials: {\n"
                    "    url: process.env.DATABASE_URL ?? "
                    f'"postgresql://rich:rich@localhost:5432/{project}",\n'
                    "  },\n"
                    "  strict: true,\n"
                    "});\n"
                ),
                "packages/db/src/schema.ts": (
                    'import { pgTable, text, timestamp, uuid } from "drizzle-orm/pg-core";\n'
                    "\n"
                    'export const projects = pgTable("projects", {\n'
                    '  id: uuid("id").primaryKey().defaultRandom(),\n'
                    '  name: text("name").notNull(),\n'
                    '  createdAt: timestamp("created_at", { withTimezone: true })\n'
                    "    .notNull()\n"
                    "    .defaultNow(),\n"
                    "});\n"
                ),
                "packages/db/src/index.ts": (
                    'import { drizzle } from "drizzle-orm/postgres-js";\n'
                    'import postgres from "postgres";\n'
                    'import * as schema from "./schema";\n'
                    "\n"
                    "export function createDatabase(url: string) {\n"
                    "  if (!url) throw new Error(\"DATABASE_URL is required\");\n"
                    "  const client = postgres(url, { max: 5 });\n"
                    "  return { db: drizzle(client, { schema }), close: () => client.end() };\n"
                    "}\n"
                    "\n"
                    'export * from "./schema";\n'
                ),
                "packages/db/src/migrate.ts": (
                    'import { migrate } from "drizzle-orm/postgres-js/migrator";\n'
                    'import { createDatabase } from "./index";\n'
                    "\n"
                    "const url = process.env.DATABASE_URL;\n"
                    'if (!url) throw new Error("DATABASE_URL is required");\n'
                    "const connection = createDatabase(url);\n"
                    'await migrate(connection.db, { migrationsFolder: "migrations" });\n'
                    "await connection.close();\n"
                ),
                "packages/db/src/seed.ts": (
                    'import { createDatabase, projects } from "./index";\n'
                    "\n"
                    "const url = process.env.DATABASE_URL;\n"
                    'if (!url) throw new Error("DATABASE_URL is required");\n'
                    "const connection = createDatabase(url);\n"
                    f'await connection.db.insert(projects).values({{ name: "{project}" }});\n'
                    "await connection.close();\n"
                ),
                "packages/db/migrations/0000_initial.sql": (
                    'CREATE EXTENSION IF NOT EXISTS "pgcrypto";\n'
                    "--> statement-breakpoint\n"
                    'CREATE TABLE IF NOT EXISTS "projects" (\n'
                    '  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,\n'
                    '  "name" text NOT NULL,\n'
                    '  "created_at" timestamp with time zone DEFAULT now() NOT NULL\n'
                    ");\n"
                ),
                "packages/db/migrations/meta/_journal.json": _json(
                    {
                        "version": "7",
                        "dialect": "postgresql",
                        "entries": [
                            {
                                "idx": 0,
                                "version": "7",
                                "when": 0,
                                "tag": "0000_initial",
                                "breakpoints": True,
                            }
                        ],
                    }
                ),
            }
        )

        if not include_data:
            files = {
                path: content
                for path, content in files.items()
                if not path.startswith("packages/db/")
            }
            files.pop("docker-compose.yml", None)
            files[".env.example"] = f"NEXT_PUBLIC_APP_NAME={project}\n"
            files["README.md"] = (
                f"# {project}\n"
                "\n"
                "A RICH-generated Next.js App Router monorepo.\n"
                "\n"
                "## Local development\n"
                "\n"
                "1. Copy `.env.example` to `.env`.\n"
                "2. Run `corepack enable && pnpm install --frozen-lockfile "
                "--ignore-scripts`.\n"
                "3. Run `pnpm dev`.\n"
                "\n"
                "Run the complete local verification pipeline with `pnpm ci`.\n"
                "Dependency changes must update and commit `pnpm-lock.yaml`.\n"
            )
        if not include_adapters:
            files = {
                path: content
                for path, content in files.items()
                if not path.startswith("packages/adapters/")
            }
        if self.config.project_spec is not None and architecture is not None:
            files.update(
                _intent_files(
                    self.config.project_spec,
                    architecture,
                    packages,
                    project,
                )
            )
        if architecture is not None:
            files.update(_property_files(architecture))

        rendered: dict[str, bytes] = {}
        for path, content in files.items():
            safe_path = str(_safe_generated_path(path))
            if safe_path == _MANIFEST_PATH:
                raise TargetPackError("templates may not replace the target-pack manifest")
            rendered[safe_path] = content.encode("utf-8")
        if self.config.project_spec is not None and architecture is not None:
            _validate_rendered_path_ownership(
                rendered,
                project=self.config.project_spec,
                architecture=architecture,
            )
        return dict(sorted(rendered.items()))

    def manifest(self, files: Mapping[str, bytes] | None = None) -> ScaffoldManifest:
        rendered = dict(self.render_files() if files is None else files)
        records = tuple(
            ManifestFile(
                path=path,
                size=len(content),
                sha256=f"sha256:{hashlib.sha256(content).hexdigest()}",
                mutable=path in _MUTABLE_GENERATED_FILES,
                managed_by=_MUTABLE_GENERATED_FILES.get(path),
            )
            for path, content in sorted(rendered.items())
        )
        digest = hashlib.sha256()
        for record in records:
            digest.update(record.path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(record.sha256.encode("ascii"))
            digest.update(b"\0")
            digest.update(str(record.size).encode("ascii"))
            digest.update(b"\0")
            digest.update(b"mutable" if record.mutable else b"immutable")
            digest.update(b"\0")
            digest.update((record.managed_by or "").encode("utf-8"))
            digest.update(b"\n")
        return ScaffoldManifest(
            project_name=self.config.project_name,
            package_scope=self.config.scope,
            content_digest=f"sha256:{digest.hexdigest()}",
            files=records,
            target_pack_version=self.target_pack_version,
        )

    def scaffold(self, destination: str | Path) -> ScaffoldManifest:
        """Atomically create the scaffold in an absent or empty destination."""

        destination_path = Path(destination).absolute()
        if os.path.lexists(destination_path) and destination_path.is_symlink():
            raise DestinationNotEmptyError("destination must not be a symbolic link")
        if destination_path.exists():
            if not destination_path.is_dir():
                raise DestinationNotEmptyError("destination is not a directory")
            if any(destination_path.iterdir()):
                raise DestinationNotEmptyError(
                    "destination contains unmanaged files; refusing to overwrite it"
                )

        parent = destination_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(
            tempfile.mkdtemp(
                prefix=f".{destination_path.name}.rich-staging-",
                dir=parent,
            )
        )
        try:
            rendered = self.render_files()
            manifest = self.manifest(rendered)
            for relative, content in rendered.items():
                _atomic_write(stage.joinpath(*PurePosixPath(relative).parts), content)
            _atomic_write(
                stage.joinpath(*PurePosixPath(_MANIFEST_PATH).parts),
                manifest.to_json().encode("utf-8"),
            )

            if os.path.lexists(destination_path):
                if destination_path.is_symlink() or not destination_path.is_dir():
                    raise DestinationNotEmptyError(
                        "destination changed while the scaffold was being rendered"
                    )
                if any(destination_path.iterdir()):
                    raise DestinationNotEmptyError(
                        "destination gained unmanaged files; refusing to overwrite it"
                    )
            os.replace(stage, destination_path)
            return manifest
        except BaseException:
            if stage.exists():
                shutil.rmtree(stage)
            raise


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    except BaseException:
        if temporary_path.exists():
            temporary_path.unlink()
        raise
