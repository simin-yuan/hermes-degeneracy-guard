"""Contract tests against the Hermes plugin API.

Every expectation below was read from NousResearch/hermes-agent at commit
22063fc099ec9ad8357d7f8f6e6248a0400680d3 (the commit CI pins, main 2026-10-18). File:line references are to
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

import types
import uuid

import _pkg

_ROOT = Path(__file__).resolve().parents[1]
# Keep every write out of the user's home; test_degeneracy_guard.py sets its own
# DEGENERACY_GUARD_HOME during collection.
os.environ.setdefault("DEGENERACY_GUARD_HOME", tempfile.mkdtemp(prefix="degeneracy-guard-test-"))

UPSTREAM_COMMIT = "22063fc099ec9ad8357d7f8f6e6248a0400680d3"

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
    """Import __init__.py the way hermes_cli/plugins_loader.py does: as a package.

    The loader first registers the ``hermes_plugins`` namespace package, then imports the
    plugin directory as ``hermes_plugins.<slug>`` with the directory as its search location,
    which is what makes the plugin's relative imports resolve.
    """
    parent = name.rpartition(".")[0]
    if parent and parent not in sys.modules:
        ns = types.ModuleType(parent)
        ns.__path__ = []          # type: ignore[attr-defined]
        ns.__package__ = parent
        sys.modules[parent] = ns
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    sys.modules.pop(name, None)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = name
    mod.__path__ = [str(_ROOT)]   # type: ignore[attr-defined]
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


def test_ask_return_shape_matches_upstream():
    """hermes_cli/plugins.py:2006-2039: a dict with action == "approve" is held back while the
    whole hook list is scanned for a veto (block wins), then becomes a human-approval request;
    ``rule_key`` picks the "[a]lways" grain and defaults to the tool name when absent."""
    tag = _pkg.submodule("tool_arg_guard")
    r = tag.on_pre_tool_call(
        tool_name="write_file", args={"content": "\n".join(["same line"] * 30)},
        task_id="", session_id="s", tool_call_id="c", turn_id="t", api_request_id="",
        middleware_trace=[], telemetry_schema_version=1)
    assert isinstance(r, dict)
    assert set(r) == {"action", "message", "rule_key"}
    assert r["action"] == "approve"
    assert isinstance(r["message"], str) and r["message"]
    assert r["rule_key"] == "degeneracy-guard:write_file"


def test_pre_llm_call_return_shape():
    """agent/turn_context.py:835-836: a dict with a truthy "context" is injected."""
    slg = _pkg.submodule("session_length_guard")
    # Unique id: the notice is de-bounced per session, so a reused id could be silenced by
    # state left behind by an earlier run.
    r = slg.on_pre_llm_call(
        session_id=f"contract-{uuid.uuid4().hex}",
        conversation_history=[{"content": "x"}] * 600)
    assert isinstance(r, dict) and isinstance(r.get("context"), str) and r["context"]


def test_plugin_loads_without_its_directory_on_sys_path(tmp_path):
    """The loader does not add the plugin directory to sys.path (plugins_loader.py:739-748).
    Run in a clean interpreter so this test's own sys.path cannot hide a failure."""
    code = (
        "import importlib.util, sys, types\n"
        f"root = {str(_ROOT)!r}\n"
        "assert root not in sys.path\n"
        "ns = types.ModuleType('hermes_plugins'); ns.__path__ = []; "
        "ns.__package__ = 'hermes_plugins'; sys.modules['hermes_plugins'] = ns\n"
        "spec = importlib.util.spec_from_file_location('hermes_plugins.dg', root + '/__init__.py',"
        " submodule_search_locations=[root])\n"
        "m = importlib.util.module_from_spec(spec); m.__package__ = 'hermes_plugins.dg';"
        " m.__path__ = [root]; sys.modules['hermes_plugins.dg'] = m\n"
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


_HERMES = (importlib.util.find_spec("hermes_cli") is not None
           and importlib.util.find_spec("hermes_yaml") is not None)


@pytest.mark.skipif(not _HERMES, reason="hermes-agent is not installed (optional integration test)")
def test_real_hermes_plugin_manager_asks_then_blocks_on_denial(tmp_path):
    """Load the plugin through Hermes' own discovery, then drive the real fail-closed path:
    ``_get_pre_tool_call_directive_details`` (plugins.py:1979) turns our hook's dict into an
    ``approve`` directive, and ``resolve_pre_tool_block`` (``_resolve_block_from_details``,
    plugins.py:2059-2088) runs the approval gate. Approving proceeds; denying, erroring or
    timing out blocks. The gate itself is replaced by a double, because the real one waits on
    a human. With no DEGENERACY_GUARD_HOME, incidents land in Hermes' per-plugin data root
    (plugins/plugin_storage.py plugin_data_dir).
    """
    home = tmp_path / "hermes"
    dest = home / "plugins" / "degeneracy-guard"
    dest.mkdir(parents=True)
    for f in list(_ROOT.glob("*.py")) + [_ROOT / "plugin.yaml"]:
        (dest / f.name).write_bytes(f.read_bytes())
    # Drop the version gate: this test drives whatever hermes-agent is importable, and the
    # >=0.22.2 floor is verified separately by running validate/doctor at the v0.22.2 tag.
    manifest = (dest / "plugin.yaml").read_text(encoding="utf-8")
    (dest / "plugin.yaml").write_text(
        "".join(line for line in manifest.splitlines(True)
                if not line.startswith("requires_hermes")), encoding="utf-8")
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - degeneracy-guard\n")
    code = (
        "import json\n"
        "from hermes_cli import plugins as P\n"
        "import tools.approval as A\n"
        "P.discover_plugins()\n"
        "assert P.get_plugin_manager()._plugins['degeneracy-guard'].error is None\n"
        "bad = {'content': '\\n'.join(['same line'] * 30)}\n"
        "d = P._get_pre_tool_call_directive_details('write_file', bad, session_id='s')\n"
        "out = {'action': d.action, 'rule_key': d.rule_key,\n"
        "       'clean': P.get_pre_tool_call_directive('write_file', {'content': 'hi'})[0]}\n"
        "seen = {}\n"
        "def gate(result=None, boom=None):\n"
        "    def call(tool, message, rule_key=None, **kw):\n"
        "        seen.update(tool=tool, message=message, rule_key=rule_key)\n"
        "        if boom: raise boom\n"
        "        return result\n"
        "    return call\n"
        "A.request_tool_approval = gate({'approved': True, 'message': 'ok'})\n"
        "out['approved'] = P.resolve_pre_tool_block('write_file', bad, session_id='s')\n"
        "A.request_tool_approval = gate({'approved': False, 'message': 'USER DENIED'})\n"
        "out['denied'] = P.resolve_pre_tool_block('write_file', bad, session_id='s')\n"
        "A.request_tool_approval = gate(boom=TimeoutError('no answer'))\n"
        "out['timeout'] = P.resolve_pre_tool_block('write_file', bad, session_id='s')\n"
        "A.request_tool_approval = gate({'approved': True, 'message': 'ok'})\n"
        "out['gate_saw'] = seen\n"
        "print(json.dumps(out))\n"
    )
    env = {**os.environ, "HERMES_HOME": str(home)}
    env.pop("DEGENERACY_GUARD_HOME", None)
    # A fresh install starts in observe, so a reload of this plugin here would return no
    # directive at all. This test is about the enforcement half of the contract, so it asks
    # for it the way a user does - the documented override - and leaves the "which mode does
    # an install get" question to tests/test_mode.py.
    env["DEGENERACY_GUARD_MODE"] = "act"
    out = subprocess.run([sys.executable, "-P", "-c", code], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["action"] == "approve"
    assert res["rule_key"] == "degeneracy-guard:write_file"
    assert res["clean"] is None
    assert res["approved"] is None                              # user allowed it: the call runs
    assert res["denied"] == "USER DENIED"                       # user refused: blocked
    assert res["timeout"].startswith("BLOCKED")                 # gate errored: fail closed
    assert res["gate_saw"]["message"].startswith("DEGENERACY_GUARD_APPROVAL")
    assert res["gate_saw"]["rule_key"] == "degeneracy-guard:write_file"
    incidents = list((home / "plugin-data" / "degeneracy-guard" / "incidents").glob("*_TOOLARG_*.json"))
    assert incidents
    rec = json.loads(incidents[0].read_text(encoding="utf-8"))
    assert rec["action"] == "approve_requested"
