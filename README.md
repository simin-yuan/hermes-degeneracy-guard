# hermes-degeneracy-guard

Hermes already cuts a stream that degenerates into a repetition loop. `RunawayStreamWatch`
(`agent/repetition_guard.py`) is wired into the streaming path, so repeated content and repeated
thinking are truncated at runaway scale — introduced by
[#130551](https://github.com/NousResearch/hermes-agent/pull/130551), merged 2026-10-01, merge commit
`77c373ac5fbba5a67afd7b6553ad6774815972da`; still present on `main` = `9e01b44d`.

What that cut does not cover is the action. A tool call whose arguments are repetition-dominated but
which arrives with a provider-confirmed finish reason is **deliberately** allowed through — see the
comment and the condition at `agent/chat_completion_helpers.py:3278-3285` (*"A provider-confirmed call
may legitimately write repetitive data"*). A call is also the only part of a turn that changes
anything outside the process: a file, a post, a shell command.

This plugin is the supplement to that one point. It runs at `pre_tool_call`, **before the tool
executes**, scores the arguments, refuses the call when they are degenerate, records an incident,
and reports it on the next turn.

## What it does

| Hook | Can it observe? | Can it change the turn? | What this plugin does there |
|---|---|---|---|
| `pre_tool_call` | yes | yes — blocks the call | **the main job**: refuses degenerate tool arguments before execution |
| `on_stream_delta` | yes, per chunk | no (off the token path) | scores the stream incrementally and writes an incident record |
| `transform_llm_output` | yes | yes — replaces the output | swaps flagged output for a short notice |
| `post_llm_call` | yes | no | clears state, records a flag nobody consumed |
| `pre_llm_call` | yes | yes — injects context | reports the last incident; warns on session length |

This is a **plugin**: it registers the five hooks above and never edits a file shipped by Hermes.
The earlier project of the same name, [`zima2042/hermes-loop-breaker`](https://github.com/zima2042/hermes-loop-breaker),
attacks the same problem with a patch to four core files — that works, but it needs a restart and
drifts every time upstream moves.

## What this plugin does not do

It does not stop a generation in progress: no plugin hook can abort the stream, so that cut remains
with core's `RunawayStreamWatch`, and this plugin relies on it. `on_stream_delta` observes and records;
`pre_tool_call` is where the intervention happens. Each incident record says which of the two acted.

## Why guards sit where they do

- The action guard is at `pre_tool_call` because a tool call is the only thing that produces a side
  effect outside the process. Degenerate text that only lands in the transcript is annoying;
  degenerate text that lands in a file, a post, or a shell command is damage.
- The stream observer is at `on_stream_delta` because that is the earliest point where degenerate
  text is visible, and an incident record needs it.
- The recovery notice is at `pre_llm_call` because an incident nobody reports is just a quieter dead
  end.

## Thresholds

Frozen in `loop_detector.py` as `FROZEN_THRESHOLDS`: identical-run, periodic-repeat (primary),
and a short-line fallback. They are constants rather than configuration on purpose — a detector
that can be re-tuned mid-incident cannot be used as evidence afterwards.

## Install

Checked against [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent) at
`9e01b44dd23978f9ff40b7837467f3a9f1582130`.

Hermes looks for user plugins in `<HERMES_HOME>/plugins/<dir>/plugin.yaml`
(`hermes_cli/plugins_discovery.py:209`). `HERMES_HOME` defaults to `~/.hermes` on Linux and macOS
(and under WSL) and to `%LOCALAPPDATA%\hermes` on native Windows (`hermes_constants.py:51-58`); an
explicit `HERMES_HOME` environment variable overrides both.

User plugins are **opt-in**: a plugin in that directory is skipped until it is in `plugins.enabled`
in `config.yaml` (`hermes_cli/plugins_discovery.py:299-302`). `hermes plugins enable` adds it there.
The name to enable is the manifest `name`, `degeneracy-guard`.

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

Then restart Hermes (a running gateway: `hermes gateway restart`). `hermes plugins list` should show
`degeneracy-guard` as `enabled`.

State (incident records, counters) goes to `~/.loop-breaker` by default (on Windows,
`%USERPROFILE%\.loop-breaker`); set `LOOP_BREAKER_HOME` to move it.

## Tests

```bash
python -m pytest -q
```

## Status

Used in daily operation. `loop_detector.py` is pure and testable without a host; the hook
modules are thin adapters around it.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
