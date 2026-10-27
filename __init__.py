"""degeneracy-guard: detects generation degeneracy loops and refuses to let them act.

A model in a repeat loop can say "I am repeating" and still cannot stop. That is the
premise of this plugin: the breaker runs outside the model, on the stream and on the way
out, instead of trusting a self-report.

Hook capability boundary (verified against the Hermes plugin API at 22063fc0):
- ``on_stream_delta``      sees every chunk but is documented as off the token path and
                           unable to transform the stream -> detect in real time, cannot
                           abort.
- ``transform_llm_output`` can return a replacement string (first non-None wins) ->
                           replaces already-degenerate output before it reaches the
                           session, but does not stop generation.
- ``pre_tool_call``        can return ``{"action": "block", ...}`` to refuse a call, or
                           ``{"action": "approve", "message", "rule_key"}`` to send it to
                           the human-approval gate (``hermes_cli/plugins.py:1979-2031``,
                           ``_resolve_block_from_details`` at ``:2059-2088``). A gate that is
                           denied, errors or times out refuses the call, so the fallback is
                           safe when nobody is watching.
- ``post_llm_call``        runs after the turn -> record only.

No plugin hook can abort a generation that is in progress. A real abort needs an external
breaker signal checked inside the core chunk loop. Until then this plugin does the part it
can, and records the missing capability in every incident it writes.

Tool arguments: the guard asks rather than refuses, because content alone cannot separate a
deliberately repeated payload from a stuck loop. See ``tool_arg_guard``.

Loading: Hermes imports a directory plugin as a package (``hermes_plugins.<slug>``), so the
sibling modules are imported relatively. Importing them by bare name would put ``paths`` and
``loop_detector`` into the global module namespace, where they can shadow another plugin's
module of the same name.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from . import loop_detector
from . import mode
from .paths import incident_path

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_states: dict[str, object] = {}   # key: session_id|turn_id -> StreamBreakerState
_flagged: dict[str, dict] = {}    # key -> incident dict (read by transform_llm_output)

_REPLACEMENT = (
    "This turn was stopped by degeneracy-guard: the output was judged a degeneracy loop and "
    "the original text was not committed to the session.\n"
    "Thresholds: see FROZEN_THRESHOLDS in loop_detector.py.\n"
    "This is an incident record, not a reply - start a new turn."
)


def _key(**kw) -> str:
    return f"{kw.get('session_id', '')}|{kw.get('turn_id', '')}"


def _write_incident(inc: dict) -> None:
    try:
        path = incident_path(f"{inc.get('reason', 'incident')}_{inc.get('key', '')}")
        path.write_text(json.dumps(inc, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.error("degeneracy-guard: incident write failed: %s", exc)


def on_stream_delta(delta=None, kind="text", **kw):
    """Judge every chunk. On a verdict, write an incident and flag the turn."""
    if kind != "text" or not delta:
        return
    key = _key(**kw)
    with _lock:
        st = _states.get(key)
        if st is None:
            st = _states[key] = loop_detector.StreamBreakerState()
        verdict = st.feed(delta)
        if verdict is None:
            return
        text = st.text
        inc = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "key": key,
            "session_id": kw.get("session_id", ""),
            "turn_id": kw.get("turn_id", ""),
            "iteration": kw.get("iteration"),
            "model": kw.get("model", ""),
            "provider": kw.get("provider", ""),
            "surface": kw.get("surface", ""),
            "reason": verdict.reason,
            "detail": verdict.detail,
            "metric": verdict.metric,
            "chars_at_trigger": len(text),
            "thresholds": loop_detector.FROZEN_THRESHOLDS,
            "capability": {
                "detected_at": "on_stream_delta (off token path)",
                "aborted": False,
                "note": "plugin hooks cannot abort generation; core-side signal required",
            },
            "tail_sample": text[-800:],
        }
        _flagged[key] = inc
        _write_incident(inc)
    logger.warning("degeneracy-guard TRIGGERED %s: %s | %s", verdict.reason, verdict.detail, key)


def transform_llm_output(response_text=None, **kw):
    """Replace the output of a flagged turn so it never reaches the session.

    Returning None means "do not interfere", which is the path every healthy turn takes.
    """
    key = _key(**kw)
    with _lock:
        inc = _flagged.get(key)
        if inc is None:
            return None
        if mode.observes():
            # Observe mode runs the same detection and keeps the same record; the only thing it does
            # not do is reach the session. The label makes the difference legible later.
            inc["action"] = "observed"
            inc["observed_at"] = "transform_llm_output"
            inc["would_replace_chars"] = len(response_text or "")
            _write_incident(inc)
            _flagged.pop(key, None)
            logger.warning("degeneracy-guard: observe mode, left degenerate output in place for %s", key)
            return None
        inc["replaced_by_transform_llm_output"] = True
        inc["original_chars"] = len(response_text or "")
        _write_incident(inc)
        _flagged.pop(key, None)
    logger.warning("degeneracy-guard: replaced degenerate output for %s", key)
    return _REPLACEMENT


def post_llm_call(**kw):
    """End of turn: drop state, and record a flag nobody consumed."""
    key = _key(**kw)
    with _lock:
        _states.pop(key, None)
        inc = _flagged.pop(key, None)
        if inc is not None:
            inc["unconsumed_at_post_llm_call"] = True
            _write_incident(inc)


def register(ctx):
    ctx.register_hook("on_stream_delta", on_stream_delta)
    ctx.register_hook("transform_llm_output", transform_llm_output)
    ctx.register_hook("post_llm_call", post_llm_call)
    # The guard belongs where an action leaves the system: a tool call is the only place
    # that can produce an external side effect.
    from .tool_arg_guard import on_pre_tool_call
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    # Long sessions are where degeneracy grows: prevention is about length, not detection.
    from .session_length_guard import on_pre_llm_call as _on_length
    ctx.register_hook("pre_llm_call", _on_length)
