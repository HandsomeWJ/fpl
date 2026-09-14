"""Writes to FPL: chip activation, the two-phase transfer submit, lineup sync."""

from __future__ import annotations

from typing import Optional

from .fpl import FPL, get_my_team
from .log import log
from .plan import match_player


def activate_team_chip(s, entry: int, chip_name: str, picks: list) -> bool:
    """Activate a my-team chip (3xc / bboost) via the my-team endpoint."""
    r = s.post(f"{FPL}/api/my-team/{entry}/",
               json={"chip": chip_name,
                     "picks": [{"element": p["element"], "position": p["position"],
                                "is_captain": p["is_captain"],
                                "is_vice_captain": p["is_vice_captain"]}
                               for p in picks]},
               timeout=60)
    if r.status_code >= 400:
        log(f"ERROR activating {chip_name}: {r.status_code} {r.text[:300]}")
        return False
    return True


def verify_transfers_applied(s, entry: int, to_apply: list, get_team=get_my_team) -> bool:
    """True if every intended transfer is already reflected in the live squad.

    FPL's two-phase submit is not a pure validate-then-commit: the confirmed=false
    call can ALREADY apply the transfer, after which the confirmed=true call is a
    duplicate and is rejected with "element in is already picked" / "element out is
    not a current pick" - errors that describe the state the first call created.
    Verified on 2026-08-29 (issue #192): the transfer had gone through and was still
    reported as FAILED. So a rejection is only a real failure if the squad does NOT
    show the change. Fails safe: unverifiable means treat as failed.
    """
    try:
        team = get_team(s, entry)
    except Exception as e:
        log(f"[verify] could not re-read my team ({e}); treating as NOT applied.")
        return False
    ids = {p["element"] for p in team["picks"]}
    for _, p_out, p_in, _, _, _ in to_apply:
        if p_in["id"] not in ids or p_out["id"] in ids:
            return False
    return True


def build_transfer_payload(entry: int, event_id: int, to_apply: list,
                           active_chip: Optional[str]) -> dict:
    return {
        "confirmed": False,
        "entry": entry,
        "event": event_id,
        "transfers": [{"element_in": p_in["id"], "element_out": p_out["id"],
                       "purchase_price": cost, "selling_price": sell}
                      for _, p_out, p_in, sell, cost, _ in to_apply],
        # chip fields - both historical and current API shapes, extras are ignored
        "wildcard": active_chip == "wildcard",
        "freehit": active_chip == "freehit",
        "bench_boost": False,
        "triple_captain": False,
        "chip": active_chip,
    }


def submit_transfers(s, entry: int, event_id: int, to_apply: list,
                     active_chip: Optional[str], *, dry: bool, debug: bool = False,
                     verify=verify_transfers_applied) -> bool:
    """Two-phase submit. Returns True when the transfers are applied (or this is a dry
    run), False when they genuinely failed - after logging the error."""
    payload = build_transfer_payload(entry, event_id, to_apply, active_chip)
    names = ", ".join(f"{p_out['web_name']} -> {p_in['web_name']}"
                      for _, p_out, p_in, _, _, _ in to_apply)
    log(f"Submitting {len(to_apply)} transfer(s): {names}"
        + (f" with chip {active_chip}" if active_chip else ""))
    if debug:
        log(f"[debug] payload transfers={payload['transfers']} "
            f"chip={payload['chip']} freehit={payload['freehit']}")
    if dry:
        return True
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
        if verify(s, entry, to_apply):
            log(f"Confirm phase rejected ({r.status_code}) but the squad already "
                f"shows these transfers; treating as applied, not failed.")
            return True
        log(f"ERROR submitting transfers: {r.status_code} {r.text[:500]}")
        return False
    return True


def sync_lineup(s_sess, entry, picks, fix, idx, bootstrap, elements_by_id, dry,
                chip: Optional[str] = None):
    """Match the target manager's starting XI, bench order and armbands.

    Only safe once the squad already matches theirs, so the caller gates on
    convergence - otherwise the lineup would reference players we do not own.

    FPL positions: 1-11 are the starting XI with the keeper at 1; 12-15 are the bench
    with the reserve keeper at 12. The reveal page's DOM order is already exactly
    that (pitch rows run GK->DEF->MID->FWD, then the bench), so document order maps
    straight onto position numbers.

    Returns (changed, note).
    """
    starters, bench = fix.get("starters") or [], fix.get("bench") or []
    if len(starters) != 11 or len(bench) != 4:
        return False, f"lineup unusable ({len(starters)} starters, {len(bench)} bench)"
    if not fix.get("captain"):
        return False, "no captain found on the reveal page"

    order, seen = [], set()
    for nm in starters + bench:
        m = match_player(idx, nm, bootstrap)
        if not m or m == "ambiguous":
            return False, f"could not resolve '{nm}'"
        if m["id"] in seen:
            return False, f"'{nm}' resolved to a duplicate player"
        seen.add(m["id"])
        order.append(m["id"])

    owned = {p["element"] for p in picks}
    if seen != owned:
        return False, "lineup names do not match the squad we own"

    cap = match_player(idx, fix["captain"], bootstrap)
    vc = match_player(idx, fix["vice"], bootstrap) if fix.get("vice") else None
    cap_id = cap["id"] if cap and cap != "ambiguous" else None
    vc_id = vc["id"] if vc and vc != "ambiguous" else None
    if cap_id is None:
        return False, f"could not resolve captain '{fix['captain']}'"
    if cap_id not in order[:11]:
        return False, "captain is not in the starting XI"

    new_picks = [{"element": pid, "position": i + 1,
                  "is_captain": pid == cap_id, "is_vice_captain": pid == vc_id}
                 for i, pid in enumerate(order)]
    current = {p["element"]: (p["position"], p["is_captain"], p["is_vice_captain"])
               for p in picks}
    wanted = {p["element"]: (p["position"], p["is_captain"], p["is_vice_captain"])
              for p in new_picks}
    if current == wanted:
        return False, "lineup already matches"

    cname = elements_by_id[cap_id]["web_name"]
    vname = elements_by_id[vc_id]["web_name"] if vc_id else "none"
    note = (f"XI/bench reordered, captain {cname}, vice {vname}")
    if dry:
        return False, f"[dry-run] would set {note}"
    # The active my-team chip MUST be re-sent: a picks POST without it cancels the
    # chip (GW4: TC activated, then the lineup save wiped it). Transfer chips (FH/WC)
    # live on the transfers endpoint and are unaffected, so the key is omitted rather
    # than sent as null when there is none.
    payload = {"picks": new_picks}
    if chip:
        payload["chip"] = chip
    r = s_sess.post(f"{FPL}/api/my-team/{entry}/", json=payload, timeout=60)
    if r.status_code >= 400:
        return False, f"FAILED {r.status_code} {r.text[:200]}"
    return True, note
