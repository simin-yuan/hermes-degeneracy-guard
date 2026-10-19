"""pre_tool_call guard: catch degenerate content on its way into tool arguments.

Why this layer is necessary: a degeneracy loop writes its repeated text into the JSON
arguments of tool calls. ``transform_llm_output`` only sees the assistant's visible text,
and it runs after the tool loop, so it guards the wrong exit. The action that reaches the
outside world is the tool call, so the check belongs on ``pre_tool_call``.

The rule itself lives in ``loop_detector.judge_tool_arg``: tool arguments are data, and a
payload that carries a data-like shape with variety (a table, a JSON array, a matrix, a CSV
export) is allowed through even when it repeats, matching the case Hermes core allows on
purpose. Only repetition with no data-like excuse is refused.

Returning ``{"action": "block", "message": ...}`` refuses the call.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from . import loop_detector
from .paths import incident_path

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# Arguments shorter than this cannot be a loop; the detector applies the same bound.
MIN_CHARS = loop_detector.TOOL_ARG_THRESHOLDS["min_chars"]

# Tools the user has exempted, read on every call so a value exported in a shell applies to
# the next tool call without a restart. No caching: a stale allow-list would be a silent
# hole in the guard.
ALLOW_ENV = "DEGENERACY_GUARD_ALLOW_TOOLS"

_BLOCK_MESSAGE = (
    "DEGENERACY_GUARD_BLOCK: the arguments for this tool call are repetition-dominated "
    "({reason}: {detail}). The call was refused before it could reach the outside world.\n"
    "If the content is intentional: split it across several calls, or let this tool through "
    "by adding it to {env} (comma-separated tool names) and retrying."
)


def allowed_tools() -> set[str]:
    raw = os.environ.get(ALLOW_ENV, "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _strings_in(obj, out, depth=0):
    """Collect every string value in args (tool arguments can be nested dicts/lists)."""
    if depth > 6:
        return
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _strings_in(v, out, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _strings_in(v, out, depth + 1)


def _write_incident(inc: dict) -> None:
    try:
        path = incident_path(f"TOOLARG_{inc.get('tool', 'unknown')}")
        path.write_text(json.dumps(inc, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.error("tool-arg guard: incident write failed: %s", exc)


def on_pre_tool_call(tool_name: str = "", args=None, session_id: str = "", **kw):
    """Block a tool call whose arguments are degenerate rather than data."""
    if not isinstance(args, dict):
        return None
    if tool_name and tool_name in allowed_tools():
        return None

    strings: list[str] = []
    _strings_in(args, strings)

    for field_text in strings:
        if not field_text or len(field_text) < MIN_CHARS:
            continue
        verdict = loop_detector.judge_tool_arg(field_text)
        if not verdict.block:
            continue
        inc = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": "TOOL_ARG_GUARD",
            "tool": tool_name,
            "session_id": session_id,
            "reason": verdict.reason,
            "detail": verdict.detail,
            "arg_chars": len(field_text),
            "metrics": verdict.metrics,
            "action": "blocked",
            "thresholds": loop_detector.TOOL_ARG_THRESHOLDS,
        }
        with _lock:
            _write_incident(inc)
        return {
            "action": "block",
            "message": _BLOCK_MESSAGE.format(
                reason=verdict.reason, detail=verdict.detail, env=ALLOW_ENV),
        }

    return None
