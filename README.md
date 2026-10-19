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

Version 1.2.0 changed the tool-argument rule. Earlier versions refused legitimate repetitive data —
an HTML table of empty rows, a JSON array of eight flag objects, a matrix of zero rows, a CSV column
of zeros. Those payloads are now allowed, and a committed corpus holds them so the fix cannot regress.

## What it does

| Hook | Can it observe? | Can it change the turn? | What this plugin does there |
|---|---|---|---|
| `pre_tool_call` | yes | yes — blocks the call | **the main job**: refuses degenerate tool arguments before execution |
| `on_stream_delta` | yes, per chunk | no (off the token path) | scores the stream incrementally and writes an incident record |
| `transform_llm_output` | yes | yes — replaces the output | swaps flagged output for a short notice |
| `post_llm_call` | yes | no | clears state, records a flag nobody consumed |
| `pre_llm_call` | yes | yes — injects context | warns once a session passes 500 messages, then every 50 more |

This is a **plugin**: it registers the five hooks above and never edits a file shipped by Hermes.
A related project, [`zima2042/hermes-loop-breaker`](https://github.com/zima2042/hermes-loop-breaker),
takes the other route: it patches core files and handles cross-turn similarity in reasoning. Both
approaches work; they sit at different points. That one reaches inside the generation and needs the
patch kept in step with upstream. This one stays outside, uses hooks only, covers the tool-argument
case, and gets its reach from what the plugin API exposes today.

## What is refused, and what is not

A tool argument is refused only when all of the following hold:

1. at least one string in the arguments is 200 characters or longer;
2. the payload has no data-like shape — it does not parse as JSON, a markdown table, HTML rows, a
   numeric matrix, a separator or checklist block, or a delimited table with varied rows;
3. either 20 identical consecutive lines appear, or one block of at most 20 lines repeats at least
   20 times and covers 60% or more of the non-blank lines.

Everything else passes. That includes a table of 40 empty `<tr><td></td></tr>` rows, 40 lines of
`0,0,0,0`, a 16-row zero matrix, and a JSON array of eight identical flag objects — the four payloads
reported as false positives against 1.1.0.

The corpus that fixes this behaviour is committed under `tests/corpus/`: **27 legitimate payloads**
(HTML and markdown tables, JSON arrays, matrices, CSV, YAML lists, a lockfile block, log lines, an SVG
path, separator and filler lines, generated stub code) and **10 degeneracy samples** (the same
sentence 60 times, a repeated paragraph, two- and three-line cycles, a wrapped-prose loop, a
code-looking loop, a Chinese sentence loop, a JSON fragment loop). All 27 pass; all 10 are refused.

Two honest limits:

- **A loop that looks like data is not refused.** 60 identical `0,0,0,0` rows parse as a numeric
  matrix, so they pass. This is the cost of the exemption that fixes the false positives, and it is
  the same trade-off Hermes core makes deliberately. The plugin covers repetition that has no data
  shape; it does not argue with a payload that looks like a table.
- **A long unlabelled repetition inside a legitimate document is still refused.** Thirty identical
  log lines with no other structure repeat 30 times over the whole payload, so they are blocked. The
  way through is the escape hatch below, or writing the content in two calls.

### Escape hatch

`DEGENERACY_GUARD_ALLOW_TOOLS` takes comma-separated tool names; a listed tool never reaches the
`pre_tool_call` check. It is read on every call, so exporting it in a shell affects the next tool
call without a restart:

```bash
export DEGENERACY_GUARD_ALLOW_TOOLS=write_file,edit_file
```

Thresholds are not touched by this variable; it removes named tools from the guard entirely, which
is the point — an escape hatch that silently changed thresholds would make incidents unauditable.

## What this plugin does not do

It does not stop a generation in progress: no plugin hook can abort the stream, so that cut remains
with core's `RunawayStreamWatch`, and this plugin relies on it. `on_stream_delta` observes and records;
`pre_tool_call` is where the intervention happens. Each incident record says which of the two acted.

`on_stream_delta` runs on a bounded background queue that drops the oldest events when full, so a
stream-side flag can arrive after `transform_llm_output` has run; `pre_tool_call` does not depend on
that queue.

Stream scoring costs one pass over a bounded window per chunk: a 102,735-character reply (1,363
lines) fed in 24-character chunks takes 0.13 s here, and the cost stays linear in reply length.
(1.1.0 re-joined and re-scanned the whole buffer per chunk: the same reply took 17.4 s here,
growing with the square of its length. A reply with no line breaks was cheap in 1.1.0 because the
line-count gate returned before the scan; the cost came from reply length times line count.)

## Why guards sit where they do

- The action guard is at `pre_tool_call` because a tool call is the only thing that produces a side
  effect outside the process. Degenerate text that only lands in the transcript is annoying;
  degenerate text that lands in a file, a post, or a shell command is damage.
- The stream observer is at `on_stream_delta` because that is the earliest point where degenerate
  text is visible, and an incident record needs it.
- The session-length warning is at `pre_llm_call` because long sessions are where loops grow, and
  that hook can add a note to the next request before the model runs.

## Thresholds

Frozen constants, not configuration: a detector that can be re-tuned mid-incident cannot be used as
evidence afterwards. Changing a number means changing `loop_detector.py` and raising the version.

Stream text (`FROZEN_THRESHOLDS`): 12 identical consecutive lines, or a block of at most 20 lines
repeated at least 8 times, or a short-line fallback for periods longer than 20.

Tool arguments (`TOOL_ARG_THRESHOLDS`) and the measurements behind each number:

| Constant | Value | What the corpus shows |
|---|---|---|
| `min_chars` | 200 | below this a repetition cannot carry a loop far enough to matter; no sample is affected |
| `min_identical_run` | 20 | largest identical run in a legitimate payload with no data shape: 17 (`26-quoted-line-in-document.txt`); degeneracy samples: 25–60 |
| `min_reps` | 20 | legitimate payloads without a data shape never repeat a block at all (1 repetition); degeneracy samples repeat 25–60 times |
| `min_share` | 0.6 | not exercised by the corpus (every degeneracy sample covers ≥98%, every legitimate one is exempt or unrepeated); it is there so a document that quotes a block and then continues is not refused |
| `min_structured_variety` | 5 | the exemption for recognised data shapes is unconditional; this bound only governs structure kinds outside the allow-list |
| `scan_cap_chars` | 1,000,000 | a pathological argument is scored head+tail instead of turning the guard into a denial of service |

`tests/test_tool_arg_rule.py` recomputes these margins on every run, so a future threshold change
that invalidates this table fails the suite instead of leaving the README quoting old numbers.

## Install

Checked against [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent) at
`22063fc099ec9ad8357d7f8f6e6248a0400680d3`. The plugin passes `hermes plugins validate`. It requires
Hermes `>=0.22.2` (`requires_hermes` in `plugin.yaml`), verified at the `v0.22.2` tag and at current
main.

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
(`%USERPROFILE%\.degeneracy-guard` on Windows). Incident file names carry a microsecond timestamp and
a short random suffix, so several blocks in the same second leave several files.

## Tests

```bash
python -m pytest tests
```

`tests/` must be the argument: pytest would otherwise treat this repository root as a package,
because a plugin's `__init__.py` lives there.

Without `hermes-agent` importable, the integration test is skipped; with it on `PYTHONPATH`, all
tests run, including one that loads the plugin through Hermes' own plugin manager and drives a
degenerate call through `_dispatch_pre_tool_call_hooks`.

## Status

Version 1.2.0. `loop_detector.py` is pure and testable without a host; the hook modules are thin
adapters around it.

## Disclosure

No network calls, shell commands, background processes, telemetry or credentials. The plugin reads only the hook payloads Hermes passes to it. It writes JSON incident records and a small session-length counter under its own state directory: `<HERMES_HOME>/plugin-data/degeneracy-guard/` inside Hermes, `DEGENERACY_GUARD_HOME` when set, or `~/.degeneracy-guard` outside Hermes. A stream-side incident record keeps the last 800 characters of the flagged output; a tool-argument record keeps the tool name, session id, reason and argument length, not the argument text. These files stay on the local machine.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.

© 2026 Simin (Simon) Yuan — ORCID [0009-0002-6843-3390](https://orcid.org/0009-0002-6843-3390) · [github.com/simin-yuan](https://github.com/simin-yuan).
