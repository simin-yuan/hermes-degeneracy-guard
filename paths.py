"""State locations for the loop breaker.

One place that decides where incidents and counters live. Default: a dot directory in
the user's home. Override with LOOP_BREAKER_HOME when the runtime keeps its own state
tree (containers, read-only homes, per-project isolation).
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_ROOT = Path(os.environ.get("LOOP_BREAKER_HOME") or (Path.home() / ".loop-breaker")).expanduser()
INCIDENT_DIR = STATE_ROOT / "incidents"


def ensure_state_root() -> Path:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    return STATE_ROOT
