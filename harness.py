#!/usr/bin/env python3
"""harness.py — Runtime enforcement for agent-native software architecture.

This is the ACTUAL harness: it sits between an agent and its tools,
enforcing the information firewall on every operation.

While rich.py does static validation (schema, DAG, budgets),
harness.py does runtime enforcement (every read/write/search mediated).

Usage:
    from harness import Harness

    h = Harness(".")
    session = h.session("auth")

    # These work — agent is within its boundary:
    session.read_file("modules/auth/src/auth.py")
    session.write_file("modules/auth/src/auth.py", "def authenticate(...")

    # These are BLOCKED — agent crossed the firewall:
    session.read_file("modules/token_store/src/token_store.py")
    # → FirewallBlocked: path outside module boundary
"""

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

# Import the core library — everything we already built
from rich import (
    load_workspace, parse_module,
    build_dep_graph,
    get_effective_budget, check_module_budget, estimate_tokens,
    check_module_boundaries,
    _generate_context_md,
    V1_TYPES,
    Contract, Module, Budget, BehaviorProperty,
    Workspace,
)


# ── Exceptions ─────────────────────────────────────────────────────────────────

class FirewallBlocked(Exception):
    """Raised when the agent attempts an operation outside its module boundary."""
    def __init__(self, operation: str, detail: str):
        self.operation = operation
        self.detail = detail
        super().__init__(f"[FIREWALL] {operation}: {detail}")


class BudgetWarning(Exception):
    """Raised when the agent approaches or exceeds its complexity budget."""
    def __init__(self, metric: str, used: int, limit: int):
        self.metric = metric
        self.used = used
        self.limit = limit
        super().__init__(f"[BUDGET] {metric}: {used}/{limit} ({100*used//limit}%)")


# ── ModuleSession — the actual harness ─────────────────────────────────────────

@dataclass
class SessionStats:
    """Track budget usage during a harness session."""
    loc_added: int = 0
    files_created: int = 0
    tokens_written: int = 0
    operations_blocked: int = 0
    operations_allowed: int = 0


