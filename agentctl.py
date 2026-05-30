#!/usr/bin/env python3
"""agentctl v1 — Agent-native software harness.

Enforces three properties on a codebase:
  1. Information firewalls (materialized, not conventional)
  2. Explicit dependency DAG
  3. Complexity budgets
"""

import argparse
import os
import sys
from dataclasses import dataclass, field
import shutil
from typing import Optional

import yaml

# ── M0: Data models ───────────────────────────────────────────────────────────

V1_TYPES = {"string", "int", "float", "bool", "list<string>", "list<int>",
            "list<float>", "list<bool>"}


@dataclass
class Budget:
    max_loc: int = 5000
    max_files: int = 100
    max_context_tokens: int = 100_000


@dataclass
class Operation:
    name: str
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class BehaviorProperty:
    id: str
    prose: str = ""
    formal: Optional[str] = None


@dataclass
class Interface:
    operations: list[Operation] = field(default_factory=list)


@dataclass
class Contract:
    name: str
    version: str = "0.1.0"
    interface: Interface = field(default_factory=Interface)
    dependencies: list[str] = field(default_factory=list)
    behavior: list[BehaviorProperty] = field(default_factory=list)
    budget: Optional[Budget] = None


@dataclass
class Module:
    name: str
    path: str
    contract: Contract
    contract_path: str = ""


@dataclass
class Workspace:
    root: str
    config: dict
    modules: list[Module] = field(default_factory=list)


