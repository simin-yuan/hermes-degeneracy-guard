"""pre_tool_call guard: hand repetition-dominated tool arguments to the user.

Why this layer is necessary: a degeneracy loop writes its repeated text into the JSON
arguments of tool calls. ``transform_llm_output`` only sees the assistant's visible text,
and it runs after the tool loop, so it guards the wrong exit. The action that reaches the
outside world is the tool call, so the check belongs on ``pre_tool_call``.

Why it asks instead of refusing: content alone cannot tell a deliberately repeated payload
from a model stuck in a loop — a thousand-row zero matrix is data in one call and pathology
in the next, and only the user knows which. So the guard does not rule on intent. When the
arguments look like repetition without a data shape, it returns
``{"action": "approve", ...}`` and Hermes runs its own human-approval gate: the user either
approves (once, or always for this tool), or the call is refused — including when the gate
times out or no human is present, because that gate is fail-closed.

The measured cost of that choice is published in README.md: a hold-out set written without
sight of these rules, and how many of its legitimate payloads reach the prompt.
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

# Grain of the "[a]lways allow" answer. Per tool, not per argument: the same tool asked again
# with different content should not re-prompt every ten seconds.
RULE_KEY_PREFIX = "degeneracy-guard"

_ASK_MESSAGE = (
    "DEGENERACY_GUARD_APPROVAL: these arguments look like a repetition loop rather than "
    "data ({reason}: {detail}). No data shape was found in the payload, so this guard cannot "
    "tell an intentional repeat from a stuck generation — that call is yours to make.\n"
    "Approve to run the call as written. If this kind of content is normal for this tool, "
    "choose \"always\" and it will not ask again for {tool!r}.\n"
    "Deny, or leave it unanswered, and the call is refused.\n"
    "To exempt the tool entirely, list it in {env} (comma-separated tool names)."
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
    """Ask the user about a tool call whose arguments are repetition without a data shape."""
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
        if not verdict.ask:
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
            "action": "approve_requested",
            # The hook returns a directive and is not called again with the user's answer, so
            # the outcome is genuinely unknown here. Recorded as such rather than guessed.
            "approval_outcome": "not observable from this hook",
            "thresholds": loop_detector.TOOL_ARG_THRESHOLDS,
        }
        with _lock:
            _write_incident(inc)
        return {
            "action": "approve",
            "message": _ASK_MESSAGE.format(
                reason=verdict.reason, detail=verdict.detail, tool=tool_name,
                env=ALLOW_ENV),
            "rule_key": f"{RULE_KEY_PREFIX}:{tool_name}" if tool_name else RULE_KEY_PREFIX,
        }

    return None
