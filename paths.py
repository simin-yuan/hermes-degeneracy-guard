"""State locations for degeneracy-guard.

One place that decides where incidents and counters live. Default: a dot directory in
the user's home. Override with DEGENERACY_GUARD_HOME when the runtime keeps its own state
tree (containers, read-only homes, per-project isolation).
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_ROOT = Path(os.environ.get("DEGENERACY_GUARD_HOME") or (Path.home() / ".degeneracy-guard")).expanduser()
INCIDENT_DIR = STATE_ROOT / "incidents"


def ensure_state_root() -> Path:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    return STATE_ROOT
