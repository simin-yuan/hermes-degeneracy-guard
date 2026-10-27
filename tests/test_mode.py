"""Tests for observe/act mode. Run with: python -m pytest -q"""

import json

import _pkg
import pytest

mode_mod = _pkg.submodule("mode")
tag = _pkg.submodule("tool_arg_guard")
guard = _pkg.load()


@pytest.fixture(autouse=True)
def _resample(monkeypatch):
    """Mode is sampled once per process by design; each test models a different install."""
    monkeypatch.setattr(mode_mod, "_FRESH_AT_IMPORT", None, raising=False)


def _payload():
    return {"content": "\n".join(["same line"] * 30)}


def _install(tmp_path, monkeypatch, *, ran_before: bool):
    """Point the plugin at a data directory, optionally one that already exists."""
    home = tmp_path / "home"
    if ran_before:
        home.mkdir()
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(home))
    monkeypatch.delenv("DEGENERACY_GUARD_MODE", raising=False)
    return home


def _seed_incident(home):
    d = home / "incidents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "identical_run_seed.json").write_text("{}", encoding="utf-8")


def _hermes_install(tmp_path, monkeypatch, *, ran_before: bool):
    """Point the plugin at Hermes' real storage shape, where resolving the path creates it.

    ``DEGENERACY_GUARD_HOME`` short-circuits that mkdir, so a test built on it cannot see the
    first-install bug: ``plugin_data_dir`` creates the directory as soon as anything asks for it,
    which means the mode decision has to be sampled before the pin lookup happens.
    """
    import sys
    import types

    home = tmp_path / "hermes-home"
    data = home / "plugin-data" / "degeneracy-guard"
    if ran_before:
        data.mkdir(parents=True)

    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: home
    plugins_pkg = types.ModuleType("plugins")
    plugins_pkg.__path__ = []
    storage = types.ModuleType("plugins.plugin_storage")

    def plugin_data_dir(name):
        path = home / "plugin-data" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    storage.plugin_data_dir = plugin_data_dir
    monkeypatch.setitem(sys.modules, "plugins", plugins_pkg)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)
    monkeypatch.delenv("DEGENERACY_GUARD_HOME", raising=False)
    monkeypatch.delenv("DEGENERACY_GUARD_MODE", raising=False)
    monkeypatch.setattr(mode_mod, "_FRESH_AT_IMPORT", None, raising=False)
    return data


# --- resolution -------------------------------------------------------------------------------


def test_env_act_wins_on_a_fresh_install(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, ran_before=False)
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "act")
    assert mode_mod.mode() == "act"
    assert not mode_mod.observes()


def test_env_observe_wins_over_existing_state(tmp_path, monkeypatch):
    home = _install(tmp_path, monkeypatch, ran_before=True)
    _seed_incident(home)
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "observe")
    assert mode_mod.mode() == "observe"
    assert mode_mod.observes()


def test_fresh_install_starts_in_observe(tmp_path, monkeypatch):
    """A first install on real Hermes storage: nothing existed, so nothing is pinned act."""
    data = _hermes_install(tmp_path, monkeypatch, ran_before=False)
    assert mode_mod.mode() == "observe"
    assert mode_mod.observes()
    pinned = json.loads((data / "mode.json").read_text(encoding="utf-8"))
    assert pinned["mode"] == "observe" and "first install" in pinned["reason"]


def test_install_with_history_keeps_act(tmp_path, monkeypatch):
    """An upgrade must not silently remove a protection the user already chose."""
    home = _install(tmp_path, monkeypatch, ran_before=True)
    _seed_incident(home)
    assert mode_mod.mode() == "act"


def test_upgrade_keeps_act_for_an_install_that_never_flagged_anything(tmp_path, monkeypatch):
    """The population 1.4.0 got wrong: enforcing for weeks, empty incident log.

    Nothing in the incident directory can tell this apart from a fresh install, so the decision is
    made from the data directory having existed before the package loaded, and it is written down so
    an incident arriving afterwards cannot re-label the install.
    """
    home = _hermes_install(tmp_path, monkeypatch, ran_before=True)
    assert mode_mod.mode() == "act"
    pinned = json.loads((home / "mode.json").read_text(encoding="utf-8"))
    assert pinned["mode"] == "act" and "existing install" in pinned["reason"]
    _seed_incident(home)
    assert mode_mod.mode() == "act"


