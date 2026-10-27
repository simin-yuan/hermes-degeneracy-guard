"""Incident records must not overwrite each other, and must never store argument text."""

import json
import os
import time

import _pkg

paths = _pkg.submodule("paths")
tag = _pkg.submodule("tool_arg_guard")
init = _pkg.load()


def _payload():
    return {"content": "\n".join(["same line"] * 40)}


def test_four_asks_in_one_second_leave_four_files(tmp_path, monkeypatch):
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(tmp_path))
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "act")
    for _ in range(4):
        assert tag.on_pre_tool_call(tool_name="write_file", args=_payload())["action"] == "approve"
    files = list((tmp_path / "incidents").glob("*.json"))
    assert len(files) == 4, [f.name for f in files]


def test_incident_names_are_unique_even_when_the_clock_is_frozen(monkeypatch, tmp_path):
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(tmp_path))
    frozen = time.time_ns()
    monkeypatch.setattr(paths.time, "time_ns", lambda: frozen)
    seen = {paths.incident_path("TOOLARG_write_file") for _ in range(20)}
    assert len(seen) == 20


def test_incident_records_do_not_contain_argument_text(tmp_path, monkeypatch):
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(tmp_path))
    secret = "SENTINEL-" + "same line\n" * 40
    tag.on_pre_tool_call(tool_name="write_file", args={"content": secret})
    record = json.loads(next((tmp_path / "incidents").glob("*.json")).read_text(encoding="utf-8"))
    assert "SENTINEL" not in json.dumps(record)
    assert record["arg_chars"] > 0
    assert record["reason"] in ("identical_run", "periodic_repeat")


def test_stream_incident_keeps_only_a_bounded_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("DEGENERACY_GUARD_HOME", str(tmp_path))
    text = "\n".join(["same line"] * 120)
    for i in range(0, len(text), 40):
        init.on_stream_delta(delta=text[i:i + 40], kind="text", session_id="s", turn_id="t")
    record = json.loads(next((tmp_path / "incidents").glob("*.json")).read_text(encoding="utf-8"))
    assert len(record["tail_sample"]) <= 800
