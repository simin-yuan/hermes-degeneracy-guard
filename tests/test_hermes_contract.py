"""Contract tests against the Hermes plugin API.

Every expectation below was read from NousResearch/hermes-agent at commit
9e01b44dd23978f9ff40b7837467f3a9f1582130 (main, 2026-10-18). File:line references are to
that commit. If upstream moves, re-read those lines before changing a test.
"""
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
# Plugin modules are imported inside the tests, not here: paths.STATE_ROOT is fixed at first
# import, and test_degeneracy_guard.py sets its own DEGENERACY_GUARD_HOME during collection.
os.environ.setdefault("DEGENERACY_GUARD_HOME", tempfile.mkdtemp(prefix="degeneracy-guard-test-"))

UPSTREAM_COMMIT = "9e01b44dd23978f9ff40b7837467f3a9f1582130"

# The subset of VALID_HOOKS this plugin uses (hermes_cli/plugins.py:107-113).
UPSTREAM_VALID_HOOKS_USED = {
    "pre_tool_call", "transform_llm_output", "pre_llm_call", "post_llm_call", "on_stream_delta",
}

# Exact keyword arguments Hermes passes to each hook. invoke_hook also adds
# telemetry_schema_version to every payload (hermes_cli/plugins_dispatch.py:272-273;
# agent/plugin_stream_hooks.py:68 for stream hooks).
UPSTREAM_KWARGS = {
    # hermes_cli/plugins.py:1995-1999 (_get_pre_tool_call_directive_details)
    "pre_tool_call": {"tool_name", "args", "task_id", "session_id", "tool_call_id", "turn_id",
                      "api_request_id", "middleware_trace"},
    # agent/stream_delivery.py:332 (delta, kind) + :282-290 (_stream_hook_base_payload)
    "on_stream_delta": {"delta", "kind", "turn_id", "iteration", "session_id", "model", "provider",
                        "surface"},
    # agent/turn_finalizer.py:510-517 (apply_llm_output_transform)
    "transform_llm_output": {"response_text", "session_id", "model", "platform", "turn_id"},
    # agent/turn_finalizer.py:469-479 (post_llm_call)
    "post_llm_call": {"session_id", "task_id", "turn_id", "user_message", "assistant_response",
                      "conversation_history", "model", "platform"},
    # agent/turn_context.py:810-822 (pre_llm_call)
    "pre_llm_call": {"session_id", "task_id", "turn_id", "user_message", "conversation_history",
                     "is_first_turn", "model", "platform", "parent_session_id", "sender_id"},
}

_SAMPLE = {
    "tool_name": "write_file", "args": {"content": "x"}, "task_id": "", "session_id": "s",
    "tool_call_id": "c", "turn_id": "t", "api_request_id": "", "middleware_trace": [],
    "delta": "hello", "kind": "text", "iteration": 1, "model": "m", "provider": "p",
    "surface": "cli", "response_text": "hello", "platform": "cli", "user_message": "hi",
    "assistant_response": "hello", "conversation_history": [], "is_first_turn": False,
    "parent_session_id": "", "sender_id": "", "telemetry_schema_version": 1,
}


class _Ctx:
    def __init__(self):
        self.hooks = []

    def register_hook(self, name, cb):
        self.hooks.append((name, cb))


def _load_plugin_like_hermes(name="hermes_plugins.degeneracy_guard_test"):
    """Import __init__.py the way hermes_cli/plugins_loader.py:739-748 does."""
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _registered():
    ctx = _Ctx()
    _load_plugin_like_hermes().register(ctx)
    return ctx.hooks


