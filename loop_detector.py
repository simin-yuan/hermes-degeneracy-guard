"""Degeneracy-loop detector: pure functions, no side effects, independently testable.

Two jobs, deliberately kept apart:

- ``check_lines`` / ``check_text`` judge streamed *model text*. Thresholds were frozen
  before the first run and the stream path keeps them, so a change here is a visible diff.
- ``judge_tool_arg`` judges *tool arguments*. A tool argument is data, not prose: a table,
  a JSON array, a matrix or a CSV export may repeat on purpose, and Hermes core lets such a
  call through on purpose (``agent/chat_completion_helpers.py``: "a provider-confirmed call
  may legitimately write repetitive data"). The tool-argument rule therefore asks a second
  question before it blocks: does the repetition look like data at all?

Why a detector exists at all: a model inside a repeat loop can report that it is repeating
and still cannot stop. Self-report is not self-control, so the check runs outside the model.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from typing import Sequence

# -- Frozen thresholds (set before the run; changing them means changing this file) ----
FROZEN_THRESHOLDS = {
    # Criterion 1: how many identical consecutive lines trigger.
    "max_identical_run": 12,
    # Criterion 2 (primary): periodic repetition. A run of lines with a period of at most
    # `max_period`, repeated at least `min_repeats` times, triggers.
    # min_repeats is set high on purpose: legitimate code blocks do repeat, but a handful
    # of times (single digits). The signal is repetition count, not variety.
    "max_period": 20,
    "min_repeats": 8,
    # Criterion 3: short-line loop (fallback for periods longer than max_period).
    "short_line_chars": 40,
    "short_window_lines": 24,
    "short_max_distinct": 4,
}

# Frozen thresholds for tool arguments. Every number below is backed by the committed
# corpus under tests/corpus/ (27 legitimate payloads, 10 degeneracy samples); the measured
# margins are quoted in README.md and re-checked by tests/test_tool_arg_rule.py.
TOOL_ARG_THRESHOLDS = {
    # Below this size a repetition cannot carry a loop far enough to matter.
    "min_chars": 200,
    # Repetition scale that blocks a payload with no data-like structure. Chosen above the
    # largest legitimate repetition seen in a payload without a data shape (17 identical
    # lines) and below the smallest degeneracy sample (25 repetitions).
    "min_reps": 20,
    "min_identical_run": 20,
    # Share of non-empty lines that must sit inside the repeated block: a document that
    # quotes the same line 20 times and then continues is not a loop.
    "min_share": 0.6,
    "max_period": 20,
    # A structured payload is exempt while it has variety; below this many distinct lines
    # the "structure" is itself the repetition (a model emitting the same code line 60
    # times is not writing structured data).
    "min_structured_variety": 5,
    # Upper bound on the text scanned per call, so a pathological argument cannot turn the
    # guard into a denial of service.
    "scan_cap_chars": 1_000_000,
}


@dataclass(frozen=True)
class Verdict:
    triggered: bool
    reason: str          # "" | "identical_run" | "periodic_repeat" | "short_loop"
    detail: str
    line_index: int      # 0-based line that triggered; -1 when not triggered
    metric: float


@dataclass(frozen=True)
class ToolArgVerdict:
    block: bool
    reason: str          # "" | "identical_run" | "periodic_repeat"
    detail: str
    metrics: dict


def _normalize(line: str) -> str:
    return line.strip()


def check_lines(lines: Sequence[str], thresholds: dict | None = None) -> Verdict:
    """Judge a list of (already split) lines. Returns the last triggered verdict.

    Used for streamed model text. See judge_tool_arg for the tool-argument rule.
    """
    t = {**FROZEN_THRESHOLDS, **(thresholds or {})}
    norm = [_normalize(x) for x in lines]

    # Criterion 1: identical consecutive lines.
    run = 1
    for i in range(1, len(norm)):
        if norm[i] and norm[i] == norm[i - 1]:
            run += 1
            if run >= t["max_identical_run"]:
                return Verdict(
                    True, "identical_run",
                    f"{run} consecutive identical lines: {norm[i][:60]!r}",
                    i, float(run),
                )
        else:
            run = 1

    # Criterion 2 (primary): periodic repetition.
    # A real degeneracy loop is not "few distinct lines", it is "the same short block
    # repeated many times". Count repetitions rather than measuring diversity.
    mp, mr = t["max_period"], t["min_repeats"]
    n = len(norm)
    for p in range(1, mp + 1):
        i = 0
        while i + p * mr <= n:
            period = norm[i:i + p]
            if not any(period):
                i += 1
                continue
            reps = 1
            j = i + p
            while j + p <= n and norm[j:j + p] == period:
                reps += 1
                j += p
            if reps >= mr:
                return Verdict(
                    True, "periodic_repeat",
                    f"period of {p} lines repeated {reps} times: {period[:3]!r}",
                    j - 1, float(reps),
                )
            i = j if reps > 1 else i + 1

    # Criterion 3: short-line loop (fallback for degenerate shapes whose period is longer
    # than max_period).
    sw, sc, smax = t["short_window_lines"], t["short_line_chars"], t["short_max_distinct"]
    for end in range(sw, len(norm) + 1):
        win = [x for x in norm[end - sw:end] if x]
        if len(win) < sw:
            continue
        if all(len(x) <= sc for x in win) and len(set(win)) <= smax:
            return Verdict(
                True, "short_loop",
                f"{sw} consecutive short lines with only {len(set(win))} distinct",
                end - 1, float(len(set(win))),
            )

    return Verdict(False, "", "no degeneracy detected", -1, 0.0)


def check_text(text: str, thresholds: dict | None = None) -> Verdict:
    return check_lines(text.splitlines(), thresholds)


# -- Tool-argument rule -----------------------------------------------------------------
_SEPARATOR = re.compile(r"^[-=_*~#+·—\s]+$")
_NUMERIC_ROW = re.compile(r"^[\[\]{}()0-9.,;\-+eE\s]+$")
_HTML_ROW = re.compile(r"<\s*(tr|td|th|option|li|rect|circle|line|path)\b", re.I)
_TABLE_ROW = re.compile(r"^\|.*\|$")
_YAML_ITEM = re.compile(r"^\s*(?:-\s+\S|[\w.-]+\s*:\s*\S)")
_CHECKLIST = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s")
_LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_CODE_LINE = re.compile(r"[;,{}()\[\]]\s*$")
_WORD = re.compile(r"\S+")


def _detect_structure(lines: Sequence[str]) -> str:
    """Name the data-like shape of the payload, or "" when it has none.

    Deliberately conservative: anything recognised here is *excused* from the block rule
    only when it also carries variety (see judge_tool_arg).
    """
    body = [x for x in lines if x]
    if len(body) < 3:
        return ""
    text = "\n".join(lines)
    try:
        json.loads(text)
        return "json"
    except Exception:
        pass
    if all(json_line.startswith("{") and json_line.endswith("}") for json_line in body[:5]):
        try:
            for json_line in body[:20]:
                json.loads(json_line)
            return "jsonl"
        except Exception:
            pass
    if sum(1 for x in body if _TABLE_ROW.match(x)) >= 3:
        return "markdown_table"
    if sum(1 for x in body if _HTML_ROW.search(x)) >= 3:
        return "html_rows"
    if sum(1 for x in body if _NUMERIC_ROW.match(x)) >= 3:
        return "numeric_rows"
    if sum(1 for x in body if _SEPARATOR.match(x)) >= 3:
        return "separator"
    if sum(1 for x in body if _CHECKLIST.match(x)) >= 3:
        return "checklist"
    for delim in (",", ";", "\t", "|"):
        rows = [x for x in body if delim in x]
        if len(rows) >= 3:
            counts = {x.count(delim) for x in rows}
            if len(counts) == 1 and counts.pop() >= 1:
                return "delimited"
    if sum(1 for x in body if _YAML_ITEM.match(x)) >= max(3, len(body) // 2):
        return "yaml"
    if sum(1 for x in body if _LOG_LINE.match(x)) >= 3:
        return "log"
    if sum(1 for x in body if _CODE_LINE.search(x)) >= max(3, len(body) // 2):
        return "code"
    return ""


@dataclass(frozen=True)
class _Repetition:
    period: int
    reps: int
    lines: int          # period * reps
    distinct: int       # distinct lines inside the repeated block


def _max_repetition(lines: Sequence[str], max_period: int) -> _Repetition:
    """Largest periodic block (by line count) in the payload."""
    n = len(lines)
    best = _Repetition(0, 1, 0, 0)
    for p in range(1, max_period + 1):
        i = 0
        while i + p <= n:
            block = lines[i:i + p]
            if not any(block):
                i += 1
                continue
            reps = 1
            j = i + p
            while j + p <= n and lines[j:j + p] == block:
                reps += 1
                j += p
            if reps * p > best.lines:
                best = _Repetition(p, reps, reps * p, len(set(block)))
            i = j if reps > 1 else i + 1
    return best


def judge_tool_arg(text: str, thresholds: dict | None = None) -> ToolArgVerdict:
    """Decide whether a tool argument is degenerate output rather than data.

    The rule, in order:
    1. Too small to matter -> allow.
    2. Repetition carried by a data-like shape with variety -> allow (this is the case
       Hermes core explicitly allows through).
    3. >= min_identical_run identical consecutive lines -> block.
    4. a block of period <= max_period repeated >= min_reps times and covering at least
       min_share of the non-empty lines -> block.
    5. otherwise -> allow.
    """
    t = {**TOOL_ARG_THRESHOLDS, **(thresholds or {})}
    if not text or len(text) < t["min_chars"]:
        return ToolArgVerdict(False, "", "below min_chars", {"chars": len(text or "")})

    cap = t["scan_cap_chars"]
    scanned = text if len(text) <= cap else text[: cap // 2] + text[-cap // 2:]

    norm = [_normalize(x) for x in scanned.splitlines()]
    body = [x for x in norm if x]
    if not body:
        return ToolArgVerdict(False, "", "no content", {"chars": len(text)})

    run = best_run = 1
    for i in range(1, len(body)):
        run = run + 1 if body[i] == body[i - 1] else 1
        best_run = max(best_run, run)

    rep = _max_repetition(body, t["max_period"])
    share = rep.lines / len(body)
    kind = _detect_structure(scanned.splitlines())
    variety = rep.distinct if rep.lines else len(set(body))

    metrics = {
        "chars": len(text),
        "lines": len(body),
        "distinct": len(set(body)),
        "max_identical_run": best_run,
        "period": rep.period,
        "reps": rep.reps,
        "rep_lines": rep.lines,
        "share": round(share, 3),
        "structure": kind or "none",
        "structure_variety": variety,
    }

    if kind and variety >= t["min_structured_variety"]:
        return ToolArgVerdict(False, "", f"structured payload ({kind})", metrics)
    if kind in ("json", "markdown_table", "html_rows", "numeric_rows", "separator"):
        return ToolArgVerdict(False, "", f"structured payload ({kind})", metrics)
    if kind == "delimited" and rep.distinct >= 2:
        return ToolArgVerdict(False, "", "delimited payload with varied rows", metrics)

    if best_run >= t["min_identical_run"]:
        return ToolArgVerdict(
            True, "identical_run",
            f"{best_run} consecutive identical lines in a {len(body)}-line argument",
            metrics,
        )
    if rep.reps >= t["min_reps"] and share >= t["min_share"]:
        return ToolArgVerdict(
            True, "periodic_repeat",
            f"{rep.period}-line block repeated {rep.reps} times "
            f"({int(share * 100)}% of the argument, structure: {kind or 'none'})",
            metrics,
        )
    return ToolArgVerdict(False, "", "repetition below the block threshold", metrics)


def count_metrics(text: str) -> dict:
    """Diagnostics: raw quantities behind each criterion, for human review."""
    lines = [_normalize(x) for x in text.splitlines()]
    nonempty = [x for x in lines if x]
    if not nonempty:
        return {"lines": 0}
    run = best = 1
    for i in range(1, len(nonempty)):
        run = run + 1 if nonempty[i] == nonempty[i - 1] else 1
        best = max(best, run)
    return {
        "lines": len(lines),
        "nonempty": len(nonempty),
        "distinct": len(set(nonempty)),
        "max_identical_run": best,
        "longest_line": max(len(x) for x in nonempty),
    }


# -- Streaming wrapper (used by on_stream_delta) ---------------------------------------
class StreamBreakerState:
    """Judge a stream incrementally instead of re-scanning the whole buffer per chunk.

    Cost per chunk is constant: complete lines go into a bounded window, and the window is
    judged every ``CHECK_EVERY_LINES`` new lines. Total work is therefore linear in the
    length of the reply (see tests/test_stream_cost.py), while the verdict is unchanged:
    the criteria only ever look at a bounded number of consecutive lines.
    """

    WINDOW_LINES = 260          # > max_period * min_repeats (20 * 8) and >> short_window
    CHECK_EVERY_LINES = 10

    def __init__(self, thresholds: dict | None = None, max_chars: int = 400_000,
                 check_every_lines: int | None = None):
        self.thresholds = {**FROZEN_THRESHOLDS, **(thresholds or {})}
        self._pending = ""
        self._window: deque[str] = deque(maxlen=self.WINDOW_LINES)
        self._lines_kept = 0
        self._kept: list[str] = []          # text kept for the incident tail, bounded
        self._kept_chars = 0
        self._new_since_check = 0
        self._max_chars = max_chars
        self._check_every = check_every_lines or self.CHECK_EVERY_LINES
        self.verdict: Verdict | None = None

    def feed(self, delta: str) -> Verdict | None:
        """Feed one chunk; return a newly triggered verdict, else None."""
        if self.verdict is not None:
            return None                      # already latched
        delta = delta or ""
        if not delta:
            return None
        self._pending += delta
        if "\n" not in self._pending:
            self._trim_pending()
            return None
        parts = self._pending.split("\n")
        self._pending = parts.pop()          # last part is an unterminated line
        if len(parts) > self.WINDOW_LINES:   # a single huge chunk: keep the tail
            parts = parts[-self.WINDOW_LINES:]
        for line in parts:
            self._window.append(line)
            self._keep(line + "\n")
        self._new_since_check += len(parts)
        if self._new_since_check < self._check_every:
            return None
        self._new_since_check = 0
        v = check_lines(list(self._window), self.thresholds)
        if v.triggered:
            self.verdict = v
            return v
        return None

    def _trim_pending(self) -> None:
        if len(self._pending) > self._max_chars:
            self._pending = self._pending[-self._max_chars:]

    def _keep(self, chunk: str) -> None:
        self._kept.append(chunk)
        self._kept_chars += len(chunk)
        while self._kept_chars > self._max_chars and len(self._kept) > 1:
            self._kept_chars -= len(self._kept.pop(0))

    @property
    def text(self) -> str:
        return "".join(self._kept) + self._pending