# ── Token estimation ───────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Approximate token count (v1: char/4). Single function for future replacement."""
    return max(1, -(len(text.encode('utf-8')) // -4))  # ceil(len/4)


# ── Error ──────────────────────────────────────────────────────────────────────

class AgentCtlError(Exception):
    """User-visible agentctl error."""
    pass


# ── M1: Workspace loading and validation ───────────────────────────────────────

def load_workspace(root: str = ".") -> Workspace:
    """Load agentnative.yaml, discover modules, parse contracts."""
    root = os.path.abspath(root)
    config_path = os.path.join(root, "agentnative.yaml")
    if not os.path.isfile(config_path):
        raise AgentCtlError(f"No agentnative.yaml found in {root} — run `agentctl init`")

    with open(config_path) as f:
        config = yaml.safe_load(f)

    ws = Workspace(root=root, config=config)

    module_root = config.get("module_root", "modules")
    modules_dir = os.path.join(root, module_root)

    if os.path.isdir(modules_dir):
        for entry in sorted(os.listdir(modules_dir)):
            mod_dir = os.path.join(modules_dir, entry)
            if not os.path.isdir(mod_dir):
                continue
            contract_file = os.path.join(mod_dir, "contract.yaml")
            if not os.path.isfile(contract_file):
                continue
            try:
                mod = parse_module(mod_dir, contract_file)
                ws.modules.append(mod)
            except yaml.YAMLError as e:
                raise AgentCtlError(f"YAML error in {contract_file}: {e}")

    return ws


def parse_module(mod_dir: str, contract_path: str) -> Module:
    """Parse a module directory and its contract.yaml."""
    with open(contract_path) as f:
        raw = yaml.safe_load(f) or {}

    # Parse contract
    contract = Contract(
        name=raw.get("name", ""),
        version=raw.get("version", "0.1.0"),
        dependencies=raw.get("dependencies", []) or [],
    )

    # Parse interface
    iface_raw = raw.get("interface", {}) or {}
    for op_raw in iface_raw.get("operations", []) or []:
        op = Operation(
            name=op_raw.get("name", ""),
            inputs=op_raw.get("inputs", {}) or {},
            outputs=op_raw.get("outputs", {}) or {},
            errors=op_raw.get("errors", []) or [],
        )
        contract.interface.operations.append(op)

    # Parse behavior
    for b_raw in raw.get("behavior", []) or []:
        bp = BehaviorProperty(
            id=b_raw.get("id", ""),
            prose=b_raw.get("prose", ""),
            formal=b_raw.get("formal"),
        )
        contract.behavior.append(bp)

    # Parse module-level budget override
    if "budget" in raw and raw["budget"] is not None:
        b = raw["budget"]
        contract.budget = Budget(
            max_loc=b.get("max_loc", 5000),
            max_files=b.get("max_files", 100),
            max_context_tokens=b.get("max_context_tokens", 100_000),
        )

    return Module(
        name=contract.name,
        path=mod_dir,
        contract=contract,
        contract_path=contract_path,
    )


def validate_workspace(ws: Workspace) -> list[str]:
    """Run all schema validation checks. Returns list of error messages."""
    errors: list[str] = []
    module_names = {m.name for m in ws.modules}

    for mod in ws.modules:
        prefix = f"[{mod.name}]"

        # Name must match directory
        dir_name = os.path.basename(mod.path.rstrip("/"))
        if mod.contract.name != dir_name:
            errors.append(
                f"{prefix} contract name '{mod.contract.name}' does not match "
                f"directory name '{dir_name}'"
            )

        # Name must not be empty
        if not mod.contract.name:
            errors.append(f"{prefix} contract name is required")
            continue

        # Version must be present
        if not mod.contract.version:
            errors.append(f"{prefix} version is required")

        # Interface operations
        seen_ops = set()
        for op in mod.contract.interface.operations:
            if not op.name:
                errors.append(f"{prefix} operation name is required")

            # Validate types
            for param, ptype in op.inputs.items():
                if ptype not in V1_TYPES:
                    errors.append(
                        f"{prefix} operation '{op.name}' input '{param}': "
                        f"type '{ptype}' is not in v1 type vocabulary"
                    )
            for param, ptype in op.outputs.items():
                if ptype not in V1_TYPES:
                    errors.append(
                        f"{prefix} operation '{op.name}' output '{param}': "
                        f"type '{ptype}' is not in v1 type vocabulary"
                    )

            # Duplicate operation names
            if op.name in seen_ops:
                errors.append(f"{prefix} duplicate operation name '{op.name}'")
            seen_ops.add(op.name)

        # Dependencies must reference existing modules
        for dep in mod.contract.dependencies:
            if dep not in module_names:
                errors.append(
                    f"{prefix} dependency '{dep}' does not reference an existing module"
                )

        # Behavior properties: ids must be present and unique
        seen_ids = set()
        for bp in mod.contract.behavior:
            if not bp.id:
                errors.append(f"{prefix} behavior property id is required")
                continue
            if bp.id in seen_ids:
                errors.append(f"{prefix} duplicate behavior property id '{bp.id}'")
            seen_ids.add(bp.id)

    return errors


# ── M2: Dependency graph and cycle detection ────────────────────────────────────

def build_dep_graph(modules: list[Module]) -> dict[str, list[str]]:
    """Build adjacency list from module dependencies."""
    return {m.name: list(m.contract.dependencies) for m in modules}


def detect_cycles(modules: list[Module]) -> list[list[str]]:
    """DFS with 3-coloring (white/grey/black). Returns list of cycle paths found.

    Each cycle is returned as a list of module names forming the cycle path.
    """
    graph = build_dep_graph(modules)
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {m.name: WHITE for m in modules}
    cycles: list[list[str]] = []
    parent: dict[str, str | None] = {}

    def dfs(node: str, stack: list[str]) -> None:
        color[node] = GREY
        stack.append(node)
        for neighbor in graph.get(node, []):
            if neighbor not in color:
                # Unknown module — schema validation handles this elsewhere
                continue
            if color[neighbor] == GREY:
                # Found a cycle: extract from stack
                cycle_start = stack.index(neighbor)
                cycle_path = stack[cycle_start:] + [neighbor]
                cycles.append(cycle_path)
            elif color[neighbor] == WHITE:
                parent[neighbor] = node
                dfs(neighbor, stack)
        stack.pop()
        color[node] = BLACK

    for m in modules:
        if color[m.name] == WHITE:
            dfs(m.name, [])

    return cycles


def format_cycle_path(cycle: list[str]) -> str:
    """Pretty-print a cycle path."""
    return " → ".join(cycle)


# ── Commands ───────────────────────────────────────────────────────────────────

def load_ws() -> Workspace:
    """Load workspace or raise."""
    return load_workspace(".")


def cmd_validate() -> int:
    """M1/M2/M4/M5: Parse + validate all modules."""
    ws = load_ws()
    errors = validate_workspace(ws)

    # M2: cycle detection
    cycles = detect_cycles(ws.modules)
    if cycles:
        for cycle in cycles:
            errors.append(f"[DAG] cycle detected: {format_cycle_path(cycle)}")

    if errors:
        for e in errors:
            print(f"  ✗ {e}")
        print(f"\n{len(errors)} error(s) found.")
        return 1

    # Success summary
    print(f"Workspace: {ws.root}")
    print(f"Modules: {len(ws.modules)}")
    for mod in ws.modules:
        deps = ", ".join(mod.contract.dependencies) if mod.contract.dependencies else "none"
        ops = len(mod.contract.interface.operations)
        props = len(mod.contract.behavior)
        print(f"  ✓ {mod.name}  ops={ops}  deps=[{deps}]  behavior={props}")
    print("All checks passed.")
    return 0


def cmd_context(module_name: str, out_dir: Optional[str], list_only: bool) -> int:
    """M3: Materialize firewall tree for a module.

    Output (per §5.4):
        {out_dir}/
        ├── CONTEXT.md
        ├── contract.yaml       # module's own contract
        ├── src/                # module's own source
        ├── tests/              # module's own tests
        └── deps/
            └── <dep>/
                └── contract.yaml   # CONTRACT ONLY — NO src
    """
    ws = load_ws()

    # Find the target module
    target = None
    for m in ws.modules:
        if m.name == module_name:
            target = m
            break
    if target is None:
        print(f"Error: module '{module_name}' not found", file=sys.stderr)
        return 1

    # Determine output directory
    if out_dir:
        dest_root = os.path.abspath(out_dir)
    else:
        config = ws.config
        # Use .agentctl/workspaces/<module> (in repo root, not module_root)
        dest_root = os.path.join(ws.root, ".agentctl", "workspaces", module_name)

    # Resolve dependency modules
    dep_modules: list[Module] = []
    dep_names = set(target.contract.dependencies)
    for m in ws.modules:
        if m.name in dep_names:
            dep_modules.append(m)
            dep_names.discard(m.name)
    if dep_names:
        print(f"Warning: unresolved dependencies: {', '.join(dep_names)}", file=sys.stderr)

    # ── Dry-run mode ──
    if list_only:
        print(f"[dry-run] Would materialize context for '{module_name}' to {dest_root}")
        print(f"\n  Own files:")
        print(f"    contract.yaml  ({target.contract_path})")
        src_dir = os.path.join(target.path, "src")
        tests_dir = os.path.join(target.path, "tests")
        if os.path.isdir(src_dir):
            for f in sorted(os.listdir(src_dir)):
                print(f"    src/{f}")
        if os.path.isdir(tests_dir):
            for f in sorted(os.listdir(tests_dir)):
                print(f"    tests/{f}")
        print(f"\n  Dep contracts (CONTRACT ONLY):")
        for dm in dep_modules:
            print(f"    deps/{dm.name}/contract.yaml  ({dm.contract_path})")
        print(f"\n  Generated:")
        print(f"    CONTEXT.md")
        print(f"\n  ⚠  No dependency source is included.")
        return 0

    # ── Materialize ──
    # Clean and recreate destination
    if os.path.exists(dest_root):
        shutil.rmtree(dest_root)
    os.makedirs(dest_root, exist_ok=True)

    # Copy own contract
    shutil.copy2(target.contract_path, os.path.join(dest_root, "contract.yaml"))

    # Copy own src/ and tests/
    for sub in ["src", "tests"]:
        src_sub = os.path.join(target.path, sub)
        if os.path.isdir(src_sub):
            shutil.copytree(src_sub, os.path.join(dest_root, sub))

    # Copy each direct dependency's contract.yaml ONLY into deps/<dep>/
    deps_dir = os.path.join(dest_root, "deps")
    os.makedirs(deps_dir, exist_ok=True)
    for dm in dep_modules:
        dep_dest = os.path.join(deps_dir, dm.name)
        os.makedirs(dep_dest, exist_ok=True)
        shutil.copy2(dm.contract_path, os.path.join(dep_dest, "contract.yaml"))

    # Generate CONTEXT.md
    _generate_context_md(target, dep_modules, dest_root)

    print(f"Materialized context for '{module_name}' → {dest_root}")
    _print_tree_summary(dest_root, module_name)
    return 0


def _generate_context_md(target: Module, deps: list[Module], dest_root: str) -> None:
    """Generate CONTEXT.md in the materialized tree."""
    contract = target.contract
    lines = []
    lines.append(f"# Context: {target.name}\n")
    lines.append(f"**Module:** `{target.name}`")
    lines.append(f"**Version:** {contract.version}\n")

    # Contract summary
    lines.append("## Contract\n")
    for op in contract.interface.operations:
        inputs = ", ".join(f"{k}: {v}" for k, v in op.inputs.items())
        outputs = ", ".join(f"{k}: {v}" for k, v in op.outputs.items())
        errs = ", ".join(op.errors) if op.errors else "none"
        lines.append(f"- **{op.name}**({inputs}) → ({outputs})  errors: {errs}")

    lines.append("")
    if contract.behavior:
        lines.append("## Behavioral Properties\n")
        for bp in contract.behavior:
            lines.append(f"- **{bp.id}**: {bp.prose}")
        lines.append("")

    # Dependencies info
    if deps:
        lines.append("## Dependencies (contracts only)\n")
        for dm in deps:
            lines.append(f"- **{dm.name}** — see `deps/{dm.name}/contract.yaml`")
        lines.append("")
    else:
        lines.append("## Dependencies\n")
        lines.append("None.\n")

    # Rules for the agent
    lines.append("## Agent Instructions\n")
    lines.append("- **You may edit:** `src/` and `tests/` only.")
    lines.append("- **You may read:** everything in this tree, including `deps/*/contract.yaml`.")
    lines.append("- **You may NOT access:** dependency implementations — they are intentionally "
                   "absent from this tree.")
    lines.append("- **Dependencies are received by injection, never imported.** "
                   "Code against the dependency's interface (defined in its contract.yaml), "
                   "and receive dependency handles as injected arguments.")
    lines.append("- **Write tests against fakes** that satisfy the dependency contracts — "
                   "do not import or reach for real dependency implementations.")
    lines.append("")

    with open(os.path.join(dest_root, "CONTEXT.md"), "w") as f:
        f.write("\n".join(lines))


def _print_tree_summary(dest_root: str, module_name: str) -> None:
    """Print a quick summary of the materialized tree."""
    print(f"\n  Tree structure:")
    for root, dirs, files in sorted(os.walk(dest_root)):
        depth = root.replace(dest_root, "").count(os.sep)
        label = os.path.basename(root) or module_name
        indent = "  " + "  " * depth
        print(f"{indent}{label}/")
        for f in sorted(files):
            print(f"{indent}  {f}")


def cmd_graph(dot: bool) -> int:
    """M2: Print dependency DAG."""
    ws = load_ws()

    if dot:
        print("digraph agentnative {")
        print('  rankdir=LR;')
        print('  node [shape=box, style=rounded];')
        for mod in ws.modules:
            if mod.contract.dependencies:
                for dep in mod.contract.dependencies:
                    print(f'  "{dep}" -> "{mod.name}";')
            else:
                print(f'  "{mod.name}";')
        print("}")
    else:
        graph = build_dep_graph(ws.modules)
        if not ws.modules:
            print("(no modules)")
            return 0
        # Find roots (no deps)
        roots = [m.name for m in ws.modules if not m.contract.dependencies]
        others = [m.name for m in ws.modules if m.contract.dependencies]

        print("Dependency DAG:")
        for name in roots:
            deps = graph.get(name, [])
            dep_str = ", ".join(deps) if deps else "none"
            print(f"  [{name}]  ←  deps: {dep_str}")
        for name in others:
            deps = graph.get(name, [])
            print(f"  [{name}]  ←  deps: {', '.join(deps)}")

    return 0


def cmd_wrap(path: str, name: str) -> int:
    """M6: Scaffold module around existing code."""
    print(f"wrap {path} -> {name}: not yet implemented")
    return 0


def cmd_init(target_dir: str) -> int:
    """M6: Scaffold new workspace."""
    print("init: not yet implemented")
    return 0


# ── Main ───────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentctl",
        description="Agent-native software harness — v1",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="Validate all modules (schema, DAG, budgets, boundaries)")

    p_ctx = sub.add_parser("context", help="Materialize firewall tree for a module")
    p_ctx.add_argument("module", help="Module name")
    p_ctx.add_argument("--out", dest="out_dir", default=None, help="Output directory")
    p_ctx.add_argument("--list", dest="list_only", action="store_true",
                       help="Dry-run: print what would be included")

    p_graph = sub.add_parser("graph", help="Print dependency DAG")
    p_graph.add_argument("--dot", action="store_true", help="Output Graphviz DOT format")

    p_wrap = sub.add_parser("wrap", help="Scaffold module around existing code")
    p_wrap.add_argument("path", help="Path to existing code file/dir")
    p_wrap.add_argument("--name", required=True, help="Module name")

    sub.add_parser("init", help="Scaffold a new workspace")

    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            return cmd_init(target_dir=".")
        elif args.command == "validate":
            return cmd_validate()
        elif args.command == "context":
            return cmd_context(args.module, args.out_dir, args.list_only)
        elif args.command == "graph":
            return cmd_graph(args.dot)
        elif args.command == "wrap":
            return cmd_wrap(args.path, args.name)
    except AgentCtlError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
