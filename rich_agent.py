#!/usr/bin/env python3
"""rich-agent — Spawn Hermes sub-agents bounded to modules by physical firewall.

Usage:
    rich-agent auth "Add OAuth flow"
    rich-agent --all "Implement per contract"
    
Each agent gets a materialized workspace tree where dependency source
doesn't exist. Agents run in parallel. No API key needed — uses Hermes.
"""

import sys
import os
import subprocess
import json

WORKSPACE = os.path.dirname(os.path.abspath(__file__))


def materialize(module_name):
    """Materialize the firewall tree for a module."""
    subprocess.run(
        [sys.executable, "rich.py", "context", module_name],
        cwd=WORKSPACE, capture_output=True,
    )


def get_tree_path(module_name):
    return os.path.join(WORKSPACE, ".agentctl", "workspaces", module_name)


def build_prompt(module_name, task):
    """Build the prompt for a bounded agent."""
    tree = get_tree_path(module_name)
    return (
        f"You are working on the '{module_name}' module inside a FIREWALLED workspace "
        f"at {tree}/.\n\n"
        f"This tree contains ONLY:\n"
        f"  - Your contract (contract.yaml)\n"
        f"  - Your source code (src/)\n"
        f"  - Your tests (tests/)\n"
        f"  - Dependency CONTRACTS only (deps/*/contract.yaml)\n\n"
        f"Dependency source code DOES NOT EXIST in this tree. You CANNOT read it.\n"
        f"The contract files are your complete interface to dependencies.\n\n"
        f"Your task: {task}\n\n"
        f"Start by reading CONTEXT.md, then the contract, then the source."
    )


def run_one(module_name, task):
    """Run one agent using Hermes delegate_task via stdin JSON."""
    materialize(module_name)
    prompt = build_prompt(module_name, task)

    # Build delegate_task JSON
    request = {
        "goal": prompt[:2000],  # Truncate for the goal field
        "toolsets": ["terminal", "file"],
        "context": f"Workspace: {get_tree_path(module_name)}",
    }
    return request


def run_all(task):
    """Build requests for all modules."""
    modules_dir = os.path.join(WORKSPACE, "modules")
    requests = []
    for name in sorted(os.listdir(modules_dir)):
        mod_dir = os.path.join(modules_dir, name)
        if os.path.isdir(mod_dir) and os.path.isfile(os.path.join(mod_dir, "contract.yaml")):
            requests.append(run_one(name, task))
    return requests


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: rich-agent <module> <task>")
        print("       rich-agent --all <task>")
        sys.exit(1)

    if sys.argv[1] == "--all":
        task = " ".join(sys.argv[2:]) or "Implement per contract"
        reqs = run_all(task)
        print(json.dumps({"tasks": reqs}, indent=2))
        print(f"\n{len(reqs)} agent requests ready. Run via Hermes delegate_task.")
    else:
        module = sys.argv[1]
        task = " ".join(sys.argv[2:]) or "Implement per contract"
        req = run_one(module, task)
        print(json.dumps(req, indent=2))
        print(f"\nAgent request for '{module}' ready.")
