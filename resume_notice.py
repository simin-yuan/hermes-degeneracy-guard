"""post-abort recovery: tell the next turn that a generation was stopped.

Aborting without reporting the abort only replaces one kind of dead end with another: the
next session has no idea that the previous turn ended mid-thought, and rebuilds on a
half-written artifact.

Mechanism: the core-side abort writes a CORE_ABORT incident. On the first ``pre_llm_call``
of a new session this module reads it and returns ``{"context": ...}``, which the runtime
appends to the user message instead of the system prompt.

Deduplication: a last_told marker, so each abort is reported once.
"""

from __future__ import annotations

import json
import time

from paths import INCIDENT_DIR, STATE_ROOT

_TOLD_STATE = STATE_ROOT / "last_told.json"


def _latest_abort_since(marker: float):
    """Newest CORE_ABORT incident newer than marker as (epoch, record), or None."""
    best = None
    try:
        if not INCIDENT_DIR.exists():
            return None
        for f in INCIDENT_DIR.glob("*_CORE_ABORT.json"):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                ts = rec.get("ts", "")
                epoch = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S")) if ts else f.stat().st_mtime
            except Exception:
                continue
            if epoch <= marker:
                continue
            if best is None or epoch > best[0]:
                best = (epoch, rec)
    except Exception:
        return None
    return best


def _read_marker() -> float:
    try:
        return float(json.loads(_TOLD_STATE.read_text(encoding="utf-8")).get("last_told_epoch", 0.0))
    except Exception:
        return 0.0


def _write_marker(epoch: float) -> None:
    try:
        _TOLD_STATE.parent.mkdir(parents=True, exist_ok=True)
        _TOLD_STATE.write_text(json.dumps({"last_told_epoch": epoch,
                                           "last_told_at": time.strftime("%Y-%m-%dT%H:%M:%S")}),
                               encoding="utf-8")
    except Exception:
        pass


def on_pre_llm_call(session_id: str = "", is_first_turn: bool = False, **kw):
    """On the first call of a new session, report the last abort once."""
    if not is_first_turn:
        return None

    marker = _read_marker()
    found = _latest_abort_since(marker)
    if not found:
        return None

    epoch, rec = found
    _write_marker(epoch)

    where = rec.get("session_id", "?")
    when = rec.get("ts", "?")
    chars = rec.get("chars_generated", "?")
    reason = rec.get("reason", "?")
    detail = rec.get("detail", "")

    return {"context": (
        "[The previous generation was aborted by the loop breaker]\n"
        f"  time: {when}\n"
        f"  session: {where}\n"
        f"  reason: {reason} - {detail}\n"
        f"  generated before abort: {chars} chars\n"
        "This was not an error: the guard worked as designed. That turn was stuck in a "
        "repetition loop and could not stop itself, so it was stopped from outside. The "
        "previous output may be incomplete - check what exists and what is missing before "
        "continuing, and if the old context was very long, split the work into a fresh "
        "session instead of resuming it."
    )}