class ModuleSession:
    """A harness session for one agent working on exactly one module.

    The firewall is enforced on every operation. The agent:
    - MAY read: own contract + own src/ + own tests/ + dep contracts
    - MAY write: own src/ + own tests/ (with import scanning)
    - MAY search: scoped to whitelisted directories
    - MAY NOT: read dependency source, sibling source, or anything outside bounds
    - MAY NOT: import undeclared dependencies
    """

    def __init__(self, workspace_root: str, module_name: str):
        self.workspace_root = os.path.abspath(workspace_root)
        self.module_name = module_name
        self.stats = SessionStats()

        # Load workspace
        self.ws = load_workspace(workspace_root)

        # Find our module
        mod = None
        for m in self.ws.modules:
            if m.name == module_name:
                mod = m
                break
        if mod is None:
            raise FirewallBlocked("session_init", f"module '{module_name}' not found in workspace")
        self.module: Module = mod  # guaranteed set after the check above

        # Resolve dependencies
        self.deps: list[Module] = []
        dep_names = set(self.module.contract.dependencies)
        for m in self.ws.modules:
            if m.name in dep_names:
                self.deps.append(m)
                dep_names.discard(m.name)

        if dep_names:
            print(f"⚠  Warning: unresolved dependencies: {', '.join(dep_names)}", file=sys.stderr)

        # Build the DAG
        self.graph = build_dep_graph(self.ws.modules)

        # Effective budget
        self.budget = get_effective_budget(self.module, self.ws)

        # ── Build whitelists ──
        self._build_whitelists()

        # Generate context for the agent
        self.context_md = self._build_context()

    def _build_whitelists(self):
        """Build read and write whitelists from module and dependency paths."""
        mod_path = os.path.abspath(self.module.path)

        # Read whitelist: own module + dep contracts + workspace root files
        self.whitelist_read: set[str] = set()
        self.whitelist_read.add(os.path.join(mod_path, "contract.yaml"))
        self.whitelist_read.add(os.path.join(mod_path, "src"))
        self.whitelist_read.add(os.path.join(mod_path, "tests"))

        for dep in self.deps:
            dep_contract = os.path.abspath(dep.contract_path)
            self.whitelist_read.add(dep_contract)
            dep_dir = os.path.abspath(dep.path)
            self.whitelist_read.add(os.path.join(dep_dir, "contract.yaml"))

        # Write whitelist: only own src/ and tests/
        self.whitelist_write: set[str] = set()
        self.whitelist_write.add(os.path.join(mod_path, "src"))
        self.whitelist_write.add(os.path.join(mod_path, "tests"))

    def _is_under_whitelist(self, path: str, whitelist: set[str]) -> bool:
        """Check if an absolute path is under any whitelisted directory or matches a file."""
        abs_path = os.path.abspath(path)
        for w in whitelist:
            w_abs = os.path.abspath(w)
            # Exact file match
            if abs_path == w_abs:
                return True
            # Under directory
            if abs_path.startswith(w_abs + os.sep):
                return True
            # The whitelisted path itself
            if w_abs.startswith(abs_path + os.sep) or abs_path == w_abs:
                return True
        return False

    # ── Tool mediation ─────────────────────────────────────────────────────────

    def read_file(self, path: str) -> str:
        """Mediated read: blocked if path is outside module boundary.

        Allowed:
            modules/<module>/src/*  — own source
            modules/<module>/tests/* — own tests
            modules/<module>/contract.yaml — own contract
            modules/<dep>/contract.yaml — dependency contracts

        Blocked:
            modules/<dep>/src/* — dependency implementation
            modules/<sibling>/* — sibling modules
            Anything else
        """
        abs_path = os.path.abspath(path)

        if self._is_under_whitelist(abs_path, self.whitelist_read):
            self.stats.operations_allowed += 1
            if not os.path.isfile(abs_path):
                raise FirewallBlocked("read_file", f"file not found: {path}")
            with open(abs_path) as f:
                return f.read()

        self.stats.operations_blocked += 1
        raise FirewallBlocked(
            "read_file",
            f"'{path}' is outside module '{self.module_name}' boundary. "
            f"You may only read files within your module and dependency contracts."
        )

    def write_file(self, path: str, content: str) -> None:
        """Mediated write: blocked if path outside module src/ or tests/.
        Also scans content for illegal imports of undeclared dependencies.
        """
        abs_path = os.path.abspath(path)

        # Must be under write whitelist (own src/ or tests/)
        if not self._is_under_whitelist(abs_path, self.whitelist_write):
            self.stats.operations_blocked += 1
            raise FirewallBlocked(
                "write_file",
                f"'{path}' is not in your module's src/ or tests/. "
                f"You may only write to your own module."
            )

        # Scan for boundary violations (imports of undeclared deps)
        if abs_path.endswith(".py"):
            boundary_issues = self._scan_imports(content, abs_path)
            if boundary_issues:
                self.stats.operations_blocked += 1
                raise FirewallBlocked(
                    "write_file",
                    f"boundary violation in {os.path.basename(path)}:\n  " +
                    "\n  ".join(boundary_issues)
                )

        # Budget check before write
        new_loc = sum(1 for line in content.splitlines() if line.strip())
        new_tokens = estimate_tokens(content)

        if new_loc + self.stats.loc_added > self.budget.max_loc:
            self.stats.operations_blocked += 1
            raise BudgetWarning("LOC", new_loc + self.stats.loc_added, self.budget.max_loc)

        if new_tokens + self.stats.tokens_written > self.budget.max_context_tokens:
            self.stats.operations_blocked += 1
            raise BudgetWarning("tokens", new_tokens + self.stats.tokens_written,
                              self.budget.max_context_tokens)

        # Write the file
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w") as f:
            f.write(content)

        self.stats.loc_added += new_loc
        self.stats.tokens_written += new_tokens
        self.stats.files_created += 1
        self.stats.operations_allowed += 1

    def search_files(self, pattern: str, path: Optional[str] = None,
                     target: str = "content") -> list[str]:
        """Mediated search: scoped to module + dep contracts only.

        The search is restricted to the read whitelist. Results from
        outside the boundary are filtered out.
        """
        if path is None:
            path = self.module.path

        abs_base = os.path.abspath(path)

        # Determine which whitelisted dirs/files to search
        search_dirs: list[str] = []
        for w in self.whitelist_read:
            w_abs = os.path.abspath(w)
            # If the requested path is under a whitelisted location, search there
            if abs_base.startswith(w_abs + os.sep) or abs_base == w_abs:
                search_dirs.append(abs_base)
            elif w_abs.startswith(abs_base + os.sep) or w_abs == abs_base:
                search_dirs.append(w_abs)

        if not search_dirs:
            self.stats.operations_blocked += 1
            raise FirewallBlocked(
                "search_files",
                f"'{path}' is outside module boundary. "
                f"Search is restricted to your module and dependency contracts."
            )

        # Perform the search only within whitelisted dirs
        results = []
        pattern_re = re.compile(pattern)

        for search_dir in search_dirs:
            if not os.path.isdir(search_dir):
                if os.path.isfile(search_dir) and target == "content":
                    try:
                        with open(search_dir) as f:
                            for i, line in enumerate(f, 1):
                                if pattern_re.search(line):
                                    results.append(f"{search_dir}:{i}: {line.rstrip()}")
                    except (OSError, UnicodeDecodeError):
                        pass
                continue

            for root, dirs, files in os.walk(search_dir):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    rel = os.path.relpath(fpath, self.workspace_root)

                    if target == "files":
                        if pattern_re.search(rel):
                            results.append(rel)
                    elif target == "content":
                        try:
                            with open(fpath) as f:
                                for i, line in enumerate(f, 1):
                                    if pattern_re.search(line):
                                        results.append(f"{rel}:{i}: {line.rstrip()}")
                        except (OSError, UnicodeDecodeError):
                            pass

        self.stats.operations_allowed += 1
        return results

    def terminal(self, command: str) -> tuple[int, str]:
        """Mediated terminal: blocks commands that reference out-of-bounds paths.

        This is a best-effort heuristic. For strong enforcement, use
        chroot or bubblewrap on a materialized workspace tree.
        """
        # Quick scan for paths in the command
        import shlex
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()

        # Check if any token looks like a path outside our boundary
        for token in tokens:
            # Heuristic: tokens that look like paths
            if os.sep in token or token.startswith("./") or token.startswith("../"):
                abs_token = os.path.abspath(token) if os.path.exists(token) else token
                if os.path.exists(abs_token) and not self._is_under_whitelist(abs_token, self.whitelist_read):
                    self.stats.operations_blocked += 1
                    raise FirewallBlocked(
                        "terminal",
                        f"command references '{token}' which is outside module boundary. "
                        f"Commands must only operate on files within your module."
                    )

        # Allow the command (caller is responsible for running it)
        self.stats.operations_allowed += 1
        return 0, ""  # Caller executes externally

    # ── Import scanning ────────────────────────────────────────────────────────

    _IMPORT_RE = re.compile(
        r'^\s*(?:from\s+(\w+)|import\s+(\w+))',
        re.MULTILINE
    )

    def _scan_imports(self, content: str, filepath: str) -> list[str]:
        """Scan content for imports of any other module.

        D4: modules must NOT import their dependencies — they receive them
        by injection. Even declared deps must not be imported.
        """
        issues = []
        all_modules = {m.name for m in self.ws.modules} - {self.module.name}

        for m in self._IMPORT_RE.finditer(content):
            imported = m.group(1) or m.group(2)
            if imported in all_modules:
                deps = sorted(self.module.contract.dependencies)
                issues.append(
                    f"imports '{imported}' — {self.module.name} modules "
                    f"receive dependencies by INJECTION, never by import. "
                    f"Declared dependencies: {deps}. "
                    f"Use injected arguments (e.g. *, token_store=...) instead."
                )
        return issues

    # ── Context and info ───────────────────────────────────────────────────────

    def _build_context(self) -> str:
        """Generate the agent's context document."""
        return _generate_context_str(self.module, self.deps)

    def context(self) -> str:
        """Return the full context markdown for the agent's system prompt."""
        return self.context_md

    def boundary_summary(self) -> str:
        """Return a summary of what the agent may and may not access."""
        lines = [
            f"Module: {self.module_name}",
            f"Version: {self.module.contract.version}",
            "",
        ]
        if self.deps:
            lines.append("Dependencies (contracts only, no source):")
            for d in self.deps:
                lines.append(f"  - {d.name} → {d.contract_path}")
        else:
            lines.append("Dependencies: none")
        lines.append("")
        lines.append(f"Budget: {self.budget.max_loc} LOC / "
                     f"{self.budget.max_files} files / "
                     f"{self.budget.max_context_tokens} tokens")
        lines.append("")
        lines.append("You may read:")
        for w in sorted(self.whitelist_read):
            lines.append(f"  ✓ {os.path.relpath(w, self.workspace_root)}")
        lines.append("")
        lines.append("You may write:")
        for w in sorted(self.whitelist_write):
            lines.append(f"  ✎ {os.path.relpath(w, self.workspace_root)}")
        return "\n".join(lines)

    def budget_status(self) -> str:
        """Return current budget consumption vs limits."""
        return (
            f"Budget: LOC {self.stats.loc_added}/{self.budget.max_loc}  "
            f"files {self.stats.files_created}/{self.budget.max_files}  "
            f"tokens {self.stats.tokens_written}/{self.budget.max_context_tokens}"
        )

    def stats_summary(self) -> str:
        """Return summary of session operations."""
        total = self.stats.operations_allowed + self.stats.operations_blocked
        return (
            f"Session: {total} operations "
            f"({self.stats.operations_allowed} allowed, "
            f"{self.stats.operations_blocked} blocked)"
        )


