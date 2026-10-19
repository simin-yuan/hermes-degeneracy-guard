"""pre_llm_call guard: long sessions are where degeneracy grows.

Detection handles the symptom; length handles the cause. The guard does not touch runtime
configuration (which is protected from the agent). It observes the size of the conversation
on ``pre_llm_call`` and suggests starting a new session.

MAX_MESSAGES is a design constant, not a measurement of real usage: it is set low enough
that a session which reaches it has a context worth restarting, and high enough that normal
work never sees the notice.

Thresholds are frozen. De-bounce: one reminder per RENOTIFY_EVERY_N_NEW_MESSAGES messages in
the same session, so the notice does not appear on every turn.
"""

from __future__ import annotations

import json
import time

from .paths import state_root

# -- Frozen thresholds ----------------------------------------------------------------
MAX_MESSAGES = 500
# Rough conversion used only to report an approximate context size: characters / 3.5.
CHARS_PER_TOKEN = 3.5
RENOTIFY_EVERY_N_NEW_MESSAGES = 50

_STATE_FILE = "session_length_state.json"


def _load_state() -> dict:
    try:
        return json.loads((state_root() / _STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    try:
        path = state_root() / _STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _rough_tokens(history) -> int:
    """Rough context estimate: characters in content/tool_calls divided by CHARS_PER_TOKEN."""
    total = 0
    try:
        for m in history or []:
            if isinstance(m, dict):
                for k in ("content", "tool_calls"):
                    v = m.get(k)
                    if isinstance(v, str):
                        total += len(v)
                    elif v is not None:
                        total += len(json.dumps(v, ensure_ascii=False))
            else:
                total += len(str(m))
    except Exception:
        return 0
    return int(total / CHARS_PER_TOKEN)


def check_session_length(session_id: str, history) -> str | None:
    """Return the reminder text, or None."""
    n = len(history or [])
    if n < MAX_MESSAGES:
        return None
    tok = _rough_tokens(history)

    st = _load_state()
    last_n = int(st.get(session_id, {}).get("last_notified_at_messages", 0) or 0)
    if last_n and (n - last_n) < RENOTIFY_EVERY_N_NEW_MESSAGES:
        return None
    st[session_id] = {"last_notified_at_messages": n, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    # Keep only the most recent 20 sessions so the file cannot grow without bound.
    if len(st) > 20:
        for k in list(st)[:-20]:
            st.pop(k, None)
    _save_state(st)

    return (
        "[Session length notice]\n"
        f"  This session has {n} messages (the plugin suggests a new session after "
        f"{MAX_MESSAGES}); the context is roughly {tok:,} tokens.\n"
        "  Degeneracy loops are a risk in very long sessions: repetition is what a model "
        "falls back on when the context stops fitting, and detection only repairs the "
        "symptom.\n"
        "  Write the current state to disk, then start a new session with just the "
        "conclusions you need."
    )


def on_pre_llm_call(session_id: str = "", conversation_history=None, **kw):
    try:
        msg = check_session_length(session_id, conversation_history)
    except Exception:
        return None
    if not msg:
        return None
    return {"context": msg}
