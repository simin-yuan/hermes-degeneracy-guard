"""pre_tool_call guard: catch degenerate content on its way into tool arguments.

Why this layer is necessary: a degeneracy loop writes its repeated text into the JSON
arguments of tool calls. ``transform_llm_output`` only sees the assistant's visible text,
and it runs after the tool loop, so it guards the wrong exit. The action that reaches the
outside world is the tool call, so the check belongs on ``pre_tool_call``.

Returning ``{"action": "block", "message": ...}`` refuses the call.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from paths import incident_dir

_lock = threading.Lock()

# Arguments shorter than this cannot be a loop; skipping them keeps the guard cheap.
MIN_CHARS = 200


def _load_detector():
    d = str(Path(__file__).resolve().parent)
    import sys
    if d not in sys.path:
        sys.path.insert(0, d)
    import loop_detector as mod
    return mod


try:
    _DET = _load_detector()
except Exception as exc:
    import logging
    logging.getLogger(__name__).error("tool-arg guard: detector load failed: %s", exc)
    _DET = None


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
        directory = incident_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        safe = str(inc.get("tool", "unknown")).replace("/", "_").replace("\\", "_")[:40]
        (directory / f"{stamp}_TOOLARG_{safe}.json").write_text(
            json.dumps(inc, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("tool-arg guard: incident write failed: %s", exc)


def on_pre_tool_call(tool_name: str = "", args=None, session_id: str = "", **kw):
    """Block a tool call whose arguments match the degeneracy criteria."""
    if _DET is None or not isinstance(args, dict):
        return None

    strings = []
    _strings_in(args, strings)
    if not strings:
        return None

    for field_text in strings:
        if not field_text or len(field_text) < MIN_CHARS:
            continue
        v = _DET.check_text(field_text)
        if v is None or not v.triggered:
            continue
        inc = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": "TOOL_ARG_GUARD",
            "tool": tool_name,
            "session_id": session_id,
            "reason": v.reason,
            "detail": v.detail,
            "metric": v.metric,
            "arg_chars": len(field_text),
            "action": "blocked",
            "thresholds": _DET.FROZEN_THRESHOLDS,
        }
        with _lock:
            _write_incident(inc)
        msg = (f"DEGENERACY_GUARD_BLOCK: tool arguments match the degeneracy criteria "
               f"({v.reason}: {v.detail}). The argument looks like degenerate output and "
               f"was blocked before it could reach the outside world. "
               f"Retry in a fresh session.")
        return {"action": "block", "message": msg}

    return None