def _generate_context_str(target: Module, deps: list[Module]) -> str:
    """Generate CONTEXT.md as a string (without writing to disk)."""
    contract = target.contract
    lines = []
    lines.append(f"# Context: {target.name}\n")
    lines.append(f"**Module:** `{target.name}`")
    lines.append(f"**Version:** {contract.version}\n")

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

    if deps:
        lines.append("## Dependencies (contracts only)\n")
        for dm in deps:
            lines.append(f"- **{dm.name}** — contract defines the complete interface")
        lines.append("")

    lines.append("## Agent Instructions\n")
    lines.append("- **You may edit:** `src/` and `tests/` only.")
    lines.append("- **You may read:** everything within your module boundary.")
    lines.append("- **You may NOT access:** dependency implementations — they are "
                   "intentionally inaccessible.")
    lines.append("- **Dependencies are received by injection, never imported.** "
                   "Code against the dependency's interface, "
                   "and receive dependency handles as injected arguments.")
    lines.append("- **Write tests against fakes** that satisfy the dependency contracts — "
                   "do not import or reach for real dependency implementations.")
    lines.append("")

    return "\n".join(lines)


# ── Harness — session factory ──────────────────────────────────────────────────

class Harness:
    """Factory for ModuleSession instances.

    Usage:
        h = Harness(".")
        session = h.session("auth")
        session.read_file("modules/auth/src/auth.py")  # allowed
        session.read_file("modules/token_store/src/token_store.py")  # BLOCKED
    """

    def __init__(self, workspace_root: str = "."):
        self.workspace_root = os.path.abspath(workspace_root)
        # Validate workspace is loadable
        self.ws = load_workspace(workspace_root)

    def session(self, module_name: str) -> ModuleSession:
        """Create a harness session for an agent working on the given module."""
        return ModuleSession(self.workspace_root, module_name)

    def module_names(self) -> list[str]:
        """List available module names."""
        return [m.name for m in self.ws.modules]

    def validate(self) -> bool:
        """Run static validation. Returns True if clean."""
        from rich import validate_workspace, detect_cycles
        errors = validate_workspace(self.ws)
        cycles = detect_cycles(self.ws.modules)
        for c in cycles:
            from rich import format_cycle_path
            errors.append(f"[DAG] cycle detected: {format_cycle_path(c)}")
        for m in self.ws.modules:
            errors.extend(check_module_budget(m, self.ws))
            errors.extend(check_module_boundaries(m, self.ws))
        return len(errors) == 0
