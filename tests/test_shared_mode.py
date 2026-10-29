"""The shared HERMES_GUARD_MODE key.

One setting across the plugin family. Consulted only when this plugin's own key says nothing, and it
never moves the pin: the pin records what an install decided on its own, while an operator's explicit
setting is a separate question answered fresh on every read.
"""

import json

import _pkg
import pytest

mode_mod = _pkg.submodule("mode")
_pkg.load()

OWN = "DEGENERACY_GUARD_MODE"
SHARED = "HERMES_GUARD_MODE"


@pytest.fixture(autouse=True)
def _resample(monkeypatch):
    monkeypatch.setattr(mode_mod, "_FRESH_AT_IMPORT", None, raising=False)
    monkeypatch.delenv(OWN, raising=False)
    monkeypatch.delenv(SHARED, raising=False)


def _install(tmp_path, monkeypatch, *, ran_before: bool):
    home = tmp_path / "home"
    if ran_before:
        home.mkdir()
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(home))
    return home


# --- the shared key decides when the own key is silent -----------------------------------------


def test_shared_observe_overrides_an_install_that_would_pin_act(tmp_path, monkeypatch):
    """An install with history would pin act; an explicit shared observe is still obeyed."""
    _install(tmp_path, monkeypatch, ran_before=True)
    monkeypatch.setenv(SHARED, "observe")
    assert mode_mod.mode() == "observe"
    assert mode_mod.observes()


def test_shared_act_wins_on_a_fresh_install(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, ran_before=False)
    monkeypatch.setenv(SHARED, "act")
    assert mode_mod.mode() == "act"
    assert not mode_mod.observes()


def test_shared_ask_maps_to_act_and_says_so(tmp_path, monkeypatch):
    """This guard's act path already goes through the approval gate, so ask lands there."""
    _install(tmp_path, monkeypatch, ran_before=False)
    monkeypatch.setenv(SHARED, "ask")
    assert mode_mod.mode() == "act"
    assert mode_mod.describe().startswith("act")
    assert "approval gate" in mode_mod.describe()


def test_shared_block_is_accepted_as_enforcement(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, ran_before=False)
    monkeypatch.setenv(SHARED, "block")
    assert mode_mod.mode() == "act"


# --- precedence -------------------------------------------------------------------------------


def test_own_key_wins_over_the_shared_key(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, ran_before=True)
    monkeypatch.setenv(OWN, "observe")
    monkeypatch.setenv(SHARED, "act")
    assert mode_mod.mode() == "observe"


def test_junk_shared_value_falls_through_to_the_pin(tmp_path, monkeypatch):
    """A misspelled family setting must not become enforcement."""
    _install(tmp_path, monkeypatch, ran_before=True)
    monkeypatch.setenv(SHARED, "enforce")
    assert mode_mod.mode() == "act"          # act here comes from the pin, not from the junk value


def test_unknown_family_setting_is_ignored(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, ran_before=False)
    monkeypatch.setenv(SHARED, "obsorve")
    assert mode_mod.mode() == "observe"      # the fresh-install default


# --- the pin is not disturbed ----------------------------------------------------------------


def test_a_shared_override_never_writes_the_pin(tmp_path, monkeypatch):
    home = _install(tmp_path, monkeypatch, ran_before=False)
    monkeypatch.setenv(SHARED, "act")
    assert mode_mod.mode() == "act"
    assert not (home / "mode.json").exists(), "an env override must not look like this install's decision"


def test_the_pin_survives_after_the_override_is_removed(tmp_path, monkeypatch):
    home = _install(tmp_path, monkeypatch, ran_before=False)
    monkeypatch.setenv(SHARED, "act")
    assert mode_mod.mode() == "act"
    monkeypatch.delenv(SHARED)
    assert mode_mod.mode() == "observe"      # the install's own decision, still a first install
    assert json.loads((home / "mode.json").read_text(encoding="utf-8"))["mode"] == "observe"
