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
executes**, scores the arguments, and when they look like a loop it asks the user — Hermes' own
approval gate — whether the call should run. A refusal, an error in the gate, or a timeout all
refuse the call, and an incident record lands on the local machine.

**Version 1.3.0 stops the plugin from deciding intent.** 1.2.0 tried to tell data from
pathology by its shape and refused whatever it did not recognise; an independent set of twelve
ordinary payloads (SQL, YAML, TOML, logs, XML, a Python list of dicts) showed seven of them
refused. The reason is not a bad threshold: content alone cannot separate a deliberately repeated
payload from a model stuck in a loop — a thousand-row zero matrix is data in one call and
pathology in the next, and only the user knows which. So the guard now raises the question instead
of answering it, and the cost of a wrong guess drops from a failed write to one click.

## What it does

| Hook | Can it observe? | Can it change the turn? | What this plugin does there |
|---|---|---|---|
| `pre_tool_call` | yes | yes — sends a call to the approval gate, which can refuse it | **the main job**: asks the user about a tool call whose arguments look like a loop |
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

## When it asks, and when it stays out of the way

A tool argument is sent to the approval gate only when all of the following hold:

1. at least one string in the arguments is 200 characters or longer and has at least 12 non-blank
   lines;
2. the payload has no data-like shape — it does not parse as JSON, and it is not a markdown table,
   a checklist, XML or HTML rows, SQL statements, TOML or YAML config, a timestamped log, a numeric
   matrix, a separator block, or delimited records with rows that differ from each other;
3. either 18 identical consecutive lines appear, or one block of at most 20 lines repeats at least
   18 times and covers 60% or more of the non-blank lines.

Everything else runs without a prompt. That includes a table of 40 empty `<tr><td></td></tr>` rows,
40 lines of `0,0,0,0`, a 16-row zero matrix, a JSON array of eight identical flag objects, a SQL
file of 40 identical INSERTs, a YAML list of 30 identical items, a log where the same heartbeat
line appears 40 times, and a Python list of 30 identical dicts.

What the prompt means, and what each answer does:

| Answer | What happens |
|---|---|
| Approve | the call runs as written |
| Approve, and choose "always" | the same tool does not ask again (`rule_key` is `degeneracy-guard:<tool>`, so the allowance is per tool, not per payload) |
| Deny | the call is refused, with the refusal as the tool result |
| No answer | the call is refused. The gate is fail-closed: a timeout refuses, cron defaults to `approvals.cron_mode: deny`, and a non-interactive context with no approval bridge refuses outright |

That last row is the reason this is safe to install and forget. The plugin itself never decides to
run something; when nobody is present to answer, nothing runs.

The mechanism is Hermes', not this plugin's: `pre_tool_call` may return
`{"action": "approve", "message", "rule_key"}`, which `hermes_cli/plugins.py:1979` collects (a
`block` from any plugin outranks an `approve`, at `:2028`) and `:2059` resolves through
`tools/approval.py:1121 request_tool_approval` — the same gate that guards dangerous shell commands,
with `fail_closed_when_no_human=True` (`tools/approval.py:1143`) and the cron default at
`hermes_cli/config_defaults.py:1696`.

The corpus that pins this behaviour is committed under `tests/corpus/`: **39 legitimate payloads**
(HTML and markdown tables, JSON arrays, matrices, CSV, YAML/TOML/env configs, SQL dumps, lockfile
blocks, timestamped logs, an SVG path, separator and filler lines, generated stub code, a C lookup
table, parametrised test data) and **16 degeneracy samples** (the same sentence 60 times, a repeated
paragraph, two- and three-line cycles, a wrapped-prose loop, code-looking loops, a Chinese sentence
loop, a JSON fragment loop, a short-line tail). All 39 pass; all 16 reach the gate.

### Hold-out

The corpus above is also the set the rules were built against, so it cannot show whether they
generalise. A second set was written independently — 40 legitimate payloads (SQL, CSV, TSV, JSON,
JSONL, YAML, TOML, INI, env, properties, proto, TF, HTML, XML, SVG, CSS, JS, Python, diff, hexdump,
base64, license text, changelog, runbook, fixed-width table, ASCII art, box drawing) and 13
degeneracy samples — by someone who never saw `loop_detector.py` or this README. It was scored
in one pass against the finished rules, and no rule was changed afterwards.

| Set | Files | Asked | Left alone |
|---|---|---|---|
| legitimate | 40 | 0 | 40 |
| degeneracy | 13 | 6 | 7 |

