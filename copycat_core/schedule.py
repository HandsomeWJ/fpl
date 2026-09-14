"""When a clock-started run should do real work.

The clock fires every 15 minutes (cron-job.org via workflow_dispatch, with GitHub's
own cron as an unreliable fallback), but real work happens only in the 150 minutes
before the FPL price change and in the 6 hours before a gameweek deadline. In the
owner's clock (SGT): summer 04:37-06:52, winter 05:37-07:52, plus deadline day.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .fpl import public_next_deadline
from .log import log
from .settings import Settings

PRICE_LEAD_MIN = 150         # start running this many minutes before the price change
DEADLINE_WINDOW_H = 6        # always run this many hours before a GW deadline

try:
    from zoneinfo import ZoneInfo
    UK_TZ = ZoneInfo("Europe/London")
except Exception:                                    # no tzdata on the runner
    UK_TZ = None


def minutes_to_price_change(now: Optional[datetime] = None) -> Optional[float]:
    """Minutes until the next FPL price change.

    For 2026/27 prices move at MIDNIGHT UK time, which is 23:00 UTC under BST and
    00:00 UTC under GMT. Computing it in Europe/London keeps that correct across the
    DST switch instead of hard-coding UTC hours that drift twice a year. Returns None
    if the zone is unavailable, and the caller falls back to fixed hours.
    """
    now = now or datetime.now(timezone.utc)
    if UK_TZ is None:
        return None
    uk = now.astimezone(UK_TZ)
    nxt_date = uk.date() + timedelta(days=1)
    nxt = datetime(nxt_date.year, nxt_date.month, nxt_date.day, 0, 0, tzinfo=UK_TZ)
    return (nxt - uk).total_seconds() / 60.0


def should_run_now(settings: Settings, now: Optional[datetime] = None,
                   deadline_fn: Callable[[], Optional[datetime]] = public_next_deadline) -> bool:
    """Gate for clock-started runs. Fails OPEN - if the deadline can't be read the run
    proceeds, because missing a deadline is far worse than a wasted run. A human
    dispatch always runs."""
    if not settings.is_clock:
        return True
    now = now or datetime.now(timezone.utc)
    mins = minutes_to_price_change(now)
    if mins is None:
        if now.hour in (21, 22):     # fallback if Europe/London is unavailable
            log(f"[gate] {now.hour:02d}:00 UTC price slot (no tzdata); running.")
            return True
    elif 0 <= mins <= PRICE_LEAD_MIN:
        log(f"[gate] {mins:.0f}min to the price change; running.")
        return True
    try:
        dl = deadline_fn()
    except Exception as e:
        log(f"[gate] could not read the deadline ({e}); running anyway.")
        return True
    if dl is None:
        log("[gate] no open gameweek; skipping.")
        return False
    hours = (dl - now).total_seconds() / 3600.0
    if 0 < hours <= DEADLINE_WINDOW_H:
        log(f"[gate] {hours:.1f}h to the GW deadline; running.")
        return True
    log(f"[gate] {hours:.1f}h to the GW deadline, outside the {DEADLINE_WINDOW_H}h "
        f"window; {mins:.0f}min to the price change, outside {PRICE_LEAD_MIN}min. Skipping."
        if mins is not None else
        f"[gate] {hours:.1f}h to the GW deadline and not a price slot; skipping.")
    return False
