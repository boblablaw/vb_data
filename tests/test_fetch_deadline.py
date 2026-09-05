"""Unit tests for the fetch hard-deadline (no network, no browser).

``_hard_deadline`` is the backstop that turns a wedged ``page.content()`` (which has no timeout and
hangs on Akamai interstitials) into a retryable error, so an unattended season sweep can't stall.
"""
from __future__ import annotations

import threading
import time

import pytest

from vb.fetch.ncaa_fetch import FetchDeadlineError, _hard_deadline


def test_deadline_fires_on_overrun():
    start = time.monotonic()
    with pytest.raises(FetchDeadlineError), _hard_deadline(0.2):
        time.sleep(2.0)  # simulate a wedged page.content()
    # Interrupted near the deadline, nowhere near the full 2s sleep.
    assert time.monotonic() - start < 1.0


def test_no_error_when_block_completes_in_time():
    with _hard_deadline(2.0):
        time.sleep(0.05)  # fast, well under the deadline


def test_timer_is_cleared_after_success():
    # A completed block must not leave an armed alarm that fires into later code.
    with _hard_deadline(0.2):
        pass
    time.sleep(0.5)  # would raise here if the itimer were still armed


def test_noop_off_main_thread():
    # Signals only fire on the main thread; off it the deadline must be an inert no-op (it falls back
    # to Playwright's own per-call timeouts) rather than raising or corrupting the run.
    result: dict = {}

    def worker():
        try:
            with _hard_deadline(0.1):
                time.sleep(0.4)
            result["ok"] = True
        except BaseException as e:
            result["err"] = e

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert result.get("ok") is True and "err" not in result
