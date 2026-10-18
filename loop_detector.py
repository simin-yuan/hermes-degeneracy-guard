"""Degeneracy-loop detector: pure functions, no side effects, independently testable.

Design constraints:
- Thresholds are frozen before the run, never tuned while running (FROZEN_THRESHOLDS is a
  module constant; changing it means changing this file and leaving a diff).
- The unit of detection is "this generation", not "this session", so it must accept
  streaming deltas.
- It only decides. What to do about a verdict (abort, record, replace) belongs to the
  caller.

Why the detector exists at all: a model inside a repeat loop can report that it is
repeating and still cannot stop. Self-report is not self-control, so the check has to
run outside the model.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class Verdict:
    triggered: bool
    reason: str          # "" | "identical_run" | "periodic_repeat" | "short_loop"
    detail: str
    line_index: int      # 0-based line that triggered; -1 when not triggered
    metric: float


def _normalize(line: str) -> str:
    return line.strip()


def check_lines(lines: Sequence[str], thresholds: dict | None = None) -> Verdict:
    """Judge a list of (already split) lines. Returns the last triggered verdict."""
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


# -- Streaming wrapper (used by on_stream_delta) ---------------------------------------
class StreamBreakerState:
    """Accumulate streaming deltas and judge incrementally instead of re-scanning a text.

    The verdict is idempotent: once triggered it stays triggered and is returned once.
    """

    def __init__(self, thresholds: dict | None = None, max_chars: int = 400_000):
        self.thresholds = {**FROZEN_THRESHOLDS, **(thresholds or {})}
        self._buf: list[str] = []
        self._checked_lines = 0
        self._max_chars = max_chars
        self.verdict: Verdict | None = None

    def feed(self, delta: str) -> Verdict | None:
        """Feed one chunk; return a newly triggered verdict, else None."""
        if self.verdict is not None:
            return None                      # already latched
        self._buf.append(delta or "")
        joined = "".join(self._buf)
        if len(joined) > self._max_chars:
            joined = joined[-self._max_chars:]
            self._buf = [joined]
        lines = joined.splitlines()
        v = check_lines(lines, self.thresholds)
        if v.triggered:
            self.verdict = v
            return v
        return None

    @property
    def text(self) -> str:
        return "".join(self._buf)


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
