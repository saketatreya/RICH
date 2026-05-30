#!/usr/bin/env python3
"""agentctl v1 — Agent-native software harness.

Enforces three properties on a codebase:
  1. Information firewalls (materialized, not conventional)
  2. Explicit dependency DAG
  3. Complexity budgets
"""

import argparse
import sys
from dataclasses import dataclass, field
from typing import Optional

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
    path: str                     # filesystem path to the module directory
    contract: Contract
    contract_path: str = ""       # path to contract.yaml


@dataclass
class Workspace:
    root: str
    config: dict
    modules: list[Module] = field(default_factory=list)


# ── Token estimation ───────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Approximate token count (v1: char/4). Single function for future replacement."""
    return max(1, -(len(text.encode('utf-8')) // -4))  # ceil(len/4)


# ── CLI skeleton ───────────────────────────────────────────────────────────────

class AgentCtlError(Exception):
    """User-visible agentctl error."""
    pass


def cmd_validate(ws: Workspace) -> int:
    """M1: Parse + validate all modules."""
    print("validate: not yet implemented")
    return 0


def cmd_context(ws: Workspace, module_name: str, out_dir: Optional[str], list_only: bool) -> int:
    """M3: Materialize firewall tree."""
    print(f"context {module_name}: not yet implemented")
    return 0


def cmd_graph(ws: Workspace, dot: bool) -> int:
    """M2: Print dependency DAG."""
    print("graph: not yet implemented")
    return 0


def cmd_wrap(ws: Workspace, path: str, name: str) -> int:
    """M6: Scaffold module around existing code."""
    print(f"wrap {path} -> {name}: not yet implemented")
    return 0


def cmd_init(target_dir: str) -> int:
    """M6: Scaffold new workspace."""
    print("init: not yet implemented")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentctl",
        description="Agent-native software harness — v1",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # validate
    sub.add_parser("validate", help="Validate all modules (schema, DAG, budgets, boundaries)")

    # context
    p_ctx = sub.add_parser("context", help="Materialize firewall tree for a module")
    p_ctx.add_argument("module", help="Module name")
    p_ctx.add_argument("--out", dest="out_dir", default=None, help="Output directory")
    p_ctx.add_argument("--list", dest="list_only", action="store_true",
                       help="Dry-run: print what would be included")

    # graph
    p_graph = sub.add_parser("graph", help="Print dependency DAG")
    p_graph.add_argument("--dot", action="store_true", help="Output Graphviz DOT format")

    # wrap
    p_wrap = sub.add_parser("wrap", help="Scaffold module around existing code")
    p_wrap.add_argument("path", help="Path to existing code file/dir")
    p_wrap.add_argument("--name", required=True, help="Module name")

    # init
    sub.add_parser("init", help="Scaffold a new workspace")

    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            return cmd_init(target_dir=".")
        elif args.command == "context":
            return cmd_context(None, args.module, args.out_dir, args.list_only)
        elif args.command == "validate":
            return cmd_validate(None)
        elif args.command == "graph":
            return cmd_graph(None, args.dot)
        elif args.command == "wrap":
            return cmd_wrap(None, args.path, args.name)
    except AgentCtlError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
