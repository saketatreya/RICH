"""Render a run's durable events as something an operator can watch.

The events are already the log — they are append-only, ordered, and the only
account of a run that survives the process. What was missing is a reading of
them: `events` printed JSON, which answers "what happened" only if you already
know what to look for, and a run takes minutes during which it says nothing.

Nothing here is a second source of truth. Every line is one stored event; the
formatting is the whole of the contribution.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterator, Mapping

from .store import RichStore

# Events whose payload is worth a second line rather than a glance.
_DETAILED = frozenset(
    {
        "evidence.recorded",
        "task.failed",
        "run.execution_error",
        "task.retry_scheduled",
        "task.reopened",
        "task.superseded",
        "task.retry_withheld",
        "task.attribution_ignored",
        "model.attempt.failed",
    }
)

_MARK = {
    "passed": "ok",
    "succeeded": "ok",
    "failed": "!!",
    "error": "!!",
    "canceled": "--",
}


def _clock(created_at: str) -> str:
    """Wall-clock time, which is what an operator is comparing against."""

    return created_at[11:19] if len(created_at) >= 19 else created_at


def _mark(event: Mapping[str, Any]) -> str:
    payload = event.get("payload") or {}
    status = str(payload.get("status") or "")
    if status in _MARK:
        return _MARK[status]
    kind = event.get("event_type", "")
    if kind.endswith(".failed") or kind.endswith("_error"):
        return "!!"
    if kind.endswith(".succeeded") or kind.endswith("_finished"):
        return "ok"
    return "  "


def _detail(event: Mapping[str, Any]) -> str:
    payload = event.get("payload") or {}
    if event["event_type"] == "evidence.recorded":
        requirements = payload.get("requirement_ids") or []
        scenarios = payload.get("acceptance_scenario_ids") or []
        parts = [str(payload.get("kind", "")), str(payload.get("status", ""))]
        if requirements:
            parts.append(f"{len(requirements)} req")
        if scenarios:
            parts.append(f"{len(scenarios)} scenario")
        return " · ".join(part for part in parts if part)
    if event["event_type"] == "run.execution_error":
        error_type = str(payload.get("error_type", "error"))
        message = str(payload.get("message", "")).strip()
        return f"{error_type}: {message}" if message else error_type
    if event["event_type"] == "task.retry_scheduled":
        return (
            f"attempt {payload.get('next_attempt', '?')} "
            f"in {payload.get('backoff_seconds', '?')}s"
        )
    if event["event_type"] == "task.reopened":
        return (
            f"attempt {payload.get('next_attempt', '?')}: "
            f"{payload.get('failed_node_id', 'the application')} failed "
            "acceptance on pages this task owns"
        )
    if event["event_type"] == "task.superseded":
        owners = ", ".join(payload.get("reopened_node_ids") or [])
        return f"runs again after {owners or 'its dependencies'}"
    if event["event_type"] == "task.retry_withheld":
        owners = ", ".join(payload.get("exhausted_node_ids") or [])
        return f"no retry: {owners} own what failed and have no attempts left"
    if event["event_type"] == "task.attribution_ignored":
        owners = ", ".join(payload.get("node_ids") or [])
        return f"attribution to {owners} ignored: {payload.get('reason', '')}"

    for key in ("summary", "reason", "error_type", "status"):
        if payload.get(key):
            return str(payload[key])
    return ""


def format_event(event: Mapping[str, Any]) -> str:
    """One stored event as one line."""

    task = event.get("task_id") or ""
    suffix = f"  [{task.rsplit(':', 1)[-1]}]" if task else ""
    line = (
        f"{_clock(str(event.get('created_at', '')))} "
        f"{_mark(event):2} "
        f"{event['event_type']}{suffix}"
    )
    detail = _detail(event)
    if detail and event["event_type"] in _DETAILED:
        return f"{line}\n{'':12}{detail}"
    return f"{line}  {detail}".rstrip()


def follow_run(
    store: RichStore,
    run_id: str,
    *,
    follow: bool = False,
    after_sequence: int = 0,
    poll_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    is_finished: Callable[[], bool] | None = None,
) -> Iterator[str]:
    """Yield formatted lines, optionally waiting for more.

    Polling rather than subscribing, because the events table is the interface
    and a second notification path would be a second thing that can be wrong.
    """

    cursor = after_sequence
    while True:
        events = store.list_events(run_id, after_sequence=cursor)
        for event in events:
            cursor = event["sequence"]
            yield format_event(event)
        if not follow:
            return
        if is_finished is not None and is_finished() and not events:
            return
        sleep(poll_seconds)


def run_is_settled(store: RichStore, run_id: str) -> bool:
    """Whether a run has reached a state that produces no further events."""

    try:
        status = store.get_run(run_id)["status"]
    except Exception:
        return True
    return status in {"succeeded", "failed", "canceled"}
