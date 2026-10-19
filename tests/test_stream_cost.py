"""Stream scoring must be linear in the length of the reply.

An earlier version re-joined and re-scanned the whole buffer on every chunk: a 102,735
character reply fed in 24-character chunks cost about 34 seconds of CPU, and the cost grew
with the square of the reply. The window-based scan below is bounded per chunk.
"""

import time

import _pkg

ld = _pkg.submodule("loop_detector")

CHUNK = 24
REPLY = 102_735


def _healthy_reply(n_chars):
    line = "The command completed successfully and the next step is to inspect the output.\n"
    return (line * (n_chars // len(line) + 1))[:n_chars]


def _feed(text, chunk=CHUNK):
    state = ld.StreamBreakerState()
    start = time.perf_counter()
    for i in range(0, len(text), chunk):
        state.feed(text[i:i + chunk])
    return time.perf_counter() - start


def test_long_reply_is_cheap():
    elapsed = _feed(_healthy_reply(REPLY))
    # The previous implementation took ~34 s on this machine; the bound is deliberately
    # loose so a slow CI runner does not fail the build, but far below the old cost.
    assert elapsed < 8.0, f"{elapsed:.2f}s for {REPLY} chars in {CHUNK}-char chunks"


def test_cost_grows_linearly_not_quadratically():
    small = _feed(_healthy_reply(REPLY // 4))
    large = _feed(_healthy_reply(REPLY))
    # Linear work quadruples when the input quadruples; quadratic work multiplies by ~16.
    assert large < max(0.5, small * 8), f"small={small:.2f}s large={large:.2f}s"


def test_detection_is_unchanged_by_the_incremental_scan():
    """The verdict for a degenerate stream must match the one-shot verdict."""
    text = "\n".join(["OK.", "Done.", "Write."] * 40) + "\n"
    state = ld.StreamBreakerState()
    verdicts = [state.feed(text[i:i + 7]) for i in range(0, len(text), 7)]
    triggered = [v for v in verdicts if v is not None]
    assert len(triggered) == 1
    assert triggered[0].triggered
    assert ld.check_text(text).triggered


def test_window_survives_a_single_huge_chunk():
    text = "\n".join(["same line"] * 300)
    state = ld.StreamBreakerState()
    assert state.feed(text) is not None
