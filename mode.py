"""Whether the guard acts, or only records what it would have done.

Two modes, and the plugin's own state decides which one a given install gets:

``observe``
    Every detection path runs exactly as it does in ``act`` mode: the same thresholds, the same
    incident record. The only difference is that no directive is returned - degenerated output is not
    replaced and a suspicious tool call is not escalated. Incidents are written with
    ``"action": "observed"`` so a later reader can tell "would have asked" from "asked".

``act``
    The guard does what it detects: flagged output is replaced, a loop-shaped tool call goes to the
    approval gate.

Why the default is not simply one value:

- A **first install** starts in ``observe``. Nobody should hand a veto to a detector they have not
  watched work.
- An **existing install** keeps ``act``. Flipping to ``observe`` in an upgrade would silently remove a
  protection the user already chose, and they would find out only when a loop ran to completion.

How "existing" is decided, and why the incident log cannot decide it
--------------------------------------------------------------------
1.4.0 asked "has this install ever written an incident?" That question is wrong for the population it
matters most for: an install that has been enforcing for weeks and has never caught anything has an
empty incident directory, so an upgrade would read it as a first install and quietly stop enforcing.
A guard that fails silently on upgrade is worse than one that was never installed, because the user
keeps believing it is there.

The evidence used instead is the install's own data directory, sampled **once at import**, before any
hook in this package can create it:

- directory already on disk -> this plugin has run here before -> ``act``
- directory absent at import -> nothing has ever run here -> ``observe``

The answer is then **pinned** to ``mode.json`` in that directory, so it cannot move under the user
later: new incidents do not make a first install look old, and a first install does not flip to
``act`` on its second load. ``DEGENERACY_GUARD_MODE`` overrides everything in either direction and is
never pinned. Its values are ``observe`` and ``act``; anything else is ignored rather than guessed at.

The shared key
--------------
``HERMES_GUARD_MODE`` is read by every plugin in this family, so one setting turns enforcement on for
all of them instead of three. It is consulted only when ``DEGENERACY_GUARD_MODE`` says nothing, and it
does **not** move the pin: the pin records what this install decided on its own, and an operator's
explicit setting is a different question that is answered fresh on every read.

Its third value, ``ask``, maps to ``act`` here rather than to a separate mode: this guard's ``act``
path already sends a loop-shaped tool call to the approval gate, so the only difference between the two
names is what happens after a timeout, and this plugin refuses there by design. That refusal is
documented rather than silently relaxed — a guard that fails open on timeout is not a guard.
"""

from __future__ import annotations

import json
import os
import time

from . import paths

MODE_OBSERVE = "observe"
MODE_ACT = "act"
ENV = "DEGENERACY_GUARD_MODE"
SHARED_ENV = "HERMES_GUARD_MODE"
PIN_FILE = "mode.json"

#: The shared key's vocabulary, mapped onto this plugin's two modes. ``act`` and ``block`` are what
#: other plugins in the family call enforcement; ``ask`` maps to ``act`` for the reason in the module
#: docstring. Anything absent from this table is ignored rather than guessed at.
_SHARED_MODES = {
    MODE_OBSERVE: MODE_OBSERVE,
    MODE_ACT: MODE_ACT,
    "block": MODE_ACT,
    "ask": MODE_ACT,
}


def _from_env() -> tuple[str | None, str | None]:
    """``(mode, key it came from)`` from an explicit setting, own key before shared key.

    Own key first so nothing that is configured today changes behaviour when the shared key appears.
    """
    value = os.environ.get(ENV, "").strip().lower()
    if value in (MODE_OBSERVE, MODE_ACT):
        return value, ENV
    shared = os.environ.get(SHARED_ENV, "").strip().lower()
    if shared in _SHARED_MODES:
        return _SHARED_MODES[shared], SHARED_ENV
    return None, None

#: Sampled once, when this package is imported - before any hook can write state, and before
#: anything here resolves a path in a way that would create the directory. Tests set it directly
#: to model an install of a given age without touching the real home directory.
_FRESH_AT_IMPORT: bool | None = not paths.state_root_exists()


def _pin_path():
    # Resolved without mkdir: looking for a pin must not create the directory that decides the
    # mode, or a first install would read its own pin lookup as "has run here before".
    return paths.state_root_path() / PIN_FILE


def _read_pin() -> dict | None:
    try:
        pinned = json.loads(_pin_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(pinned, dict) and pinned.get("mode") in (MODE_OBSERVE, MODE_ACT):
        return pinned
    return None


def _write_pin(value: str, reason: str) -> None:
    try:
        path = _pin_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "mode": value,
            "reason": reason,
            "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # an unwritable state directory must not become a crash on every turn


def _fresh() -> bool:
    """True when nothing of this plugin existed here when the package was imported."""
    global _FRESH_AT_IMPORT
    if _FRESH_AT_IMPORT is None:
        _FRESH_AT_IMPORT = not paths.state_root_exists()
    return _FRESH_AT_IMPORT


def mode() -> str:
    forced, _origin = _from_env()
    if forced:
        return forced
    pinned = _read_pin()
    if pinned:
        return pinned["mode"]
    if _fresh():
        _write_pin(MODE_OBSERVE, "first install: nothing existed here when the plugin loaded")
        return MODE_OBSERVE
    _write_pin(MODE_ACT, "existing install: the plugin's data directory predates this load")
    return MODE_ACT


def observes() -> bool:
    return mode() == MODE_OBSERVE


def describe() -> str:
    """One line for a status readout: the mode and where it came from."""
    forced, origin = _from_env()
    if forced:
        if origin == SHARED_ENV:
            asked = os.environ.get(SHARED_ENV, "").strip().lower()
            mapped = f" (asked for {asked!r}; the act path already goes through the approval gate)" \
                if asked not in (MODE_OBSERVE, MODE_ACT) else ""
            return f"{forced} (from {SHARED_ENV}){mapped}"
        return f"{forced} (from {ENV})"
    pinned = _read_pin()
    if pinned:
        return f"{pinned['mode']} (pinned: {pinned.get('reason', 'decided earlier')})"
    return "not yet decided; the next check records it in " + PIN_FILE


# Sampled here, at import, so the answer describes the install as it was before this package ran.
_fresh()
