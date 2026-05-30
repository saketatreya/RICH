"""orchestrator.py — Multi-agent orchestration harness.

Spawns one LLM-powered sub-agent per module, each bounded by its
ModuleSession firewall. Agents communicate through contracts, not source.
The harness enforces boundaries on every tool call.

Requires: OPENROUTER_API_KEY env var, or pass api_key directly.

Usage:
    from orchestrator import Orchestrator

    orch = Orchestrator(".", api_key="sk-or-...")
    orch.run_all(task="Implement all modules according to their contracts")
    # Spawns agent_auth, agent_token_store, agent_user_repo in parallel
    # Each agent sees only its own source + dep contracts
    # Results are collected per module
"""

import os
import json
import time
import sys
from typing import Optional, Callable

# ── LLM Client ─────────────────────────────────────────────────────────────────

class LLMClient:
    """Minimal OpenRouter-compatible API client."""

    def __init__(self, api_key: str = None, model: str = None,
                 base_url: str = "https://openrouter.ai/api/v1"):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.model = model or os.environ.get("RICH_MODEL", "anthropic/claude-sonnet-4")
        self.base_url = base_url

        if not self.api_key:
            raise ValueError(
                "No API key provided. Set OPENROUTER_API_KEY env var "
                "or pass api_key= to Orchestrator()."
            )

    def chat(self, messages: list[dict], tools: list[dict] = None,
             max_tokens: int = 4096) -> dict:
        """Send a chat completion request. Returns the API response dict."""
        import urllib.request
        import urllib.error

        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            raise RuntimeError(f"API error {e.code}: {error_body}")


# ── Bounded Agent ──────────────────────────────────────────────────────────────

class BoundedAgent:
    """One LLM agent bounded to a single module by the harness firewall.

    The agent receives:
      - The module's contract (what it must implement)
      - The module's own source (what it may edit)
      - The contracts of direct dependencies (interface, no source)

    Every tool call is mediated through ModuleSession. The agent
    cannot read dependency source, cannot import other modules,
    cannot exceed its budget.
    """

    # Tool definitions for the LLM (matches ModuleSession methods)
    TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file within your module boundary. Blocked for dependency source.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to the file to read"}
                    },
                    "required": ["path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write to a file in your module's src/ or tests/. Blocked for imports of other modules.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path within your module"},
                        "content": {"type": "string", "description": "File contents"}
                    },
                    "required": ["path", "content"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_files",
                "description": "Search within your module boundary. Scoped to whitelisted directories.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Search pattern (regex)"},
                        "path": {"type": "string", "description": "Directory to search in"}
                    },
                    "required": ["pattern"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "run_tests",
                "description": "Run your module's test suite.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "report_done",
                "description": "Call this when you've completed your task. Include a summary of what you did.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "What you accomplished"}
                    },
                    "required": ["summary"]
                }
            }
        },
    ]

    def __init__(self, module_name: str, session, llm: LLMClient,
                 task: str = None):
        self.module_name = module_name
        self.session = session
        self.llm = llm
        self.task = task or f"Implement the {module_name} module according to its contract."
        self.messages = []
        self.done = False
        self.result = None
        self.turns = 0
        self.max_turns = 20

    def run(self) -> dict:
        """Run the agent loop until done or max turns reached."""
        # Build system prompt from harness context
        system = self.session.context()
        system += f"\n\n## Your Task\n\n{self.task}\n\n"
        system += (
            "Work step by step. Read the files you need, write code, run tests. "
            "Call report_done() when finished. "
            "Remember: you CANNOT read dependency source code. "
            "Dependency contracts are your complete interface."
        )

        self.messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": "Begin working on your task."},
        ]

        while not self.done and self.turns < self.max_turns:
            self.turns += 1
            try:
                response = self.llm.chat(self.messages, tools=self.TOOLS)
            except Exception as e:
                self.result = {"error": str(e)}
                break

            choice = response.get("choices", [{}])[0]
            message = choice.get("message", {})

            # If the model wants to call tools
            if message.get("tool_calls"):
                self.messages.append(message)
                for tc in message["tool_calls"]:
                    tool_result = self._execute_tool(tc)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    })
            else:
                # Model responded with text — it's done or needs direction
                content = message.get("content", "")
                self.messages.append({"role": "assistant", "content": content})
                # If no tool calls and it's giving a final answer, consider it done
                if self.turns > 1:
                    self.done = True
                    self.result = {"summary": content}

        if not self.result:
            self.result = {"summary": f"Reached max turns ({self.max_turns}) without completing."}

        return {
            "module": self.module_name,
            "turns": self.turns,
            "result": self.result,
            "stats": {
                "allowed": self.session.stats.operations_allowed,
                "blocked": self.session.stats.operations_blocked,
                "budget": self.session.budget_status(),
            },
        }

    def _execute_tool(self, tool_call: dict) -> str:
        """Execute a tool call through the harness session."""
        fn = tool_call["function"]
        name = fn["name"]
        args = json.loads(fn.get("arguments", "{}"))

        try:
            if name == "read_file":
                content = self.session.read_file(args["path"])
                # Truncate very large files
                if len(content) > 8000:
                    content = content[:8000] + f"\n... (truncated, {len(content)} bytes total)"
                return content

            elif name == "write_file":
                self.session.write_file(args["path"], args["content"])
                return f"OK: written {len(args['content'])} bytes to {args['path']}"

            elif name == "search_files":
                results = self.session.search_files(
                    args.get("pattern", ".*"),
                    path=args.get("path"),
                    target=args.get("target", "content"),
                )
                if results:
                    return "\n".join(results[:20])
                return "(no matches)"

            elif name == "run_tests":
                return self._run_module_tests()

            elif name == "report_done":
                self.done = True
                self.result = {"summary": args.get("summary", "Done.")}
                return "Task marked as complete."

            else:
                return f"Unknown tool: {name}"

        except Exception as e:
            return f"ERROR: {e}"

    def _run_module_tests(self) -> str:
        """Run the module's test suite."""
        import subprocess
        import os

        module_dir = self.session.module.path
        src_dir = os.path.join(module_dir, "src")
        tests_dir = os.path.join(module_dir, "tests")

        env = os.environ.copy()
        env["PYTHONPATH"] = src_dir + ":" + env.get("PYTHONPATH", "")

        try:
            result = subprocess.run(
                ["python3", "-m", "pytest", tests_dir, "-v", "--tb=short", "-q"],
                capture_output=True, text=True, timeout=30,
                cwd=self.session.workspace_root, env=env,
            )
            output = result.stdout + result.stderr
            if len(output) > 3000:
                output = output[:3000] + "\n... (truncated)"
            return output
        except subprocess.TimeoutExpired:
            return "Tests timed out (>30s)"
        except Exception as e:
            return f"Failed to run tests: {e}"


