"""Backend selection for RICH skill calls.

`skills.py` imports its model seam from `llm.py` at module import time. Backends
install by monkeypatching that seam, matching the existing `subagent_skill.install()`
pattern.
"""

import os


_installed_backend: str | None = None
_installed_module = None


def selected(default: str = "claude") -> str:
    return os.environ.get("RICH_BACKEND", default).strip().lower() or default


def install_from_env(default: str = "claude") -> str:
    """Install the backend named by RICH_BACKEND and return its normalized name."""
    global _installed_backend, _installed_module
    name = selected(default)

    if _installed_module and hasattr(_installed_module, "uninstall"):
        _installed_module.uninstall()
        _installed_module = None

    if name in ("claude", "subagent", "claude-p"):
        import subagent_skill as mod
        normalized = "claude"
    elif name == "codex":
        import codex_skill as mod
        normalized = "codex"
    elif name in ("openrouter", "llm"):
        _installed_backend = "openrouter"
        _installed_module = None
        return "openrouter"
    else:
        raise ValueError(
            f"Unknown RICH_BACKEND={name!r}; expected claude, codex, or openrouter"
        )

    mod.install()
    _installed_backend = normalized
    _installed_module = mod
    return normalized


def is_available(default: str = "claude") -> bool:
    name = selected(default)
    if name in ("claude", "subagent", "claude-p"):
        import subagent_skill
        return subagent_skill.is_available()
    if name == "codex":
        import codex_skill
        return codex_skill.is_available()
    if name in ("openrouter", "llm"):
        import llm
        return llm.is_available()
    return False


def print_telemetry():
    if _installed_module and hasattr(_installed_module, "print_telemetry"):
        _installed_module.print_telemetry()


def reset_telemetry():
    if _installed_module and hasattr(_installed_module, "reset_telemetry"):
        _installed_module.reset_telemetry()
