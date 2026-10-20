"""Retries for the reads every run depends on.

FPL's API answers 500 now and then, account.premierleague.com sits behind a gateway
that times out, and the reveal site occasionally hangs. None of those is a reason to
page the owner: retry briefly inside the tick, and if it still fails raise
UpstreamUnavailable so the caller records a skipped tick and the next one retries.
"""

from __future__ import annotations

import time
from typing import Callable

import requests

from .log import log

TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_DELAYS = (2, 5)        # seconds before the 2nd and 3rd attempt
SLEEP = time.sleep           # tests replace this


class UpstreamUnavailable(RuntimeError):
    """An upstream service answered 5xx/429 or not at all, even after retries.
    Transient by definition: nothing about our credentials or plan is wrong."""


def get_with_retry(request: Callable[[], requests.Response], what: str):
    """Run `request()` (which returns a Response), retrying connection errors and
    transient statuses. Returns the final response - a 4xx is for the caller to judge."""
    attempts = 1 + len(RETRY_DELAYS)
    why = ""
    for i in range(attempts):
        if i:
            SLEEP(RETRY_DELAYS[i - 1])
        try:
            r = request()
        except requests.RequestException as e:
            why = f"{type(e).__name__}: {e}"
        else:
            if r.status_code not in TRANSIENT_STATUSES:
                return r
            why = f"HTTP {r.status_code} {r.text[:120]}"
        log(f"[http] {what} unavailable, attempt {i + 1}/{attempts}: {why}")
    raise UpstreamUnavailable(f"{what} unavailable after {attempts} attempts ({why}); "
                              "the next run retries.")