# ── Orchestrator ───────────────────────────────────────────────────────────────

class Orchestrator:
    """Multi-agent orchestrator: spawns one bounded agent per module.

    Usage:
        orch = Orchestrator(".", api_key="sk-or-...")
        results = orch.run_all(task="Implement all modules per their contracts")

        # Or run a single module:
        result = orch.run_one("auth", task="Add OAuth support")
    """

    def __init__(self, workspace_root: str = ".", api_key: str = None,
                 model: str = None):
        self.workspace_root = os.path.abspath(workspace_root)
        self.llm = LLMClient(api_key=api_key, model=model)

        # Load the workspace
        from harness import Harness
        self.harness = Harness(workspace_root)
        self.module_names = self.harness.module_names()

    def run_one(self, module_name: str, task: str = None) -> dict:
        """Run a single bounded agent on one module."""
        if module_name not in self.module_names:
            return {"error": f"Module '{module_name}' not found. Available: {self.module_names}"}

        session = self.harness.session(module_name)
        agent = BoundedAgent(module_name, session, self.llm, task=task)

        print(f"\n{'='*60}")
        print(f"  Agent: {module_name}")
        print(f"  Dependencies: {[d.name for d in session.deps]}")
        print(f"  Boundary: {len(session.whitelist_read)} readable, "
              f"{len(session.whitelist_write)} writable")
        print(f"{'='*60}")

        result = agent.run()

        print(f"  Turns: {result['turns']}")
        print(f"  Blocked ops: {result['stats']['blocked']}")
        print(f"  Budget: {result['stats']['budget']}")

        return result

    def run_all(self, task: str = None, parallel: bool = False) -> dict[str, dict]:
        """Run bounded agents on all modules.

        Args:
            task: Override task for all modules (default: implement per contract)
            parallel: If True, run in parallel (requires threading). Default: sequential.

        Returns:
            {module_name: result_dict}
        """
        if task is None:
            task = "Implement this module according to its contract.yaml. Write the source in src/, tests in tests/. Call report_done() when complete."

        results = {}

        if parallel:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.module_names)) as ex:
                futures = {
                    ex.submit(self._run_one_silent, name, task): name
                    for name in self.module_names
                }
                for future in concurrent.futures.as_completed(futures):
                    name = futures[future]
                    results[name] = future.result()
        else:
            # Sequential — with output
            for name in self.module_names:
                results[name] = self.run_one(name, task=task)

        # Summary
        print(f"\n{'='*60}")
        print("  ORCHESTRATOR SUMMARY")
        print(f"{'='*60}")
        for name, r in results.items():
            if "error" in r:
                print(f"  {name}: ERROR — {r['error']}")
            else:
                summary = r.get("result", {}).get("summary", "no summary")[:80]
                print(f"  {name}: {r['turns']} turns, "
                      f"{r['stats']['blocked']} blocked — {summary}")
        print(f"{'='*60}\n")
        return results

    def _run_one_silent(self, module_name: str, task: str) -> dict:
        """Run without printing (for parallel mode)."""
        session = self.harness.session(module_name)
        agent = BoundedAgent(module_name, session, self.llm, task=task)
        return agent.run()
