"""Whether the guard acts, or only records what it would have done.

Two modes, and the plugin's own state decides which one a given install gets:

``observe``
    Every detection path runs exactly as it does in ``act`` mode: the same thresholds, the same
    incident record. The only difference is that no directive is returned — degenerated output is not
    replaced and a suspicious tool call is not escalated. Incidents are written with
    ``"action": "observed"`` so a later reader can tell "would have asked" from "asked".

``act``
    The guard does what it detects: flagged output is replaced, a loop-shaped tool call goes to the
    approval gate.

Why the default is not simply one value:

- A **first install** starts in ``observe``. Nobody should hand a veto to a detector they have not
  watched work; the whole point of the first day is to see what it flags on your own traffic.
- An **existing install** keeps ``act``. Flipping to ``observe`` in an upgrade would silently remove a
  protection the user already chose, and the user would find out only when a loop ran to completion.
  "Existing" is decided by evidence, not by a version number: if this plugin has ever written an
  incident, it has been running, and it keeps its posture.

``DEGENERACY_GUARD_MODE`` overrides both, in either direction. Values are ``observe`` and ``act``;
anything else is ignored rather than guessed at.
"""

from __future__ import annotations

import os

from .paths import incident_dir

MODE_OBSERVE = "observe"
MODE_ACT = "act"
ENV = "DEGENERACY_GUARD_MODE"


def _has_run_before() -> bool:
    """True when this install has produced state, i.e. it is not a first run.

    Deliberately cheap and deliberately tolerant: a permission error while listing the directory
    counts as "has run before", because the safe reading of an unreadable history is not "fresh
    install, downgrade to observe".
    """
    try:
        directory = incident_dir()
        with os.scandir(directory) as entries:
            return any(True for _ in entries)
    except FileNotFoundError:
        return False
    except Exception:
        return True


def mode() -> str:
    value = os.environ.get(ENV, "").strip().lower()
    if value in (MODE_OBSERVE, MODE_ACT):
        return value
    return MODE_ACT if _has_run_before() else MODE_OBSERVE


def observes() -> bool:
    return mode() == MODE_OBSERVE


def describe() -> str:
    """One line for a status readout: the mode and, when it is set, where it came from."""
    value = os.environ.get(ENV, "").strip().lower()
    if value in (MODE_OBSERVE, MODE_ACT):
        return f"{value} (from {ENV})"
    if _has_run_before():
        return f"{MODE_ACT} (default: this install has recorded incidents before)"
    return f"{MODE_OBSERVE} (default: first run — nothing has been recorded yet)"
