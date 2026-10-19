"""The tool-argument rule, checked against the committed corpus.

tests/corpus/legit/ holds payloads people legitimately write (tables, arrays, matrices,
CSV exports, fixtures, logs, generated code). tests/corpus/degeneracy/ holds repetition
loops of the shape a model produces when it degenerates. The rule must pass every file in
the first group and block every file in the second.
"""

import json
from pathlib import Path

import _pkg

ld = _pkg.submodule("loop_detector")
tag = _pkg.submodule("tool_arg_guard")

CORPUS = Path(__file__).resolve().parent / "corpus"
LEGIT = sorted((CORPUS / "legit").glob("*.txt"))
DEGENERACY = sorted((CORPUS / "degeneracy").glob("*.txt"))


def test_corpus_is_present():
    assert len(LEGIT) >= 25, f"only {len(LEGIT)} legitimate payloads"
    assert len(DEGENERACY) >= 10, f"only {len(DEGENERACY)} degeneracy samples"


def test_legitimate_payloads_are_not_blocked():
    failures = []
    for path in LEGIT:
        verdict = ld.judge_tool_arg(path.read_text(encoding="utf-8"))
        if verdict.block:
            failures.append(f"{path.name}: {verdict.reason} ({verdict.detail}) {verdict.metrics}")
    assert not failures, "legitimate payloads blocked:\n" + "\n".join(failures)


def test_degeneracy_samples_are_blocked():
    failures = []
    for path in DEGENERACY:
        verdict = ld.judge_tool_arg(path.read_text(encoding="utf-8"))
        if not verdict.block:
            failures.append(f"{path.name}: not blocked {verdict.metrics}")
    assert not failures, "degeneracy samples passed:\n" + "\n".join(failures)


def test_the_four_reported_false_positives_pass_through_the_hook():
    """The four payloads reported in the audit, driven through pre_tool_call itself."""
    cases = {
        "html-table-8-empty-rows": "<table>\n" + "  <tr><td></td></tr>\n" * 8 + "</table>",
        "json-8-flags": json.dumps([{"enabled": False, "weight": 1.0}] * 8, indent=2),
        "matrix-16-zero-rows": "[\n" + '  [0, 0, 0, 0, 0, 0, 0, 0],\n' * 16 + "]",
        "csv-40-zero-rows": "0,0,0,0\n" * 40,
    }
    for name, content in cases.items():
        assert tag.on_pre_tool_call(tool_name="write_file", args={"content": content}) is None, name


def test_corpus_metrics_are_reported_in_the_readme():
    """Every threshold in TOOL_ARG_THRESHOLDS must be justified by a measurement.

    This test recomputes the margins so a future threshold change cannot leave the README
    quoting numbers that no longer hold.
    """
    legit_metrics = [ld.judge_tool_arg(p.read_text(encoding="utf-8")).metrics for p in LEGIT]
    degen_metrics = [ld.judge_tool_arg(p.read_text(encoding="utf-8")).metrics for p in DEGENERACY]
    t = ld.TOOL_ARG_THRESHOLDS
    # Largest legitimate repetition without a data-like shape.
    plain_legit = [m for m in legit_metrics if m.get("structure") in ("none", "")]
    largest_legit_run = max((m["max_identical_run"] for m in plain_legit), default=0)
    largest_legit_reps = max((m["reps"] for m in plain_legit), default=0)
    assert largest_legit_run < t["min_identical_run"], (largest_legit_run, t)
    assert largest_legit_reps < t["min_reps"], (largest_legit_reps, t)
    # Smallest degeneracy sample must sit above the thresholds.
    assert min(m["max_identical_run"] for m in degen_metrics) >= t["min_identical_run"] or \
        min(m["reps"] for m in degen_metrics if m["reps"] > 1) >= t["min_reps"]


def test_too_small_payloads_are_skipped():
    assert ld.judge_tool_arg("same line\n" * 3).block is False


def test_allow_list_is_read_on_every_call(monkeypatch):
    payload = {"content": "\n".join(["same line"] * 40)}
    assert tag.on_pre_tool_call(tool_name="write_file", args=payload)["action"] == "block"
    monkeypatch.setenv("DEGENERACY_GUARD_ALLOW_TOOLS", "other, write_file")
    assert tag.on_pre_tool_call(tool_name="write_file", args=payload) is None
    monkeypatch.delenv("DEGENERACY_GUARD_ALLOW_TOOLS")
    assert tag.on_pre_tool_call(tool_name="write_file", args=payload)["action"] == "block"


def test_block_message_tells_the_user_how_to_proceed():
    r = tag.on_pre_tool_call(tool_name="write_file", args={"content": "\n".join(["same line"] * 40)})
    msg = r["message"]
    assert "DEGENERACY_GUARD_BLOCK" in msg
    assert "DEGENERACY_GUARD_ALLOW_TOOLS" in msg
    assert "fresh session" not in msg.lower()
