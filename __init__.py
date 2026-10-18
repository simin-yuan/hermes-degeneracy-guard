"""degeneracy-guard: detects generation degeneracy loops and refuses to let them act.

A model in a repeat loop can say "I am repeating" and still cannot stop. That is the
premise of this plugin: the breaker runs outside the model, on the stream and on the way
out, instead of trusting a self-report.

Hook capability boundary (verified against the Hermes plugin API):
- ``on_stream_delta``      sees every chunk but is documented as off the token path and
                           unable to transform the stream -> detect in real time, cannot
                           abort.
- ``transform_llm_output`` can return a replacement string (first non-None wins) ->
                           replaces already-degenerate output before it reaches the
                           session, but does not stop generation.
- ``post_llm_call``        runs after the turn -> record only.

No plugin hook can abort a generation that is in progress. A real abort needs an external
breaker signal checked inside the core chunk loop. Until then this plugin does the part it
can, and records the missing capability in every incident it writes.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _ensure_sibling_path() -> None:
    """Put this directory on sys.path so sibling modules import by bare name.

    The loader imports this module by file path and does not add the plugin directory to
    sys.path. Sibling modules are not packages, so without this step ``register()`` raises
    ModuleNotFoundError; the loader then rolls back the whole plugin, and every hook
    registered before the failure disappears with it while the plugin still shows as
    enabled. Registration is therefore ordered so a failure here is not silent.
    """
    d = str(Path(__file__).resolve().parent)
    if d not in sys.path:
        sys.path.insert(0, d)


# Must run before the first sibling import below: the loader execs this file without the
# plugin directory on sys.path, so a module-level ``from paths import ...`` fails otherwise.
_ensure_sibling_path()

from paths import incident_dir  # noqa: E402

_lock = threading.Lock()
_states: dict[str, object] = {}   # key: session_id|turn_id -> StreamBreakerState
_flagged: dict[str, dict] = {}    # key -> incident dict (read by transform_llm_output)

_REPLACEMENT = (
    "This turn was stopped by degeneracy-guard: the output was judged a degeneracy loop and "
    "the original text was not committed to the session.\n"
    "Thresholds: see FROZEN_THRESHOLDS in loop_detector.py.\n"
    "This is an incident record, not a reply - start a new turn."
)


def _load_detector():
    """Import the detector from this directory (same file the tests use)."""
    d = str(Path(__file__).resolve().parent)
    if d not in sys.path:
        sys.path.insert(0, d)
    import loop_detector as mod
    return mod


try:
    _DET = _load_detector()
except Exception as exc:            # never take the host down because a detector is missing
    logger.error("degeneracy-guard: failed to load loop_detector: %s", exc)
    _DET = None


def _key(**kw) -> str:
    return f"{kw.get('session_id', '')}|{kw.get('turn_id', '')}"


def _write_incident(inc: dict) -> None:
    try:
        directory = incident_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = inc["ts"].replace(":", "").replace("-", "")
        path = directory / f"{stamp}_{inc['key'].replace('|', '_')}.json"
        path.write_text(json.dumps(inc, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.error("degeneracy-guard: incident write failed: %s", exc)


def on_stream_delta(delta=None, kind="text", **kw):
    """Judge every chunk. On a verdict, write an incident and flag the turn."""
    if _DET is None or kind != "text" or not delta:
        return
    key = _key(**kw)
    with _lock:
        st = _states.get(key)
        if st is None:
            st = _states[key] = _DET.StreamBreakerState()
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
            "thresholds": _DET.FROZEN_THRESHOLDS,
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
    _ensure_sibling_path()
    ctx.register_hook("on_stream_delta", on_stream_delta)
    ctx.register_hook("transform_llm_output", transform_llm_output)
    ctx.register_hook("post_llm_call", post_llm_call)
    # The guard belongs where an action leaves the system: a tool call is the only place
    # that can produce an external side effect.
    from tool_arg_guard import on_pre_tool_call
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    # Long sessions are where degeneracy grows: prevention is about length, not detection.
    from session_length_guard import on_pre_llm_call as _on_length
    ctx.register_hook("pre_llm_call", _on_length)
