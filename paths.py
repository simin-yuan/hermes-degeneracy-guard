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

Incident file names are unique on purpose: an earlier version stamped them to the second,
and four incidents inside one second left only two files behind.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path

PLUGIN_NAME = "degeneracy-guard"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


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


def state_root_path() -> Path:
    """Where this install keeps its state, resolved without creating the directory.

    ``state_root()`` delegates to ``plugin_data_dir()``, which creates the directory as a side
    effect, so it cannot answer "did this exist before I arrived". This resolves the same location
    without the mkdir — the difference between "a first install" and "an install that has been
    running and has never flagged anything", which is exactly what decides the guard's mode, and
    the path a pin file is looked for at.
    """
    override = os.environ.get("DEGENERACY_GUARD_HOME")
    if override:
        return Path(override).expanduser()
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return Path.home() / ".degeneracy-guard"
    return get_hermes_home() / "plugin-data" / PLUGIN_NAME


def state_root_exists() -> bool:
    """Whether this install's data directory is already on disk, without creating it."""
    return state_root_path().exists()


def incident_path(tag: str = "incident") -> Path:
    """Return a collision-free incident path, creating the directory if needed.

    Whole seconds cannot separate two blocks in one turn, so the stamp carries nanoseconds
    and a short random suffix.
    """
    directory = incident_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S") + f".{time.time_ns() % 1_000_000_000:09d}"
    safe = _UNSAFE.sub("_", tag)[:48] or "incident"
    return directory / f"{stamp}_{safe}_{secrets.token_hex(3)}.json"
