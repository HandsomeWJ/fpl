"""FPL auth and API reads.

Auth: FPL moved to OIDC (account.premierleague.com) for 2026/27. The refresh token is
exchanged for a short-lived access token sent as a Bearer header; no cookies needed.

FPL issues ONE-TIME-USE refresh tokens: each refresh returns a new one and invalidates
the old one immediately. If the rotation is not persisted the run after next presents
a dead token and dies with invalid_grant - so TokenManager escalates a failed save
instead of merely logging it.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

import requests

from .fix import UA
from .log import log
from .ports import Notifier, TokenStore

FPL = "https://fantasy.premierleague.com"
PL_TOKEN_URL = "https://account.premierleague.com/as/token"
PL_CLIENT_ID = "bfcbaf69-aade-4c1b-8f00-c1cb8a193030"


def refresh_access_token(refresh_token: str, http=requests):
    return http.post(PL_TOKEN_URL,
                     data={"grant_type": "refresh_token",
                           "refresh_token": refresh_token,
                           "client_id": PL_CLIENT_ID},
                     headers={"User-Agent": UA,
                              "Content-Type": "application/x-www-form-urlencoded"},
                     timeout=60)


class TokenManager:
    """Return a valid access token, refreshing and persisting as needed."""

    def __init__(self, store: TokenStore, seed: Optional[str], notifier: Notifier,
                 http=requests, now_fn=time.time):
        self.store, self.seed, self.notifier = store, seed, notifier
        self.http, self.now_fn = http, now_fn
        # Set when a rotated refresh token could not be persisted; the run still does
        # its work but should exit non-zero so the failure shows red as well as in an
        # issue.
        self.persist_failed = False

    def access_token(self) -> Optional[str]:
        """None if no OAuth is set up at all (caller may fall back to a cookie)."""
        tokens = self.store.load()
        now = self.now_fn()
        if tokens and tokens.get("expires_at", 0) - now > 600 and tokens.get("access_token"):
            return tokens["access_token"]
        candidates = []
        if tokens and tokens.get("refresh_token"):
            candidates.append(tokens["refresh_token"])
        if self.seed and self.seed not in candidates:
            candidates.append(self.seed)
        for rt in candidates:
            r = refresh_access_token(rt, http=self.http)
            if r.status_code < 400:
                j = r.json()
                new = {"access_token": j["access_token"],
                       "refresh_token": j.get("refresh_token", rt),
                       "expires_at": now + int(j.get("expires_in", 3600))}
                rotated = new["refresh_token"] != rt
                if not self.store.save(new) and rotated:
                    # The old token is spent and the new one is now only in this
                    # process. Finish this run (it still holds a valid access token and
                    # may have a deadline to hit), but make the breakage impossible to
                    # miss.
                    self.persist_failed = True
                    self.notifier.notify(
                        "FPL Copycat: token rotation could not be saved",
                        "The FPL refresh token rotated but writing the FPL_TOKENS Actions "
                        "variable failed, so the new token is lost when this run ends and "
                        "the next run will fail with invalid_grant.\n\n"
                        "Check that the GH_PAT secret exists, has not expired, and grants "
                        "Variables: read and write on this repo. Then re-seed "
                        "FPL_REFRESH_TOKEN from the FPL site.")
                log("[tokens] refreshed FPL access token.")
                return new["access_token"]
            log(f"[tokens] refresh attempt failed: {r.status_code} {r.text[:200]}")
        if candidates:
            raise RuntimeError("FPL token refresh failed for all stored refresh tokens - "
                               "log in to fantasy.premierleague.com again and update the "
                               "FPL_REFRESH_TOKEN secret.")
        return None


def fpl_session(token: Optional[str], fpl_cookie: Optional[str] = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": "https://fantasy.premierleague.com/",
        "Origin": "https://fantasy.premierleague.com",
        "Accept": "application/json",
    })
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
        s.headers["X-API-Authorization"] = f"Bearer {token}"
    elif fpl_cookie:
        s.headers["Cookie"] = fpl_cookie
        log("[auth] no OAuth token available - falling back to FPL_COOKIE.")
    else:
        raise RuntimeError("No FPL auth configured: set FPL_REFRESH_TOKEN (preferred) "
                           "or FPL_COOKIE.")
    return s


def get_bootstrap(s) -> dict:
    r = s.get(f"{FPL}/api/bootstrap-static/", timeout=60)
    r.raise_for_status()
    return r.json()


def get_my_team(s, entry: int) -> dict:
    r = s.get(f"{FPL}/api/my-team/{entry}/", timeout=60)
    if r.status_code in (401, 403):
        raise RuntimeError(f"FPL auth failed ({r.status_code}): {r.text[:200]}")
    r.raise_for_status()
    return r.json()


def get_my_gw_transfers(s, entry: int, event: int) -> list:
    r = s.get(f"{FPL}/api/entry/{entry}/transfers/", timeout=60)
    if r.status_code != 200:
        return []
    return [t for t in r.json() if t.get("event") == event]


def public_next_deadline(http=requests) -> Optional[datetime]:
    """Deadline of the open GW, read from the UNAUTHENTICATED bootstrap endpoint.

    Deliberately auth-free: the gate must be able to decide whether to skip without
    spending a refresh-token rotation on a run that will do nothing.
    """
    r = http.get(f"{FPL}/api/bootstrap-static/", headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    events = r.json()["events"]
    e = next((x for x in events if x["is_next"]), None) or \
        next((x for x in events if x["is_current"]), None)
    if not e:
        return None
    return datetime.fromisoformat(e["deadline_time"].replace("Z", "+00:00"))


def open_gameweek(bootstrap: dict) -> Optional[dict]:
    events = bootstrap["events"]
    return next((e for e in events if e["is_next"]), None) or \
        next((e for e in events if e["is_current"]), None)