A second outside set, written later by the project's reviewer and scored once, was harder: of 12 legitimate repetitive payloads (Dockerfile RUN lines, shell echo lines, INI sections, reStructuredText and LaTeX tables, a hex dump, and others), 6 would have triggered a confirmation; of 5 degeneracy samples, 3 were caught (missed: a list item repeated 40 times, and a numbered four-step cycle). A confirmation is one prompt per tool, and "always" stops it for that tool, so these numbers are a cost to read, not a failure mode.

The seven misses in the first set are worth naming, because they define what this check cannot do:

- `endless_list.txt` — `- item` repeated 68 times. A markdown list, so it is exempt; the same
  payload written on purpose is a legitimate checklist (it is in the committed corpus as
  `markdown_checklist`), and no rule can tell the two apart.
- `json_record_cycle.txt`, `yaml_record_cycle.txt` — a record repeated 30 times. Both parse as data,
  so both are exempt; this is the documented price of not fighting tables.
- `counter_only.txt`, `numbered_same_content.txt` — the repeated sentence behind a counter
  (`Step 3 completed.`, `3. I should verify ...`). Every line differs, so there is no exact repeat.
- `letme_check_variations.txt`, `paragraph_variations.txt` — a cycle reworded on each pass.

The last four are the real gap: this check matches **exact** repeated lines, not near-repeats. A
fuzzy match would close them and reopen the false positives, because a numbered list and a numbered
loop are the same text — which is why the answer here is to ask a human rather than to score harder.

Two more limits:



- **A loop that parses as data is never questioned.** 60 identical `0,0,0,0` rows form a numeric
  matrix; 30 identical `<row a="0"/>` lines form XML; both pass silently. Keeping real data usable
  costs exactly this, and it is the same trade-off Hermes core makes deliberately. The plugin covers
  repetition with no data shape, and leaves tabular repetition to the user's judgement.
- **The prompt is per tool, not per incident.** Once "always" is chosen for a tool, a later loop in
  the same tool's arguments will not be caught. `DEGENERACY_GUARD_ALLOW_TOOLS` has the same property
  and says so.

### The prompt, and how to stop being asked

`DEGENERACY_GUARD_ALLOW_TOOLS` takes comma-separated tool names; a listed tool never reaches the
`pre_tool_call` check. It is read on every call, so exporting it in a shell affects the next tool
call without a restart:

```bash
export DEGENERACY_GUARD_ALLOW_TOOLS=write_file,edit_file
```

Thresholds are not touched by this variable; it removes named tools from the guard entirely, which
is the point — an escape hatch that silently changed thresholds would make incidents unauditable.
For a one-off payload, the "always" answer in the prompt is the narrower option and needs no
environment change.

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
| `min_lines` | 12 | below this the payload is a snippet, not a body of output |
| `min_identical_run` | 18 | largest identical run in a legitimate payload with no data shape: 17 (`26-quoted-line-in-document.txt`); degeneracy samples: 25–80 |
| `min_reps` | 18 | legitimate payloads without a data shape repeat a block at most once; degeneracy samples repeat 25–80 times |
| `min_share` | 0.6 | every degeneracy sample covers ≥98% of its lines, every legitimate one is exempt or unrepeated; it is there so a document that quotes a block and then continues is not questioned |
| `max_period` | 20 | cycles longer than this are not searched for a repeat; the accepted gap is a >20-line cycle repeated verbatim, which is not questioned unless it contains an 18-line identical run |
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
tests run, including one that loads the plugin through Hermes' own plugin manager, turns a
degenerate call into an `approve` directive, and drives Hermes' real gate with a stand-in for the
human: approving proceeds, denying blocks, and a gate that errors blocks.

## Status

Version 1.3.0. `loop_detector.py` is pure and testable without a host; the hook modules are thin
adapters around it.

## Disclosure

No network calls, shell commands, background processes, telemetry or credentials. The plugin reads only the hook payloads Hermes passes to it. A tool call whose arguments look like a repetition loop is **not refused by the plugin**: it is forwarded to Hermes' human-approval gate, and the call is refused when the user denies it, when the gate errors, or when the answer times out — including cron, where `approvals.cron_mode` defaults to `deny`. Approving runs the call, and choosing "always" stops the prompt for that tool. It writes JSON incident records and a small session-length counter under its own state directory: `<HERMES_HOME>/plugin-data/degeneracy-guard/` inside Hermes, `DEGENERACY_GUARD_HOME` when set, or `~/.degeneracy-guard` outside Hermes. A stream-side incident record keeps the last 800 characters of the flagged output; a tool-argument record keeps the tool name, session id, reason and argument length, not the argument text. These files stay on the local machine.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.

© 2026 Simin Yuan — ORCID [0009-0002-6843-3390](https://orcid.org/0009-0002-6843-3390) · [github.com/simin-yuan](https://github.com/simin-yuan).
