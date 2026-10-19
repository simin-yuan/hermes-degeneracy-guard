"""Degeneracy-loop detector: pure functions, no side effects, independently testable.

Two jobs, deliberately kept apart:

- ``check_lines`` / ``check_text`` judge streamed *model text*. Thresholds were frozen
  before the first run and the stream path keeps them, so a change here is a visible diff.
- ``judge_tool_arg`` judges *tool arguments*. A tool argument is data, not prose: a table,
  a JSON array, a matrix or a CSV export may repeat on purpose, and Hermes core lets such a
  call through on purpose (``agent/chat_completion_helpers.py``: "a provider-confirmed call
  may legitimately write repetitive data"). The tool-argument rule therefore asks a second
  question first: does the repetition look like data at all? And a third: content cannot
  separate intent from pathology, so instead of refusing, the verdict is "ask the user".

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
# corpus under tests/corpus/ and by a hold-out set that was written without sight of these
# rules (numbers in README.md); tests/test_tool_arg_rule.py re-checks the corpus.
TOOL_ARG_THRESHOLDS = {
    # Below this size a repetition cannot carry a loop far enough to matter.
    "min_chars": 200,
    # Below this many non-empty lines the payload is a snippet, not a body of output.
    "min_lines": 12,
    # Repetition scale that is worth a human's attention when the payload has no data shape:
    # one line repeated, or a short block repeated.
    "min_reps": 18,
    "min_identical_run": 18,
    # Share of non-empty lines that must sit inside the repeated block: a document that
    # quotes the same line 18 times and then continues is not a loop.
    "min_share": 0.6,
    "max_period": 20,
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
    ask: bool            # True = hand this call to the human-approval gate
    reason: str          # "" | "identical_run" | "periodic_repeat" | "short_loop"
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
# Recognisers for "this payload has a data shape". They answer ONE question: could a person
# have written this on purpose? Anything recognised here is left alone — never blocked, never
# escalated — because a table, a matrix, a SQL script or a log tail may repeat by design.
_SEPARATOR = re.compile(r"^[-=_*~#+·—\s]+$")
_NUMERIC_ROW = re.compile(r"^[\[\]{}()0-9.,;:\-+eE\s*/%=]+$")
_TABLE_ROW = re.compile(r"^\|.*\|$")
_CHECKLIST = re.compile(r"^[-*+]\s+\[[ xX-]\]")
_YAML_ITEM = re.compile(r"^-\s|^-\s*$")
_YAML_KV = re.compile(r"^[^\s:#{}\[\]\"';,][^:#{}\[\]\"';,]*:\s*(\S.*)?$")
_TOML_TABLE = re.compile(r"^\[\[?[A-Za-z0-9_.\"-]+\]?\]$")
_TOML_KV = re.compile(
    r'^[A-Za-z0-9_.\-]+\s*=\s*("[^"]*"|\'[^\']*\'|\[[^\]]*\]|\{[^}]*\}|[^\s+\-*/=]{0,120})$')
_XML_LINE = re.compile(
    r"^(</?[A-Za-z][\w:.-]*(\s[^<>]*)?/?>(</?[A-Za-z][\w:.-]*(\s[^<>]*)?/?>)*|<!--.*-->)$")
_SQL_LINE = re.compile(
    r"^(insert|update|delete|select|create|alter|drop|with|values|replace|commit)\b", re.I)
_LOG_PREFIX = re.compile(
    r"^(\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\[?\d{1,2}:\d{2}:\d{2}|[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2})")
_LOG_LEVEL = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|FATAL|CRITICAL)\b")
# A code *element* (a list/dict entry ending in a comma, a bracket-only line) is data; a code
# *statement* is not. "return f()  # recursion fixed" matches neither, which is the point.
_CODE_ELEMENT = re.compile(r"^[\[\]{}(),;]*\s*$|,\s*$")
_KIND_MIN_RATIO = 0.6
_PROSE_END = ".!?。！？…；;:"


def _is_prose_line(line: str) -> bool:
    """True when a line reads like a sentence rather than a data record.

    Data has grammar (delimiters, tags, terminators); prose has sentences. This is the only
    signal the guard trusts when it decides whether repetition is worth a human's attention.
    """
    s = line.strip()
    if not s:
        return False
    if s[-1] in _PROSE_END:
        return True
    if re.search(r"[;,{}()\[\]=<>|@$\\]", s):
        return False
    words = s.split()
    if len(words) <= 4 and sum(1 for ch in s if ch.isalpha() or ord(ch) > 0x2E80) >= len(s) * 0.6:
        return True
    return False


def _detect_structure(lines: Sequence[str]) -> str:
    """Name the data-like shape of the payload, or "" when it has none."""
    body = [x.strip() for x in lines if x.strip()]
    if len(body) < 3:
        return ""
    text = "\n".join(lines)
    try:
        json.loads(text)
        return "json"
    except Exception:
        pass
    head = body[:20]
    if head and all(x.startswith("{") and x.endswith("}") for x in head):
        try:
            for x in head:
                json.loads(x)
            return "jsonl"
        except Exception:
            pass

    n = len(body)
    checks = (
        ("markdown_table", _TABLE_ROW.match),
        ("checklist", _CHECKLIST.match),
        ("xml", _XML_LINE.match),
        ("sql", _SQL_LINE.match),
        ("toml_table", _TOML_TABLE.match),
        ("toml_kv", _TOML_KV.match),
        ("yaml", lambda x: bool(_YAML_ITEM.match(x) or _YAML_KV.match(x))),
        ("log", lambda x: bool(_LOG_PREFIX.match(x) or _LOG_LEVEL.search(x))),
    )
    for name, pred in checks:
        hits = sum(1 for x in body if pred(x))
        if hits >= 3 and hits >= n * _KIND_MIN_RATIO:
            return name
    hits = sum(1 for x in body if _NUMERIC_ROW.match(x) and any(c.isdigit() for c in x))
    if hits >= 3 and hits >= n * _KIND_MIN_RATIO:
        return "numeric_rows"
    hits = sum(1 for x in body if _SEPARATOR.match(x))
    if hits >= 3 and hits >= n * _KIND_MIN_RATIO:
        return "separator"
    # Delimited records: the same field count on most lines (a CSV/TSV export). One delimiter
    # per line is not enough — prose with a comma is not a table — and a table whose rows are
    # all the same row is not a table either, it is the loop wearing a table's clothes.
    for delim in (",", ";", "\t", "|"):
        rows = [x for x in body if delim in x]
        if len(rows) >= 3 and len(rows) >= n * _KIND_MIN_RATIO:
            if len({x.count(delim) for x in rows}) == 1 and len(set(rows)) >= 2:
                return "delimited"
    # Code *elements* (list/dict entries ending in a comma, bracket-only lines) are data.
    # Code *statements* are not: a model repeating `count += 1` fifty times is a loop.
    hits = sum(1 for x in body if _CODE_ELEMENT.search(x))
    if hits >= 3 and hits >= n * _KIND_MIN_RATIO:
        return "code_elements"
    return ""


@dataclass(frozen=True)
class _Repetition:
    period: int
    reps: int
    lines: int          # period * reps
    distinct: int       # distinct lines inside the repeated block
    block: tuple        # the repeated unit itself


def _max_repetition(lines: Sequence[str], max_period: int) -> _Repetition:
    """Largest periodic block (by line count) in the payload."""
    n = len(lines)
    best = _Repetition(0, 1, 0, 0, ())
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
                best = _Repetition(p, reps, reps * p, len(set(block)), tuple(block))
            i = j if reps > 1 else i + 1
    return best


def judge_tool_arg(text: str, thresholds: dict | None = None) -> ToolArgVerdict:
    """Decide whether a tool argument deserves a human's attention before it runs.

    This does not decide right and wrong, because content alone cannot: a thousand-row
    zero matrix may be data, and the same thousand rows may be a model stuck in a loop.
    Only the user knows. So the rule asks a narrow question — "is this repetition carrying
    nothing but sentences?" — and if so hands the call to the approval gate.

    The rule, in order:
    1. Too small / too few lines to matter -> allow.
    2. The payload has a data shape (table, matrix, SQL, YAML/TOML, XML, log, checklist,
       delimited records, code literals) -> allow. Repetition there is expected.
    3. No repetition worth mentioning -> allow.
    4. The repeating unit reads as prose -> escalate. Otherwise -> allow.
    """
    t = {**TOOL_ARG_THRESHOLDS, **(thresholds or {})}
    if not text or len(text) < t["min_chars"]:
        return ToolArgVerdict(False, "", "below min_chars", {"chars": len(text or "")})

    cap = t["scan_cap_chars"]
    scanned = text if len(text) <= cap else text[: cap // 2] + text[-cap // 2:]

    norm = [_normalize(x) for x in scanned.splitlines()]
    body = [x for x in norm if x]
    if len(body) < t["min_lines"]:
        return ToolArgVerdict(False, "", "too few lines", {"chars": len(text), "lines": len(body)})

    run = best_run = 1
    for i in range(1, len(body)):
        run = run + 1 if body[i] == body[i - 1] else 1
        best_run = max(best_run, run)

    rep = _max_repetition(body, t["max_period"])
    share = rep.lines / len(body)
    kind = _detect_structure(scanned.splitlines())

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
    }

    if kind:
        metrics["prose_share"] = 0.0
        return ToolArgVerdict(False, "", f"data-shaped payload ({kind})", metrics)

    run_hit = best_run >= t["min_identical_run"]
    unit = (body[-1],) if run_hit else rep.block
    prose = sum(1 for x in unit if _is_prose_line(x)) / max(1, len(unit))
    metrics["prose_share"] = round(prose, 3)
    if not run_hit and not (rep.reps >= t["min_reps"] and share >= t["min_share"]):
        return ToolArgVerdict(False, "", "repetition below the ask threshold", metrics)

    if run_hit:
        return ToolArgVerdict(
            True, "identical_run",
            f"{best_run} consecutive identical sentences in a {len(body)}-line argument: "
            f"{body[-1][:60]!r}", metrics)
    return ToolArgVerdict(
        True, "periodic_repeat",
        f"{rep.period}-line block repeated {rep.reps} times "
        f"({int(share * 100)}% of a {len(body)}-line argument): {unit[0][:60]!r}", metrics)


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
