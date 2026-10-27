"""Tests for observe/act mode. Run with: python -m pytest -q"""

import _pkg

mode_mod = _pkg.submodule("mode")
tag = _pkg.submodule("tool_arg_guard")
guard = _pkg.load()


def _payload():
    return {"content": "\n".join(["same line"] * 30)}


def _seed_incident(home):
    """Make this install look like it has run before."""
    d = home / "incidents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "identical_run_seed.json").write_text("{}", encoding="utf-8")


# --- resolution -------------------------------------------------------------------------------


def test_env_act_wins_on_a_fresh_install(tmp_path, monkeypatch):
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(tmp_path))
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "act")
    assert mode_mod.mode() == "act"
    assert not mode_mod.observes()


def test_env_observe_wins_over_existing_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(tmp_path))
    _seed_incident(tmp_path)
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "observe")
    assert mode_mod.mode() == "observe"
    assert mode_mod.observes()


def test_fresh_install_starts_in_observe(tmp_path, monkeypatch):
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(tmp_path))
    monkeypatch.delenv("DEGENERACY_GUARD_MODE", raising=False)
    assert mode_mod.mode() == "observe"


def test_install_with_history_keeps_act(tmp_path, monkeypatch):
    """An upgrade must not silently remove a protection the user already chose."""
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(tmp_path))
    _seed_incident(tmp_path)
    monkeypatch.delenv("DEGENERACY_GUARD_MODE", raising=False)
    assert mode_mod.mode() == "act"


def test_unreadable_state_is_read_as_has_run_before(tmp_path, monkeypatch):
    """An unreadable history is not a fresh install: fail toward keeping the guard."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "incidents").write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(blocked))
    monkeypatch.delenv("DEGENERACY_GUARD_MODE", raising=False)
    assert mode_mod.mode() == "act"


def test_nonsense_value_is_ignored_not_guessed(tmp_path, monkeypatch):
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(tmp_path))
    _seed_incident(tmp_path)
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "obsorve")
    assert mode_mod.mode() == "act"


def test_describe_names_the_source(tmp_path, monkeypatch):
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(tmp_path))
    monkeypatch.delenv("DEGENERACY_GUARD_MODE", raising=False)
    assert "first run" in mode_mod.describe()
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "act")
    assert mode_mod.describe().startswith("act (from DEGENERACY_GUARD_MODE)")


# --- behaviour --------------------------------------------------------------------------------


def test_observe_records_the_tool_call_without_asking(tmp_path, monkeypatch):
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(tmp_path))
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "observe")
    assert tag.on_pre_tool_call(tool_name="write_file", args=_payload()) is None
    files = list((tmp_path / "incidents").glob("*.json"))
    assert len(files) == 1
    import json
    inc = json.loads(files[0].read_text(encoding="utf-8"))
    assert inc["action"] == "observed"
    assert inc["approval_outcome"] == "not requested (observe mode)"
    # The detection still ran: the record has the verdict, not just a shrug.
    assert inc["reason"]


def test_act_still_asks(tmp_path, monkeypatch):
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(tmp_path))
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "act")
    r = tag.on_pre_tool_call(tool_name="write_file", args=_payload())
    assert r["action"] == "approve"
    assert r["rule_key"] == "degeneracy-guard:write_file"
    import json
    inc = json.loads(next((tmp_path / "incidents").glob("*.json")).read_text(encoding="utf-8"))
    assert inc["action"] == "approve_requested"


def test_observe_lets_degenerate_output_through_but_keeps_the_record(tmp_path, monkeypatch):
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(tmp_path))
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "observe")
    text = "\n".join(["same line"] * 20)
    guard.on_stream_delta(delta=text, kind="text", session_id="s", turn_id="t")
    out = guard.transform_llm_output(response_text=text, session_id="s", turn_id="t")
    assert out is None, "observe mode must not replace the output"
    import json
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "incidents").glob("*.json")]
    assert rows and all(r.get("action") == "observed" for r in rows)
    assert any(r.get("would_replace_chars") for r in rows)


def test_act_replaces_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(tmp_path))
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "act")
    text = "\n".join(["same line"] * 20)
    guard.on_stream_delta(delta=text, kind="text", session_id="s2", turn_id="t2")
    out = guard.transform_llm_output(response_text=text, session_id="s2", turn_id="t2")
    assert out is not None and out != text
    import json
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "incidents").glob("*.json")]
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
        import json
        rows = [json.loads(p.read_text(encoding="utf-8")) for p in (home / "incidents").glob("*.json")]
        reasons[m] = sorted({r["reason"] for r in rows if r.get("reason")})
    assert reasons["observe"] == reasons["act"] == ["identical_run"]
