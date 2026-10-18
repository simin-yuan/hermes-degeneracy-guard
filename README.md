# hermes-loop-breaker

**A model stuck in a repetition loop can report that it is repeating and still cannot stop.
This plugin stops it from outside.**

Five hooks, one job: notice degeneracy in the token stream, keep it out of the session, keep
it out of tool arguments, and leave a record of every time it happened.

## What it does

| Hook | Can it observe? | Can it change the turn? | What this plugin does there |
|---|---|---|---|
| `on_stream_delta` | yes, per chunk | no (off the token path) | judges the stream incrementally, writes an incident |
| `transform_llm_output` | yes | yes - replaces the output | swaps flagged output for a short notice |
| `post_llm_call` | yes | no | clears state, records a flag nobody consumed |
| `pre_tool_call` | yes | yes - blocks the call | refuses tool arguments that are degenerate output |
| `pre_llm_call` | yes | yes - injects context | reports the last abort; warns on session length |

The honest limit is in the first row: **no plugin hook can abort a generation in progress.**
Detection is real time, aborts are not. A true abort needs a breaker flag checked inside the
core streaming loop; until that exists, this plugin replaces output instead of stopping it,
and says so in every incident record.

## Why guards sit where they do

- The stream guard is at `on_stream_delta` because that is the earliest point where degenerate
  text is visible.
- The action guard is at `pre_tool_call` because a tool call is the only thing that produces a
  side effect outside the process. Degenerate text that only lands in the transcript is
  annoying; degenerate text that lands in a file, a post, or a shell command is damage.
- The recovery notice is at `pre_llm_call` because an abort nobody reports is just a quieter
  dead end.

## Thresholds

Frozen in `loop_detector.py` as `FROZEN_THRESHOLDS`: identical-run, periodic-repeat (primary),
and a short-line fallback. They are constants rather than configuration on purpose - a detector
that can be re-tuned mid-incident cannot be used as evidence afterwards.

## Install

Copy the directory into the host's plugin directory and enable it. State goes to
`~/.loop-breaker` by default; set `LOOP_BREAKER_HOME` to move it.

## Tests

```bash
python -m pytest -q
```

## Status

Used in daily operation. `loop_detector.py` is pure and testable without a host; the hook
modules are thin adapters around it.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