def _manifest_hooks():
    out, in_hooks = [], False
    for line in (_ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("provides_hooks:"):
            in_hooks = True
        elif in_hooks and line.strip().startswith("- "):
            out.append(line.strip()[2:].strip())
        elif in_hooks and line and not line.startswith(" "):
            break
    return out


def test_manifest_uses_provides_hooks_not_legacy_hooks_key():
    """At 22063fc0 the loader accepts the legacy "hooks:" alias (hermes_cli/plugins_manifest.py:551),
    but `hermes plugins validate` only reads provides_hooks (hermes_cli/plugin_validate.py:381-385)."""
    keys = [line.split(":", 1)[0] for line in
            (_ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines()
            if line and not line[0].isspace() and ":" in line]
    assert "hooks" not in keys
    assert "provides_hooks" in keys


def test_registered_hooks_match_manifest_and_valid_hooks():
    names = {n for n, _ in _registered()}
    assert names == set(_manifest_hooks())
    assert names <= UPSTREAM_VALID_HOOKS_USED


@pytest.mark.parametrize("hook", sorted(UPSTREAM_KWARGS))
def test_callbacks_accept_exact_upstream_kwargs(hook):
    sent = UPSTREAM_KWARGS[hook] | {"telemetry_schema_version"}
    callbacks = [cb for n, cb in _registered() if n == hook]
    assert callbacks
    for cb in callbacks:
        params = inspect.signature(cb).parameters
        # A **kwargs callback receives the full payload (hermes_cli/plugins_dispatch.py:231-244).
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()), cb
        named = {k for k, p in params.items() if p.kind is not inspect.Parameter.VAR_KEYWORD}
        assert named <= sent, f"{cb.__qualname__} reads {named - sent}, which Hermes never sends"
        cb(**{k: _SAMPLE[k] for k in sent})  # must not raise


def test_block_return_shape_matches_upstream():
    """hermes_cli/plugins.py:2002-2023: only a dict with action == "block" and a non-empty str
    message vetoes the call; the message becomes the tool result. Executors return before
    dispatch on a block: model_tools.py:787-788 and :941-944, agent/tool_executor.py:739-745,
    agent/agent_runtime_helpers.py:2540-2541."""
    import tool_arg_guard
    r = tool_arg_guard.on_pre_tool_call(
        tool_name="write_file", args={"content": "\n".join(["same line"] * 30)},
        task_id="", session_id="s", tool_call_id="c", turn_id="t", api_request_id="",
        middleware_trace=[], telemetry_schema_version=1)
    assert isinstance(r, dict)
    assert set(r) == {"action", "message"}
    assert r["action"] == "block"
    assert isinstance(r["message"], str) and r["message"]


def test_pre_llm_call_return_shape():
    """agent/turn_context.py:835-836: a dict with a truthy "context" is injected."""
    import resume_notice
    import session_length_guard
    r = session_length_guard.on_pre_llm_call(
        session_id="contract", conversation_history=[{"content": "x"}] * 600)
    assert isinstance(r, dict) and isinstance(r.get("context"), str) and r["context"]
    assert resume_notice.on_pre_llm_call(is_first_turn=False) is None


def test_plugin_loads_without_its_directory_on_sys_path(tmp_path):
    """The loader does not add the plugin directory to sys.path (plugins_loader.py:739-748).
    Run in a clean interpreter so this test's own sys.path cannot hide a failure."""
    code = (
        "import importlib.util, sys\n"
        f"root = {str(_ROOT)!r}\n"
        "assert root not in sys.path\n"
        "spec = importlib.util.spec_from_file_location('hermes_plugins.dg', root + '/__init__.py',"
        " submodule_search_locations=[root])\n"
        "m = importlib.util.module_from_spec(spec); sys.modules['hermes_plugins.dg'] = m\n"
        "spec.loader.exec_module(m)\n"
        "class C:\n"
        "    n = []\n"
        "    def register_hook(self, h, cb): self.n.append(h)\n"
        "c = C(); m.register(c); print(','.join(c.n))\n"
    )
    env = {**os.environ, "DEGENERACY_GUARD_HOME": str(tmp_path)}
    env.pop("PYTHONPATH", None)
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert set(out.stdout.strip().split(",")) == set(_manifest_hooks())


_HERMES = importlib.util.find_spec("hermes_cli") is not None


@pytest.mark.skipif(not _HERMES, reason="hermes-agent is not installed (optional integration test)")
def test_real_hermes_plugin_manager_blocks_degenerate_tool_call(tmp_path):
    """Load the plugin through Hermes' own discovery and drive pre_tool_call through the
    function every executor calls (_dispatch_pre_tool_call_hooks, plugins.py:2091-2099)."""
    home = tmp_path / "hermes"
    dest = home / "plugins" / "degeneracy-guard"
    dest.mkdir(parents=True)
    for f in list(_ROOT.glob("*.py")) + [_ROOT / "plugin.yaml"]:
        (dest / f.name).write_bytes(f.read_bytes())
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - degeneracy-guard\n")
    code = (
        "import json\n"
        "from hermes_cli import plugins as P\n"
        "P.discover_plugins()\n"
        "lp = P.get_plugin_manager()._plugins['degeneracy-guard']\n"
        "bad = {'content': '\\n'.join(['same line'] * 30)}\n"
        "print(json.dumps({'error': lp.error,\n"
        "  'block': P._dispatch_pre_tool_call_hooks('write_file', bad, session_id='s')[0],\n"
        "  'ok': P._dispatch_pre_tool_call_hooks('write_file', {'content': 'hi'})[0]}))\n"
    )
    env = {**os.environ, "HERMES_HOME": str(home), "DEGENERACY_GUARD_HOME": str(tmp_path / "lb")}
    out = subprocess.run([sys.executable, "-P", "-c", code], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["error"] is None
    assert res["block"] and res["block"].startswith("DEGENERACY_GUARD_BLOCK")
    assert res["ok"] is None
