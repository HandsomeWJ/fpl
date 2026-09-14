"""Daily price snapshots - the one Phase 1 piece that cannot wait.

FPL publishes no price history. Every day without a snapshot is price-analytics data
that can never be recovered, so capture starts here, in the simplest durable store we
already have (this repo), and Phase 1's importer loads these files into Postgres later.

Two slots per UK date, keyed by the date the prices are in effect:
  pre   the last PRE_WINDOW_MIN before midnight UK  - the predictive signal (transfers
        in/out so far) at its most complete; overwritten within the window, latest wins
  post  the first POST_WINDOW_MIN after midnight UK - the realised price change;
        written once

Files: data/prices/<uk-date>/<slot>.json.gz  (slim: players + teams + meta)
       data/prices/latest.json                (uncompressed copy of the newest snapshot)

Runs on every clock tick of the copycat workflow BEFORE the run gate, from the public
endpoint, so it costs no FPL auth and cannot interfere with mirroring.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from .fix import UA
from .fpl import FPL
from .log import log
from .schedule import UK_TZ, minutes_to_price_change

PRE_WINDOW_MIN = 35
POST_WINDOW_MIN = 75

PLAYER_FIELDS = [
    "id", "web_name", "first_name", "second_name", "team", "element_type",
    "now_cost", "cost_change_event", "cost_change_event_fall",
    "cost_change_start", "cost_change_start_fall",
    "selected_by_percent", "transfers_in_event", "transfers_out_event",
    "transfers_in", "transfers_out",
    "status", "news", "chance_of_playing_next_round",
    "total_points", "event_points", "form", "minutes",
]
TEAM_FIELDS = ["id", "name", "short_name", "strength", "strength_overall_home",
               "strength_overall_away", "strength_attack_home", "strength_attack_away",
               "strength_defence_home", "strength_defence_away"]


def slot_for(now: datetime) -> tuple[Optional[str], Optional[str]]:
    """(slot, uk_date) for this instant, or (None, None) outside both windows.

    `uk_date` is the UK calendar date whose prices the snapshot describes: a `pre`
    snapshot late on D describes D; a `post` snapshot just after midnight describes
    the new day D+1.
    """
    if UK_TZ is None:
        return None, None
    mins = minutes_to_price_change(now)
    uk_now = now.astimezone(UK_TZ)
    if mins is not None and mins <= PRE_WINDOW_MIN:
        return "pre", uk_now.date().isoformat()
    since_last = 24 * 60 - mins if mins is not None else None
    if since_last is not None and since_last <= POST_WINDOW_MIN:
        return "post", uk_now.date().isoformat()
    return None, None


def slim(bootstrap: dict, now: datetime, slot: str, uk_date: str) -> dict:
    events = bootstrap.get("events", [])
    cur = next((e for e in events if e.get("is_current")), None)
    nxt = next((e for e in events if e.get("is_next")), None)
    return {
        "meta": {
            "fetched_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "uk_date": uk_date,
            "slot": slot,
            "event_current": cur["id"] if cur else None,
            "event_next": nxt["id"] if nxt else None,
            "deadline_next": nxt["deadline_time"] if nxt else None,
        },
        "teams": [{k: t.get(k) for k in TEAM_FIELDS} for t in bootstrap.get("teams", [])],
        "players": [{k: e.get(k) for k in PLAYER_FIELDS} for e in bootstrap["elements"]],
    }


def fetch_bootstrap(http=requests) -> dict:
    r = http.get(f"{FPL}/api/bootstrap-static/", headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    return r.json()


def take_snapshot(now: datetime, data_dir: str, http=requests,
                  force: bool = False) -> Optional[str]:
    """Write the snapshot for this instant if a window is open. Returns the path
    written, or None. Never raises past a logged failure - the caller must be able to
    treat this as strictly optional relative to mirroring."""
    slot, uk_date = slot_for(now)
    if slot is None:
        if not force:
            return None
        slot = "manual"
        uk_date = (now.astimezone(UK_TZ) if UK_TZ else now).date().isoformat()
    day_dir = os.path.join(data_dir, "prices", uk_date)
    path = os.path.join(day_dir, f"{slot}.json.gz")
    if slot == "post" and os.path.exists(path):
        log(f"[snapshot] post snapshot for {uk_date} already captured; skipping.")
        return None
    bootstrap = fetch_bootstrap(http=http)
    doc = slim(bootstrap, now, slot, uk_date)
    os.makedirs(day_dir, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    with open(os.path.join(data_dir, "prices", "latest.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    rel = os.path.relpath(path, os.path.dirname(data_dir))
    log(f"[snapshot] wrote {rel} ({len(doc['players'])} players, {slot} for {uk_date})")
    return path


def safe_take_snapshot(now: datetime, data_dir: str, http=requests,
                       force: bool = False) -> Optional[str]:
    try:
        return take_snapshot(now, data_dir, http=http, force=force)
    except Exception as e:  # the snapshot must never block the mirror
        log(f"[snapshot] failed: {e}")
        return None
