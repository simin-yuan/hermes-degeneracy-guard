"""Tests for degeneracy-guard. Run with: python -m pytest -q"""
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# Keep every write out of the user's home; this must happen before the modules import.
_TMP = tempfile.mkdtemp(prefix="degeneracy-guard-test-")
os.environ["DEGENERACY_GUARD_HOME"] = _TMP

import loop_detector as ld          # noqa: E402
import tool_arg_guard as tag        # noqa: E402
import paths                        # noqa: E402


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


def test_tool_guard_blocks_degenerate_arguments():
    r = tag.on_pre_tool_call(tool_name="write_file",
                             args={"content": "\n".join(["same line"] * 30)})
    assert r and r["action"] == "block"
    assert "DEGENERACY_GUARD_BLOCK" in r["message"]


def test_tool_guard_allows_normal_arguments():
    r = tag.on_pre_tool_call(tool_name="write_file", args={"content": "print('hello')\n"})
    assert r is None


def test_state_root_follows_the_environment():
    assert str(paths.STATE_ROOT) == _TMP
