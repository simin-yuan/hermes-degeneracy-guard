"""Tests for degeneracy-guard. Run with: python -m pytest -q"""

import sys
import types
from pathlib import Path

import _pkg

ld = _pkg.submodule("loop_detector")
tag = _pkg.submodule("tool_arg_guard")
paths = _pkg.submodule("paths")


def test_identical_lines_trigger():
    v = ld.check_text("\n".join(["same line"] * 20))
    assert v.triggered and v.reason == "identical_run", v


def test_periodic_block_triggers():
    v = ld.check_text("\n".join(["OK.", "Done.", "Write."] * 10))
    assert v.triggered and v.reason == "periodic_repeat", v


def test_legitimate_repetition_does_not_trigger():
    """A code block repeating a handful of times is normal; the detector must stay quiet."""
    block = ["    return value", "}", "", "def f(x):", "    value = x * 2"]
    v = ld.check_text("\n".join(block * 5))
    assert not v.triggered, v


def test_short_loop_fallback():
    v = ld.check_text("\n".join(["a", "b", "b"] * 9))
    assert v.triggered, v


def test_stream_state_latches_once():
    st = ld.StreamBreakerState()
    saw = 0
    for _ in range(40):
        if st.feed("OK.\n") is not None:
            saw += 1
    assert saw == 1


def test_tool_guard_asks_about_degenerate_arguments(monkeypatch):
    monkeypatch.setenv("DEGENERACY_GUARD_MODE", "act")
    r = tag.on_pre_tool_call(tool_name="write_file",
                             args={"content": "\n".join(["same line"] * 30)})
    assert r and r["action"] == "approve"
    assert "DEGENERACY_GUARD_APPROVAL" in r["message"]


def test_tool_guard_allows_normal_arguments():
    r = tag.on_pre_tool_call(tool_name="write_file", args={"content": "print('hello')\n"})
    assert r is None


def test_state_root_follows_the_environment():
    assert str(paths.state_root()).startswith(str(paths.Path.home())) or paths.state_root()
    assert paths.incident_dir() == paths.state_root() / "incidents"


def test_state_root_uses_hermes_plugin_data_dir_on_every_call(monkeypatch, tmp_path):
    """Without an override, state goes to Hermes' per-plugin data root, resolved per call so a
    profile switch is followed."""
    monkeypatch.delenv("DEGENERACY_GUARD_HOME")
    homes = iter([tmp_path / "a", tmp_path / "b"])
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = lambda name: next(homes) / "plugin-data" / name
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    assert paths.state_root() == tmp_path / "a" / "plugin-data" / "degeneracy-guard"
    assert paths.state_root() == tmp_path / "b" / "plugin-data" / "degeneracy-guard"


def test_state_root_outside_hermes_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("DEGENERACY_GUARD_HOME")
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", None)  # import raises ImportError
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.state_root() == tmp_path / ".degeneracy-guard"
