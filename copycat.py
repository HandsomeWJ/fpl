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
  FPL_COOKIE   - Cookie header value for fantasy.premierleague.com (e.g. "pl_profile=...; datadome=...")
  FPL_ENTRY    - your FPL entry (team) id
  TARGET_MANAGER - display name on the reveal page (default "Tom Dollimore")
  DRY_RUN      - "1" = don't submit anything, just log what would happen
"""

import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

FIX_URL = "https://www.fantasyfootballfix.com/reveal/"
FPL = "https://fantasy.premierleague.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")

STATE_PATH = os.path.join(os.path.dirname(__file__), "state", "state.json")

CHIP_MAP = {  # Fix label -> FPL chip API name
    "WC1": "wildcard", "WC2": "wildcard", "WC": "wildcard",
    "FH": "freehit", "BB": "bboost", "TC": "3xc",
}
TRANSFER_CHIPS = {"wildcard", "freehit"}  # activated as part of the transfer payload

report_lines = []


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


# ---------------------------------------------------------------- fpl side
def fpl_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Cookie": os.environ["FPL_COOKIE"],
        "Referer": "https://fantasy.premierleague.com/",
        "Origin": "https://fantasy.premierleague.com",
        "Accept": "application/json",
    })
    return s


def get_bootstrap(s):
    r = s.get(f"{FPL}/api/bootstrap-static/", timeout=60)
    r.raise_for_status()
    return r.json()


def get_my_team(s, entry):
    r = s.get(f"{FPL}/api/my-team/{entry}/", timeout=60)
    if r.status_code in (401, 403):
        raise RuntimeError(f"FPL auth failed ({r.status_code}) - FPL cookie has likely expired.")
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


def match_player(idx, name, bootstrap):
    key = norm_name(name)
    cands = idx.get(key, [])
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        # disambiguate by higher ownership
        return max(cands, key=lambda e: float(e.get("selected_by_percent") or 0))
    # fuzzy: containment either way
    all_matches = [e for e in bootstrap["elements"]
                   if key and (key in norm_name(e["web_name"]) or norm_name(e["web_name"]) in key)]
    if len(all_matches) == 1:
        return all_matches[0]
    if len(all_matches) > 1:
        return max(all_matches, key=lambda e: float(e.get("selected_by_percent") or 0))
    return None


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

    to_apply, skipped = [], []
    bank_left = bank
    for tr in fix["transfers"]:
        key = f"gw{event_id}:{tr['out']}->{tr['in']}"
        if key in state["processed"]:
            continue
        p_out = match_player(idx, tr["out"], bootstrap)
        p_in = match_player(idx, tr["in"], bootstrap)
        if not p_out or not p_in:
            skipped.append((tr, "could not identify player(s) in FPL data"))
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
                log(f"ERROR submitting transfers: {r.status_code} {r.text[:500]}")
                notify(f"FPL Copycat: transfer submission FAILED (GW{event_id})")
                finish(state, dry, changed)
                sys.exit(1)
        for _, _, _, _, _, key in to_apply:
            state["processed"].append(key)
        changed = True

    # ---------- 4) report skips (once per skip)
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


def notify(title, body=""):
    """Open a GitHub issue in this repo so the owner gets an email/notification."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        log(f"[notify] {title}\n{body}")
        return
    try:
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
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        notify(f"FPL Copycat: run failed - {e}")
        raise