def test_first_install_pins_observe_and_does_not_flip(tmp_path, monkeypatch):
    """A first install must not become act just because it eventually flags something."""
    home = _install(tmp_path, monkeypatch, ran_before=False)
    assert mode_mod.mode() == "observe"
    _seed_incident(home)
    monkeypatch.setattr(mode_mod, "_FRESH_AT_IMPORT", False, raising=False)
    assert mode_mod.mode() == "observe", "the pin has to outrank evidence that arrived later"
    assert json.loads((home / "mode.json").read_text(encoding="utf-8"))["mode"] == "observe"


def test_unreadable_state_is_read_as_has_run_before(tmp_path, monkeypatch):
    """An unreadable history is not a fresh install: fail toward keeping the guard."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "incidents").write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(blocked))
    monkeypatch.delenv("DEGENERACY_GUARD_MODE", raising=False)
    assert mode_mod.mode() == "act"


def test_nonsense_value_is_ignored_not_guessed(tmp_path, monkeypatch):
    home = _install(tmp_path, monkeypatch, ran_before=True)
    _seed_incident(home)
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "obsorve")
    assert mode_mod.mode() == "act"


def test_describe_names_the_source(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, ran_before=False)
    assert "not yet decided" in mode_mod.describe()
    mode_mod.mode()
    assert mode_mod.describe().startswith("observe (pinned:")
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "act")
    assert mode_mod.describe().startswith("act (from DEGENERACY_GUARD_MODE)")


# --- behaviour --------------------------------------------------------------------------------


def test_observe_records_the_tool_call_without_asking(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, ran_before=False)
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "observe")
    assert tag.on_pre_tool_call(tool_name="write_file", args=_payload()) is None
    files = list((tmp_path / "home" / "incidents").glob("*.json"))
    assert len(files) == 1
    inc = json.loads(files[0].read_text(encoding="utf-8"))
    assert inc["action"] == "observed"
    assert inc["approval_outcome"] == "not requested (observe mode)"
    assert inc["reason"]


def test_act_still_asks(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, ran_before=True)
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "act")
    r = tag.on_pre_tool_call(tool_name="write_file", args=_payload())
    assert r["action"] == "approve"
    assert r["rule_key"] == "degeneracy-guard:write_file"
    inc = json.loads(next((tmp_path / "home" / "incidents").glob("*.json")).read_text(encoding="utf-8"))
    assert inc["action"] == "approve_requested"


def test_observe_lets_degenerate_output_through_but_keeps_the_record(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, ran_before=False)
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "observe")
    text = "\n".join(["same line"] * 20)
    guard.on_stream_delta(delta=text, kind="text", session_id="s", turn_id="t")
    out = guard.transform_llm_output(response_text=text, session_id="s", turn_id="t")
    assert out is None, "observe mode must not replace the output"
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "home" / "incidents").glob("*.json")]
    # Two records share the key: the detection itself (no action yet) and the decision taken on it.
    acted = [r for r in rows if r.get("action")]
    assert acted and all(r["action"] == "observed" for r in acted)
    assert any(r.get("would_replace_chars") for r in rows)


def test_act_replaces_and_says_so(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, ran_before=True)
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "act")
    text = "\n".join(["same line"] * 20)
    guard.on_stream_delta(delta=text, kind="text", session_id="s2", turn_id="t2")
    out = guard.transform_llm_output(response_text=text, session_id="s2", turn_id="t2")
    assert out is not None and out != text
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "home" / "incidents").glob("*.json")]
    assert any(r.get("replaced_by_transform_llm_output") for r in rows)


def test_observe_detects_the_same_things_as_act(tmp_path, monkeypatch):
    """A mode that skips the work would not be an observe mode."""
    text = "\n".join(["same line"] * 20)
    reasons = {}
    for m in ("observe", "act"):
        home = tmp_path / m
        home.mkdir()
        monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(home))
        monkeypatch.setenv("DEGENERACY_GUARD_MODE", m)
        guard.on_stream_delta(delta=text, kind="text", session_id=m, turn_id="t")
        guard.transform_llm_output(response_text=text, session_id=m, turn_id="t")
        rows = [json.loads(p.read_text(encoding="utf-8")) for p in (home / "incidents").glob("*.json")]
        reasons[m] = sorted({r["reason"] for r in rows if r.get("reason")})
    assert reasons["observe"] == reasons["act"] == ["identical_run"]
