#!/usr/bin/env python3
"""
FPL Copycat — mirror a target manager's revealed transfers & chips onto your FPL team.

Source of truth for the target manager: fantasyfootballfix.com/reveal/ (Elite XI Team Reveal),
parsed from the server-rendered HTML using your Fix session cookie.

Rules implemented:
  1. If the target manager has a chip ACTIVE and you haven't played it this GW, activate it FIRST
     (wildcard/freehit are attached to the transfer request itself, which is how FPL activates
     them before transfers; bboost/3xc are activated via the my-team endpoint immediately).
  2. Mirror each of the manager's transfers (out X -> in Y) that maps cleanly onto your squad:
     you own X, you don't own Y, you can afford Y, and the 3-per-club rule holds.
  3. Transfers that don't map cleanly are SKIPPED and reported (GitHub issue).
  4. Without an active WC/FH, only as many transfers as you have free transfers are applied;
     the rest are skipped and reported (no automatic -4 hits).
  5. State is kept in state/state.json so the same transfer/skip is not re-processed every run.

Required environment variables:
  FIX_COOKIE   - Cookie header value for fantasyfootballfix.com (e.g. "sessionid=...")
  FPL_REFRESH_TOKEN - OAuth refresh token from the FPL site (localStorage oidc.user entry)
  FPL_COOKIE   - optional fallback: Cookie header for fantasy.premierleague.com
  FPL_ENTRY    - your FPL entry (team) id
  TARGET_MANAGER - display name on the reveal page (default "Tom Dollimore")
  DRY_RUN      - "1" = don't submit anything, just log what would happen

Auth: FPL moved to OAuth (account.premierleague.com). The script exchanges the refresh
token for a short-lived access token and sends it as a Bearer header (no cookies needed).
Rotated tokens are persisted in the repo Actions variable FPL_TOKENS so the automation
keeps itself logged in across runs.
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

FIX_URL = "https://www.fantasyfootballfix.com/reveal/"
FPL = "https://fantasy.premierleague.com"
# Set when a rotated refresh token could not be persisted; the run still does its
# work but exits non-zero so the failure shows red in Actions as well as in an issue.
PERSIST_FAILED = False

PL_TOKEN_URL = "https://account.premierleague.com/as/token"
PL_CLIENT_ID = "bfcbaf69-aade-4c1b-8f00-c1cb8a193030"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")

STATE_PATH = os.path.join(os.path.dirname(__file__), "state", "state.json")

CHIP_MAP = {  # Fix label -> FPL chip API name
    "WC1": "wildcard", "WC2": "wildcard", "WC": "wildcard",
    "FH": "freehit", "BB": "bboost", "TC": "3xc",
}
TRANSFER_CHIPS = {"wildcard", "freehit"}  # activated as part of the transfer payload

report_lines = []

DEBUG = os.environ.get("DEBUG") == "1"
# WC/FH are whole-squad chips. This tool mirrors individual transfers, so attaching
# one to a partial mirror spends a chip worth a full rebuild on a handful of swaps.
# Held back unless explicitly enabled.
ALLOW_TRANSFER_CHIP = os.environ.get("ALLOW_TRANSFER_CHIP") == "1"


def _desc(m):
    """Render a match_player result for the debug log."""
    if m is None:
        return "NO MATCH"
    if m == "ambiguous":
        return "AMBIGUOUS"
    return (f"{m['web_name']}#{m['id']} type={m['element_type']} "
            f"team={m['team']} PS{m['now_cost']/10}")


def log(msg):
    print(msg, flush=True)
    report_lines.append(msg)


def norm_name(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"processed": [], "chips_done": [], "notified": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=1)


# ---------------------------------------------------------------- fix side
def fetch_fix_manager(name):
    r = requests.get(FIX_URL, headers={"User-Agent": UA, "Cookie": os.environ["FIX_COOKIE"]},
                     timeout=60)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    section = None
    for s in soup.select(".reveal-section"):
        if name.lower() in s.get_text(" ").lower():
            section = s
            break
    if section is None:
        raise RuntimeError(
            f"Manager '{name}' not found on the reveal page - Fix session cookie may have "
            "expired, or the manager list changed.")

    chips = {}
    for chip in section.select(".rchip"):
        label = chip.select_one(".rchip__chip")
        status = chip.select_one(".rchip__status")
        if label and status:
            chips[label.get_text(strip=True)] = status.get_text(strip=True).lower()

    gw = None
    rt = section.select_one(".rtransfers")
    if rt:
        m = re.search(r"Gameweek\s+(\d+)", rt.get_text(" ", strip=True))
        if m:
            gw = int(m.group(1))

    transfers = []
    for li in section.select(".rtransfers__transfer"):
        players = li.select(".rtransfers__player")
        out_name = in_name = None
        for p in players:
            status = p.select_one(".rtransfers__status")
            txt = p.get_text(" ", strip=True)
            if not status:
                continue
            stat = status.get_text(strip=True).lower()
            pname = txt.replace(status.get_text(strip=True), "").strip()
            if stat == "out":
                out_name = pname
            elif stat == "in":
                in_name = pname
        if out_name and in_name:
            transfers.append({"out": out_name, "in": in_name})

    updated = None
    m = re.search(r"Last Updated:\s*([^F]+?)(?:FPL|$)", section.get_text(" ", strip=True))
    if m:
        updated = m.group(1).strip()
    return {"chips": chips, "transfers": transfers, "updated": updated, "gw": gw}


def dump_reveal_structure(name, max_lines=250, max_depth=6):
    """Print the tag/class tree of the target's reveal section.

    Structure only - tags, classes, truncated text - because this repo is public and
    its workflow logs are public with it. Enough to write a parser for the full XI
    without republishing the page.
    """
    r = requests.get(FIX_URL, headers={"User-Agent": UA, "Cookie": os.environ["FIX_COOKIE"]},
                     timeout=60)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    section = None
    for sec in soup.select(".reveal-section"):
        if name.lower() in sec.get_text(" ").lower():
            section = sec
            break
    if section is None:
        log(f"[dump] no .reveal-section matched '{name}'")
        return False

    classes = {}
    for el in section.find_all(True):
        for c in el.get("class", []):
            classes[c] = classes.get(c, 0) + 1
    log("[dump] classes in section: "
        + ", ".join(f"{c}x{n}" for c, n in sorted(classes.items(), key=lambda kv: -kv[1])))

    n = [0]

    def walk(el, depth=0):
        if depth > max_depth or n[0] >= max_lines:
            return
        for child in el.find_all(recursive=False):
            if n[0] >= max_lines:
                log("[dump] ... truncated")
                return
            cls = ".".join(child.get("class", []))
            own = " ".join(t.strip() for t in child.find_all(string=True, recursive=False)
                           if t.strip())[:70]
            log(f"[dump] {'  ' * depth}<{child.name}{'.' + cls if cls else ''}>"
                + (f"  {own!r}" if own else ""))
            n[0] += 1
            walk(child, depth + 1)

    walk(section)
    return True


# ---------------------------------------------------------------- fpl auth
def _gh_headers(admin=False):
    """Headers for the GitHub API.

    admin=True is for the Actions *variables* API, which the workflow GITHUB_TOKEN
    cannot write no matter what `permissions:` grants ("Resource not accessible by
    integration"). That needs a PAT with Variables: read/write, supplied as GH_PAT.
    """
    token = (os.environ.get("GH_PAT") if admin else None) or os.environ.get("GITHUB_TOKEN", "")
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json"}


def _gh_var_url(name=""):
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"https://api.github.com/repos/{repo}/actions/variables" + (f"/{name}" if name else "")


PRICE_LEAD_MIN = 150         # start running this many minutes before the price change
DEADLINE_WINDOW_H = 6        # always run this many hours before a GW deadline

try:
    from zoneinfo import ZoneInfo
    UK_TZ = ZoneInfo("Europe/London")
except Exception:                                    # no tzdata on the runner
    UK_TZ = None


def minutes_to_price_change(now=None):
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


def public_next_deadline():
    """Deadline of the open GW, read from the UNAUTHENTICATED bootstrap endpoint.

    Deliberately auth-free: the gate must be able to decide whether to skip without
    spending a refresh-token rotation on a run that will do nothing.
    """
    r = requests.get("https://fantasy.premierleague.com/api/bootstrap-static/",
                     headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    events = r.json()["events"]
    e = next((x for x in events if x["is_next"]), None) or \
        next((x for x in events if x["is_current"]), None)
    if not e:
        return None
    return datetime.fromisoformat(e["deadline_time"].replace("Z", "+00:00"))


def should_run_now():
    """Gate for the hourly cron.

    The schedule fires hourly so deadline-day moves are caught, but real work only
    happens when it matters: the daily pre-price-change slots, or inside the window
    before the open gameweek's deadline. Fails OPEN - if the deadline can't be read
    the run proceeds, because missing a deadline is far worse than a wasted run.
    """
    if os.environ.get("GITHUB_EVENT_NAME", "") != "schedule":
        return True                      # manual dispatch always runs
    now = datetime.now(timezone.utc)
    mins = minutes_to_price_change(now)
    if mins is None:
        if now.hour in (21, 22):     # fallback if Europe/London is unavailable
            log(f"[gate] {now.hour:02d}:00 UTC price slot (no tzdata); running.")
            return True
    elif 0 <= mins <= PRICE_LEAD_MIN:
        log(f"[gate] {mins:.0f}min to the price change; running.")
        return True
    try:
        dl = public_next_deadline()
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


def check_pat():
    """Probe whether GH_PAT can write Actions variables, without spending an FPL token.

    Writes and deletes a throwaway variable. Run via the workflow's check_pat input
    after issuing or renewing the PAT; a green result means token rotation will stick.
    """
    if not os.environ.get("GH_PAT"):
        log("[check-pat] FAIL: GH_PAT is not set in the environment.")
        return False
    probe = "FPL_PAT_PROBE"
    body = {"name": probe, "value": "ok"}
    r = requests.post(_gh_var_url(), headers=_gh_headers(admin=True), json=body, timeout=30)
    if r.status_code == 409:  # already there from an earlier probe
        r = requests.patch(_gh_var_url(probe), headers=_gh_headers(admin=True),
                           json=body, timeout=30)
    if r.status_code >= 400:
        log(f"[check-pat] FAIL: write returned {r.status_code} {r.text[:200]}")
        log("[check-pat] GH_PAT needs 'Variables: read and write' on this repo.")
        return False
    d = requests.delete(_gh_var_url(probe), headers=_gh_headers(admin=True), timeout=30)
    log(f"[check-pat] OK: GH_PAT can write Actions variables (cleanup {d.status_code}).")
    return True


def load_saved_tokens():
    if not os.environ.get("GITHUB_REPOSITORY"):
        return None
    try:
        r = requests.get(_gh_var_url("FPL_TOKENS"), headers=_gh_headers(admin=True), timeout=30)
        if r.status_code == 200:
            return json.loads(r.json()["value"])
    except Exception as e:
        log(f"[tokens] could not load saved tokens: {e}")
    return None


def save_tokens(tokens):
    """Persist the rotated tokens. Returns True on success.

    FPL issues one-time-use refresh tokens: each refresh invalidates the previous
    one. If this write fails the automation is already doomed - the next run will
    present a dead token - so the caller escalates instead of only logging.
    """
    if not os.environ.get("GITHUB_REPOSITORY"):
        return True
    try:
        body = {"name": "FPL_TOKENS", "value": json.dumps(tokens)}
        r = requests.patch(_gh_var_url("FPL_TOKENS"), headers=_gh_headers(admin=True),
                           json=body, timeout=30)
        if r.status_code == 404:
            r = requests.post(_gh_var_url(), headers=_gh_headers(admin=True),
                              json=body, timeout=30)
        if r.status_code >= 400:
            log(f"[tokens] could not persist tokens: {r.status_code} {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log(f"[tokens] could not persist tokens: {e}")
        return False


def refresh_access_token(refresh_token):
    r = requests.post(PL_TOKEN_URL,
                      data={"grant_type": "refresh_token",
                            "refresh_token": refresh_token,
                            "client_id": PL_CLIENT_ID},
                      headers={"User-Agent": UA,
                               "Content-Type": "application/x-www-form-urlencoded"},
                      timeout=60)
    return r


def get_fpl_access_token():
    """Return a valid access token, refreshing and persisting as needed. None if no OAuth set up."""
    tokens = load_saved_tokens()
    now = time.time()
    if tokens and tokens.get("expires_at", 0) - now > 600 and tokens.get("access_token"):
        return tokens["access_token"]
    candidates = []
    if tokens and tokens.get("refresh_token"):
        candidates.append(tokens["refresh_token"])
    seed = os.environ.get("FPL_REFRESH_TOKEN")
    if seed and seed not in candidates:
        candidates.append(seed)
    for rt in candidates:
        r = refresh_access_token(rt)
        if r.status_code < 400:
            j = r.json()
            new = {"access_token": j["access_token"],
                   "refresh_token": j.get("refresh_token", rt),
                   "expires_at": now + int(j.get("expires_in", 3600))}
            rotated = new["refresh_token"] != rt
            if not save_tokens(new) and rotated:
                # The old token is spent and the new one is now only in this process.
                # Finish this run (it still holds a valid access token and may have a
                # deadline to hit), but make the breakage impossible to miss.
                global PERSIST_FAILED
                PERSIST_FAILED = True
                notify("FPL Copycat: token rotation could not be saved",
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


def fpl_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": "https://fantasy.premierleague.com/",
        "Origin": "https://fantasy.premierleague.com",
        "Accept": "application/json",
    })
    token = get_fpl_access_token()
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
        s.headers["X-API-Authorization"] = f"Bearer {token}"
    elif os.environ.get("FPL_COOKIE"):
        s.headers["Cookie"] = os.environ["FPL_COOKIE"]
        log("[auth] no OAuth token available - falling back to FPL_COOKIE.")
    else:
        raise RuntimeError("No FPL auth configured: set FPL_REFRESH_TOKEN (preferred) "
                           "or FPL_COOKIE.")
    return s


def get_bootstrap(s):
    r = s.get(f"{FPL}/api/bootstrap-static/", timeout=60)
    r.raise_for_status()
    return r.json()


def get_my_team(s, entry):
    r = s.get(f"{FPL}/api/my-team/{entry}/", timeout=60)
    if r.status_code in (401, 403):
        raise RuntimeError(f"FPL auth failed ({r.status_code}): {r.text[:200]}")
    r.raise_for_status()
    return r.json()


def get_my_gw_transfers(s, entry, event):
    r = s.get(f"{FPL}/api/entry/{entry}/transfers/", timeout=60)
    if r.status_code != 200:
        return []
    return [t for t in r.json() if t.get("event") == event]


def build_player_index(bootstrap):
    idx = {}
    for el in bootstrap["elements"]:
        for key in {norm_name(el["web_name"]),
                    norm_name(el["first_name"] + el["second_name"]),
                    norm_name(el["second_name"])}:
            idx.setdefault(key, []).append(el)
    return idx


def match_player(idx, name, bootstrap, restrict_ids=None, etype=None):
    """Match a Fix display name to an FPL element.

    Same-name safety: candidates are filtered by squad membership (restrict_ids, used
    for OUT players - the player must be in my squad) and position (etype, used for IN
    players - FPL transfers are always like-for-like by position). If more than one
    candidate survives and the leader isn't clearly the intended one (>=3x the ownership
    of the runner-up), return the string "ambiguous" so the transfer is skipped and
    reported instead of guessing.
    """
    key = norm_name(name)
    cands = idx.get(key, [])
    if not cands:
        # fuzzy: containment either way
        cands = [e for e in bootstrap["elements"]
                 if key and (key in norm_name(e["web_name"]) or norm_name(e["web_name"]) in key)]
    if restrict_ids is not None:
        cands = [e for e in cands if e["id"] in restrict_ids]
    if etype is not None:
        cands = [e for e in cands if e["element_type"] == etype]
    cands = list({e["id"]: e for e in cands}.values())
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    ranked = sorted(cands, key=lambda e: float(e.get("selected_by_percent") or 0), reverse=True)
    top, second = float(ranked[0].get("selected_by_percent") or 0), \
        float(ranked[1].get("selected_by_percent") or 0)
    if top >= 3 * max(second, 0.1):
        return ranked[0]
    return "ambiguous"


def verify_transfers_applied(s, entry, to_apply):
    """True if every intended transfer is already reflected in the live squad.

    FPL's two-phase submit is not a pure validate-then-commit: the confirmed=false
    call can ALREADY apply the transfer, after which the confirmed=true call is a
    duplicate and is rejected with "element in is already picked" / "element out is
    not a current pick" - errors that describe the state the first call created.
    Verified on 2026-08-29 (run 33..., issue #192): the transfer had gone through and
    was still reported as FAILED. So a rejection is only a real failure if the squad
    does NOT show the change. Fails safe: unverifiable means treat as failed.
    """
    try:
        team = get_my_team(s, entry)
    except Exception as e:
        log(f"[verify] could not re-read my team ({e}); treating as NOT applied.")
        return False
    ids = {p["element"] for p in team["picks"]}
    for _, p_out, p_in, _, _, _ in to_apply:
        if p_in["id"] not in ids or p_out["id"] in ids:
            return False
    return True


# ---------------------------------------------------------------- main logic
def main():
    entry = int(os.environ["FPL_ENTRY"])
    target = os.environ.get("TARGET_MANAGER", "Tom Dollimore")
    dry = os.environ.get("DRY_RUN") == "1"

    state = load_state()
    fix = fetch_fix_manager(target)
    log(f"Fix reveal for {target}: chips={fix['chips']}, transfers={fix['transfers']}, "
        f"last updated: {fix['updated']}")

    s = fpl_session()
    bootstrap = get_bootstrap(s)
    events = bootstrap["events"]
    current = next((e for e in events if e["is_next"]), None) or \
              next((e for e in events if e["is_current"]), None)
    if current is None:
        raise RuntimeError("Could not determine the upcoming gameweek.")
    event_id = current["id"]
    deadline = current["deadline_time"]
    if datetime.now(timezone.utc) > datetime.fromisoformat(deadline.replace("Z", "+00:00")):
        log(f"GW{event_id} deadline has passed; nothing to do until next GW opens.")
        finish(state, dry, changed=False)
        return

    if fix["gw"] is not None and fix["gw"] != event_id:
        log(f"Fix reveal shows GW{fix['gw']} transfers but the open FPL gameweek is "
            f"GW{event_id}; waiting for the reveal page to roll over.")
        finish(state, dry, changed=False)
        return

    team = get_my_team(s, entry)
    picks = team["picks"]
    bank = team["transfers"]["bank"]  # tenths of a million
    free_transfers = team["transfers"].get("limit")
    made = team["transfers"].get("made", 0)
    unlimited = free_transfers is None  # pre-GW1 / wildcard-style unlimited window
    my_chips = {c["name"]: c for c in team.get("chips", [])}
    squad_ids = {p["element"] for p in picks}
    elements_by_id = {e["id"]: e for e in bootstrap["elements"]}
    idx = build_player_index(bootstrap)

    if DEBUG:
        log(f"[debug] event_id={event_id} fix_gw={fix['gw']} bank={bank} "
            f"free_transfers={free_transfers} made={made} unlimited={unlimited}")
        log("[debug] live squad: " + ", ".join(
            f"{elements_by_id[p['element']]['web_name']}#{p['element']}"
            f"(sell={p['selling_price']/10})" for p in picks))
        log("[debug] my chips: " + str({k: v.get("status_for_entry")
                                        for k, v in my_chips.items()}))

    changed = False

    # ---------- 1) chips first (VERY IMPORTANT: before any transfers)
    active_chip_fpl = None
    for fix_label, status in fix["chips"].items():
        if status != "active":
            continue
        chip_name = CHIP_MAP.get(fix_label)
        if not chip_name:
            continue
        chip_key = f"gw{event_id}:{chip_name}"
        mine = my_chips.get(chip_name)
        if chip_name in TRANSFER_CHIPS:
            active_chip_fpl = chip_name  # attach to transfer payload below
            if mine and mine.get("status_for_entry") == "active":
                log(f"Chip {chip_name} already active on my team.")
                active_chip_fpl = chip_name
            elif mine and mine.get("status_for_entry") not in ("available",):
                log(f"SKIP chip {chip_name}: my chip status is "
                    f"'{mine.get('status_for_entry')}' (not available).")
                active_chip_fpl = None
        else:
            if chip_key in state["chips_done"]:
                continue
            if mine and mine.get("status_for_entry") == "active":
                log(f"Chip {chip_name} already active on my team.")
                state["chips_done"].append(chip_key)
                continue
            if not mine or mine.get("status_for_entry") != "available":
                log(f"SKIP chip {chip_name}: not available on my team "
                    f"(status={mine.get('status_for_entry') if mine else 'missing'}).")
                continue
            log(f"Activating chip {chip_name} (target manager has {fix_label} active).")
            if not dry:
                r = s.post(f"{FPL}/api/my-team/{entry}/",
                           json={"chip": chip_name,
                                 "picks": [{"element": p["element"], "position": p["position"],
                                            "is_captain": p["is_captain"],
                                            "is_vice_captain": p["is_vice_captain"]}
                                           for p in picks]},
                           timeout=60)
                if r.status_code >= 400:
                    log(f"ERROR activating {chip_name}: {r.status_code} {r.text[:300]}")
                    continue
            state["chips_done"].append(chip_key)
            changed = True

    # ---------- 2) map transfers
    club_counts = {}
    for pid in squad_ids:
        t = elements_by_id[pid]["team"]
        club_counts[t] = club_counts.get(t, 0) + 1
    sell_price = {p["element"]: p["selling_price"] for p in picks}
    my_gw_transfers = get_my_gw_transfers(s, entry, event_id)
    already_in = {t["element_in"] for t in my_gw_transfers}
    if DEBUG:
        log(f"[debug] transfers already made this GW: {my_gw_transfers}")

    to_apply, skipped = [], []
    bank_left = bank
    for tr in fix["transfers"]:
        key = f"gw{event_id}:{tr['out']}->{tr['in']}"
        if key in state["processed"]:
            continue
        # OUT player must be someone I own -> restrict candidates to my squad
        p_out = match_player(idx, tr["out"], bootstrap, restrict_ids=squad_ids)
        if DEBUG:
            log(f"[debug] OUT '{tr['out']}' restricted -> {_desc(p_out)}")
            log(f"[debug] OUT '{tr['out']}' unrestricted -> "
                f"{_desc(match_player(idx, tr['out'], bootstrap))}")
        if p_out == "ambiguous":
            skipped.append((tr, f"two players in my squad match the name '{tr['out']}'"))
            continue
        if not p_out:
            # maybe I don't own them at all - resolve without restriction for the report
            p_out_any = match_player(idx, tr["out"], bootstrap)
            if p_out_any and p_out_any != "ambiguous":
                skipped.append((tr, f"I don't own {p_out_any['web_name']}"))
            else:
                skipped.append((tr, f"could not identify '{tr['out']}' in FPL data"))
            continue
        # IN player must play the same position as the OUT player (FPL rule)
        p_in = match_player(idx, tr["in"], bootstrap, etype=p_out["element_type"])
        if DEBUG:
            log(f"[debug] IN  '{tr['in']}' etype={p_out['element_type']} -> {_desc(p_in)}")
        if p_in == "ambiguous":
            skipped.append((tr, f"multiple FPL players match the name '{tr['in']}' - "
                                "not guessing"))
            continue
        if not p_in:
            skipped.append((tr, f"could not identify '{tr['in']}' in FPL data"))
            continue
        if p_in["id"] in squad_ids or p_in["id"] in already_in:
            log(f"Already mirrored/own {p_in['web_name']}; marking done.")
            state["processed"].append(key)
            continue
        if p_out["id"] not in squad_ids:
            skipped.append((tr, f"I don't own {p_out['web_name']}"))
            continue
        if p_in["status"] == "u":  # unavailable/removed
            skipped.append((tr, f"{p_in['web_name']} unavailable in FPL"))
            continue
        cost = p_in["now_cost"]
        sell = sell_price.get(p_out["id"], p_out["now_cost"])
        if bank_left + sell < cost:
            skipped.append((tr, f"can't afford {p_in['web_name']} "
                                f"(need {cost/10}, have {(bank_left+sell)/10})"))
            continue
        cc = dict(club_counts)
        cc[p_out["team"]] -= 1
        cc[p_in["team"]] = cc.get(p_in["team"], 0) + 1
        if cc[p_in["team"]] > 3:
            skipped.append((tr, f"would exceed 3 players from the same club "
                                f"({p_in['web_name']})"))
            continue
        if p_out["element_type"] != p_in["element_type"]:
            skipped.append((tr, "position mismatch between out/in players"))
            continue
        to_apply.append((tr, p_out, p_in, sell, cost, key))
        bank_left = bank_left + sell - cost
        club_counts = cc
        squad_ids = (squad_ids - {p_out["id"]}) | {p_in["id"]}
        sell_price[p_in["id"]] = cost

    # ---------- 2b) don't spend a whole-squad chip on a partial mirror
    if active_chip_fpl in TRANSFER_CHIPS and not ALLOW_TRANSFER_CHIP:
        held = active_chip_fpl
        active_chip_fpl = None
        log(f"HOLDING chip {held}: it would be attached to only {len(to_apply)} "
            f"transfer(s). {held} is a whole-squad chip and this tool mirrors "
            f"individual transfers, so spending it here wastes it. "
            f"Set ALLOW_TRANSFER_CHIP=1 to override.")
        notify(f"FPL Copycat GW{event_id}: {held} held back, not played",
               f"The target manager has {held} active, and {len(to_apply)} of their "
               f"transfer(s) map onto your squad.\n\n"
               f"{held} gives a full-squad rebuild for one gameweek. Attaching it to a "
               f"partial mirror spends it for almost nothing, so it was NOT played and "
               f"the transfers fell back to the normal free-transfer budget.\n\n"
               f"To play it anyway, re-run with ALLOW_TRANSFER_CHIP=1.")

    # free-transfer budget (no automatic hits) unless WC/FH active or unlimited window
    if not unlimited and active_chip_fpl not in TRANSFER_CHIPS:
        allowed = max((free_transfers or 0) - made, 0)
        if len(to_apply) > allowed:
            for tr, _, _, _, _, _ in to_apply[allowed:]:
                skipped.append((tr, f"would need a -4 hit (only {allowed} free transfer(s) left)"))
            to_apply = to_apply[:allowed]

    # ---------- 3) submit
    if to_apply:
        payload = {
            "confirmed": False,
            "entry": entry,
            "event": event_id,
            "transfers": [{"element_in": p_in["id"], "element_out": p_out["id"],
                           "purchase_price": cost, "selling_price": sell}
                          for _, p_out, p_in, sell, cost, _ in to_apply],
            # chip fields - both historical and current API shapes, extras are ignored
            "wildcard": active_chip_fpl == "wildcard",
            "freehit": active_chip_fpl == "freehit",
            "bench_boost": False,
            "triple_captain": False,
            "chip": active_chip_fpl,
        }
        names = ", ".join(f"{p_out['web_name']} -> {p_in['web_name']}"
                          for _, p_out, p_in, _, _, _ in to_apply)
        log(f"Submitting {len(to_apply)} transfer(s): {names}"
            + (f" with chip {active_chip_fpl}" if active_chip_fpl else ""))
        if DEBUG:
            log(f"[debug] payload transfers={payload['transfers']} "
                f"chip={payload['chip']} freehit={payload['freehit']}")
        if not dry:
            headers = {"Referer": "https://fantasy.premierleague.com/transfers",
                       "Content-Type": "application/json"}
            # phase 1: validation request (confirmed=false); FPL returns errors here
            r = s.post(f"{FPL}/api/transfers/", json=payload, headers=headers, timeout=60)
            body = r.text[:500]
            if r.status_code < 400 and (not body.strip() or "non_form_errors" not in body):
                payload["confirmed"] = True
                r = s.post(f"{FPL}/api/transfers/", json=payload, headers=headers, timeout=60)
            if r.status_code >= 400 or "non_form_errors" in r.text[:500]:
                # Do NOT trust the rejection on its own - the confirm phase is often a
                # duplicate of a transfer the validation phase already applied. Ask the
                # squad what actually happened before crying failure.
                if verify_transfers_applied(s, entry, to_apply):
                    log(f"Confirm phase rejected ({r.status_code}) but the squad already "
                        f"shows these transfers; treating as applied, not failed.")
                else:
                    log(f"ERROR submitting transfers: {r.status_code} {r.text[:500]}")
                    notify(f"FPL Copycat: transfer submission FAILED (GW{event_id})")
                    finish(state, dry, changed)
                    sys.exit(1)
        for _, _, _, _, _, key in to_apply:
            state["processed"].append(key)
        changed = True

    # ---------- 4) report skips (once per skip)
    for tr, reason in skipped:
        # print, not log(): the issue body already lists these via new_skips
        print(f"SKIP {tr['out']} -> {tr['in']}: {reason}", flush=True)
    new_skips = []
    for tr, reason in skipped:
        nkey = f"gw{event_id}:{tr['out']}->{tr['in']}:{reason}"
        if nkey not in state["notified"]:
            state["notified"].append(nkey)
            new_skips.append(f"- {tr['out']} -> {tr['in']}: {reason}")
    if new_skips or to_apply:
        title = f"FPL Copycat GW{event_id}: " + \
                (f"{len(to_apply)} transfer(s) applied" if to_apply else "action needed")
        body = "\n".join(report_lines + ([""] + ["Skipped (need your decision):"] + new_skips
                                         if new_skips else []))
        notify(title, body)

    finish(state, dry, changed or bool(new_skips))


def find_open_issue(repo, token, title):
    """Return the first open issue with exactly this title, or None.

    Paginates the open-issue list (not the fuzzy search API) so a recurring
    failure like an auth outage matches its existing issue exactly.
    """
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    for page in range(1, 4):  # up to 300 open issues
        r = requests.get(f"https://api.github.com/repos/{repo}/issues",
                         headers=headers,
                         params={"state": "open", "per_page": 100, "page": page},
                         timeout=30)
        if r.status_code != 200:
            return None
        items = r.json()
        for it in items:
            if "pull_request" not in it and it.get("title") == title:
                return it
        if len(items) < 100:
            return None
    return None


def notify(title, body=""):
    """Open a GitHub issue in this repo so the owner gets an email/notification.

    Deduped: if an open issue with the identical title already exists (e.g. the
    same failure every 15-min cron run), do nothing instead of piling up copies.
    """
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        log(f"[notify] {title}\n{body}")
        return
    try:
        existing = find_open_issue(repo, token, title)
        if existing:
            log(f"[notify] open issue #{existing.get('number')} already has this "
                f"title; not opening a duplicate.")
            return
        requests.post(f"https://api.github.com/repos/{repo}/issues",
                      headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json"},
                      json={"title": title, "body": body or "\n".join(report_lines)},
                      timeout=30)
    except Exception as e:
        log(f"[notify] failed: {e}")


def finish(state, dry, changed):
    if not dry:
        save_state(state)
    log(f"Done. changed={changed} dry_run={dry}")


if __name__ == "__main__":
    if os.environ.get("CHECK_PAT") == "1":
        raise SystemExit(0 if check_pat() else 1)
    if os.environ.get("DUMP_REVEAL") == "1":
        raise SystemExit(0 if dump_reveal_structure(
            os.environ.get("TARGET_MANAGER", "Tom Dollimore")) else 1)
    if not should_run_now():
        raise SystemExit(0)
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        notify(f"FPL Copycat: run failed - {e}")
        raise
    if PERSIST_FAILED:
        raise SystemExit("token rotation could not be saved - see the opened issue")
