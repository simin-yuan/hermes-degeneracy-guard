# hermes-degeneracy-guard

Hermes already cuts a stream that degenerates into a repetition loop. `RunawayStreamWatch`
(`agent/repetition_guard.py`) is wired into the streaming path, so repeated content and repeated
thinking are truncated at runaway scale — introduced by
[#130551](https://github.com/NousResearch/hermes-agent/pull/130551), merged 2026-10-01, merge commit
`77c373ac5fbba5a67afd7b6553ad6774815972da`; still present at `22063fc0`.

What that cut does not cover is the action. A tool call whose arguments are repetition-dominated but
which arrives with a provider-confirmed finish reason is **deliberately** allowed through — see the
comment and the condition at `agent/chat_completion_helpers.py:3278-3285` (*"A provider-confirmed call
may legitimately write repetitive data"*). A call is also the only part of a turn that changes
anything outside the process: a file, a post, a shell command.

This plugin is the supplement to that one point. It runs at `pre_tool_call`, **before the tool
executes**, scores the arguments, refuses the call when they are degenerate, and records an incident
on the local machine.

## What it does

| Hook | Can it observe? | Can it change the turn? | What this plugin does there |
|---|---|---|---|
| `pre_tool_call` | yes | yes — blocks the call | **the main job**: refuses degenerate tool arguments before execution |
| `on_stream_delta` | yes, per chunk | no (off the token path) | scores the stream incrementally and writes an incident record |
| `transform_llm_output` | yes | yes — replaces the output | swaps flagged output for a short notice |
| `post_llm_call` | yes | no | clears state, records a flag nobody consumed |
| `pre_llm_call` | yes | yes — injects context | warns once a session passes 500 messages, then every 50 more |

This is a **plugin**: it registers the five hooks above and never edits a file shipped by Hermes.
It was renamed because a public project already used its former name:
[`zima2042/hermes-loop-breaker`](https://github.com/zima2042/hermes-loop-breaker), which attacks the
same problem with a patch to four core files — that works, but it needs a restart and drifts every
time upstream moves.

## What this plugin does not do

It does not stop a generation in progress: no plugin hook can abort the stream, so that cut remains
with core's `RunawayStreamWatch`, and this plugin relies on it. `on_stream_delta` observes and records;
`pre_tool_call` is where the intervention happens. Each incident record says which of the two acted.

`on_stream_delta` runs on a bounded background queue that drops the oldest events when full, so a
stream-side flag can arrive after `transform_llm_output` has run; `pre_tool_call` does not depend on
that queue.

## Why guards sit where they do

- The action guard is at `pre_tool_call` because a tool call is the only thing that produces a side
  effect outside the process. Degenerate text that only lands in the transcript is annoying;
  degenerate text that lands in a file, a post, or a shell command is damage.
- The stream observer is at `on_stream_delta` because that is the earliest point where degenerate
  text is visible, and an incident record needs it.
- The session-length warning is at `pre_llm_call` because long sessions are where loops grow, and
  that hook can add a note to the next request before the model runs.

## Thresholds

Frozen in `loop_detector.py` as `FROZEN_THRESHOLDS`: identical-run, periodic-repeat (primary),
and a short-line fallback. They are constants rather than configuration on purpose — a detector
that can be re-tuned mid-incident cannot be used as evidence afterwards.

## Install

Checked against [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent) at
`22063fc099ec9ad8357d7f8f6e6248a0400680d3`. The plugin passes `hermes plugins validate`.

Hermes looks for user plugins in `<HERMES_HOME>/plugins/<dir>/plugin.yaml`
(`hermes_cli/plugins_discovery.py:209`). `HERMES_HOME` defaults to `~/.hermes` on Linux and macOS
(and under WSL) and to `%LOCALAPPDATA%\hermes` on native Windows (`hermes_constants.py:51-58`); an
explicit `HERMES_HOME` environment variable overrides both.

User plugins are **opt-in**: a plugin in that directory is skipped until it is in `plugins.enabled`
in `config.yaml` (`hermes_cli/plugins_discovery.py:299-302`). `hermes plugins enable` adds it there.
The name to enable is the manifest `name`, `degeneracy-guard`. `hermes plugins enable` needs network
access because Hermes syncs its own environment; if that step fails, the plugin stays disabled.

Linux / macOS:

```bash
git clone https://github.com/simin-yuan/hermes-degeneracy-guard ~/.hermes/plugins/degeneracy-guard
hermes plugins enable degeneracy-guard
```

Windows (PowerShell, native install):

```powershell
git clone https://github.com/simin-yuan/hermes-degeneracy-guard "$env:LOCALAPPDATA\hermes\plugins\degeneracy-guard"
hermes plugins enable degeneracy-guard
```

Then restart Hermes (a running gateway: `hermes gateway restart`) and confirm it loaded:

```bash
hermes plugins doctor degeneracy-guard
```

Expect `OK` and `5 hook(s)`. `hermes plugins list` only shows whether the plugin is enabled in
config, not whether it loaded.

State (incident records, the session-length counter) goes to Hermes' per-plugin data root,
`<HERMES_HOME>/plugin-data/degeneracy-guard/` (`plugins.plugin_storage.plugin_data_dir`). It follows
the active Hermes profile and survives `hermes plugins update` and `hermes plugins remove`. On native
Windows that is `%LOCALAPPDATA%\hermes\plugin-data\degeneracy-guard\` unless `HERMES_HOME` is set.
Set `DEGENERACY_GUARD_HOME` to put it somewhere else. Outside Hermes (for example this repository's
own tests without an override) it falls back to `~/.degeneracy-guard`
(`%USERPROFILE%\.degeneracy-guard` on Windows).

## Tests

```bash
python -m pytest -q
```

## Status

Used in daily operation. `loop_detector.py` is pure and testable without a host; the hook
modules are thin adapters around it.

## Disclosure

No network calls, shell commands, background processes, telemetry or credentials. The plugin reads only the hook payloads Hermes passes to it. It writes JSON incident records and a small session-length counter under its own state directory: `<HERMES_HOME>/plugin-data/degeneracy-guard/` inside Hermes, `DEGENERACY_GUARD_HOME` when set, or `~/.degeneracy-guard` outside Hermes. A stream-side incident record keeps the last 800 characters of the flagged output, and incident details can quote a short excerpt of the repeated text; these files stay on the local machine.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
