"""State locations for degeneracy-guard.

One place that decides where incidents and counters live, in this order:

1. ``DEGENERACY_GUARD_HOME`` when set (containers, read-only homes, tests).
2. Inside Hermes, its per-plugin data root ``<hermes home>/plugin-data/degeneracy-guard/``
   (``plugins.plugin_storage.plugin_data_dir``): profile-scoped, kept by
   ``hermes plugins update`` and ``remove``.
3. Outside Hermes (this plugin's own tests, a bare interpreter), ``~/.degeneracy-guard``.

Resolved on every call, never at import: ``plugin_data_dir`` follows the active profile and
creates the directory, so a path cached at import time would write one profile's state into
another's.
"""

from __future__ import annotations

import os
from pathlib import Path

PLUGIN_NAME = "degeneracy-guard"


def state_root() -> Path:
    override = os.environ.get("DEGENERACY_GUARD_HOME")
    if override:
        return Path(override).expanduser()
    try:
        from plugins.plugin_storage import plugin_data_dir
    except ImportError:
        return Path.home() / ".degeneracy-guard"
    return plugin_data_dir(PLUGIN_NAME)


def incident_dir() -> Path:
    return state_root() / "incidents"
