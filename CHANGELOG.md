# Changelog

## 1.4.1 — 2026-10-28

**Fixed: 1.4.0 could turn the guard off on upgrade, without saying so.**

1.4.0 decided its mode from the incident log: `act` if this install had ever written an incident,
`observe` if not. That reads correctly only for an install that has been catching things. An install
that had been enforcing for weeks and had never flagged anything has an empty incident directory, so
1.4.0 would have read it as a first install and quietly stopped replacing degenerate output and stopped
asking about loop-shaped tool calls. The user would have gone on believing the guard was there. That is
the worst failure this plugin can have, so the rule changed before anyone could upgrade into it.

The decision now comes from the install itself rather than from its history of findings: the plugin's
data directory is sampled once, when the package is imported — before any hook in this package can
create it. Directory already on disk means this plugin has run here before, and the install keeps
`act`. Directory absent means a first install, and it starts in `observe`.

The answer is then written to `mode.json` in that data directory and read from there afterwards. It
cannot move under the user later: a first incident appearing does not make a first install look old,
and a first install does not become `act` just because it eventually flags something.

`DEGENERACY_GUARD_MODE` overrides both directions, is never pinned, and still ignores anything that is
not exactly `observe` or `act` — a typo must not silently disable a guard.

**Also fixed before release: the pin lookup created the directory it was asking about.**

`mode.json` was resolved through `plugin_data_dir()`, which creates its directory on the way.
So the very first `mode()` call built the data directory, then sampled it for "did this exist before
I arrived" and answered yes — a genuine first install read itself as an existing one and pinned `act`.
The pin path is now resolved without the mkdir, and the freshness answer is taken when the package is
imported, before anything in it can write. A first install on real Hermes storage is `observe`, which
is what the tests now assert against the real storage shape rather than the environment override.
Tests: 50 passed, 1 skipped (`python -m pytest tests -q`). Two of them are the ones this change exists
for: an install that never flagged anything stays `act` across the upgrade, and a first install stays
`observe` even after it records its first incident.

## 1.4.0 — 2026-10-27

Added `DEGENERACY_GUARD_MODE=observe`: the same detectors, the same incident record, no directive.
Incidents written in observe mode carry `"action": "observed"` so a later reader can tell "would have
asked" from "asked". Its default-mode rule is the one 1.4.1 replaces.
