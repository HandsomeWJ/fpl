"""Deciding what to do: name matching, the net diff, affordability, chips, hits.

Everything here is pure - no HTTP, no clock - so it is fully testable. The only
side effect is that plan_transfers() marks already-mirrored transfers in `state`,
exactly as the original loop did.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from .log import log
from .ports import Notifier

CHIP_MAP = {  # Fix label -> FPL chip API name
    "WC1": "wildcard", "WC2": "wildcard", "WC": "wildcard",
    "FH": "freehit", "BB": "bboost", "TC": "3xc",
}
TRANSFER_CHIPS = {"wildcard", "freehit"}  # activated as part of the transfer payload


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _desc(m) -> str:
    """Render a match_player result for the debug log."""
    if m is None:
        return "NO MATCH"
    if m == "ambiguous":
        return "AMBIGUOUS"
    return (f"{m['web_name']}#{m['id']} type={m['element_type']} "
            f"team={m['team']} PS{m['now_cost']/10}")


def build_player_index(bootstrap: dict) -> dict:
    idx: dict = {}
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


def net_out_transfer_log(transfers: list) -> list:
    """Collapse a chronological transfer log into its NET effect.

    Only used when the reveal squad cannot be parsed and we must fall back to the log.
    The log is a sequence, but an FPL transfers batch is not: every entry is validated
    against the CURRENT squad, so a log containing A->B and later B->A rejects the
    whole payload. Cancelling round trips first means the fallback submits what the
    manager actually ended up doing, not the path they took to get there.
    """
    outs, ins = [], []
    for tr in transfers:
        o, i = tr["out"], tr["in"]
        if o in ins:
            ins.remove(o)          # bought earlier in the log, now sold again
        else:
            outs.append(o)
        if i in outs:
            outs.remove(i)         # sold earlier in the log, now bought back
        else:
            ins.append(i)
    return [{"out": o, "in": i} for o, i in zip(outs, ins)]


def resolve_target_squad(fix: dict, hold_latest: int) -> list:
    """The squad to aim for: the target's 15, optionally rewound by their N most
    recent logged transfers so the newest move can be tested on its own."""
    target_squad = list(fix.get("squad") or [])
    if hold_latest and fix.get("transfers"):
        for tr in fix["transfers"][-hold_latest:]:
            if tr["in"] in target_squad:
                target_squad[target_squad.index(tr["in"])] = tr["out"]
                log(f"[plan] holding back their {tr['out']} -> {tr['in']}; "
                    f"targeting {tr['out']} instead")
    return target_squad


def plan_net_transfers(fix_squad, squad_ids, idx, bootstrap, elements_by_id):
    """Net diff from my squad to the target manager's, as out/in name pairs.

    The reveal page lists a CHRONOLOGICAL transfer log for the gameweek, not a net
    change. Under a Free Hit especially it contains reversals (A->B then B->A) and the
    same player leaving twice. Replaying that log produces a batch FPL rejects
    outright, because it validates every entry against the CURRENT squad rather than
    applying them in sequence - so A->B and B->A in one payload contradict each other.

    Diffing squads sidesteps all of it: whatever the route, the destination is the
    same. Pairs are matched within position, which is always possible because both
    sides are valid FPL squads and therefore have identical positional shape.
    Returns (pairs, note); empty pairs means nothing to do or nothing usable.
    """
    if not fix_squad or len(fix_squad) != 15:
        return None, f"reveal squad unusable ({len(fix_squad or [])} players)"
    target = set()
    for nm in fix_squad:
        m = match_player(idx, nm, bootstrap)
        if not m or m == "ambiguous":
            return None, f"could not resolve '{nm}' to an FPL player"
        target.add(m["id"])
    if len(target) != 15:
        return None, f"reveal squad resolved to {len(target)} distinct players"

    sell = sorted(squad_ids - target)
    buy = sorted(target - squad_ids)
    if not sell and not buy:
        return [], "already matching"
    by_pos_out, by_pos_in = {}, {}
    for i in sell:
        by_pos_out.setdefault(elements_by_id[i]["element_type"], []).append(i)
    for i in buy:
        by_pos_in.setdefault(elements_by_id[i]["element_type"], []).append(i)
    if sorted((k, len(v)) for k, v in by_pos_out.items()) != \
       sorted((k, len(v)) for k, v in by_pos_in.items()):
        shape_out = {k: len(v) for k, v in by_pos_out.items()}
        shape_in = {k: len(v) for k, v in by_pos_in.items()}
        return None, f"positional shape differs - out {shape_out} vs in {shape_in}"
    pairs = []
    for pos, outs in by_pos_out.items():
        for o, i in zip(outs, by_pos_in[pos]):
            pairs.append({"out": elements_by_id[o]["web_name"],
                          "in": elements_by_id[i]["web_name"]})
    return pairs, f"{len(pairs)} net transfer(s) to match the target squad"


def squad_converges(fix_squad, projected_ids, idx, bootstrap, elements_by_id):
    """Would the projected squad be exactly the target manager's 15?

    This is the test for whether a whole-squad chip (WC/FH) is worth playing: spend it
    only when it actually buys you their squad, never on a partial mirror. Returns
    (converged, explanation). Fails CLOSED - anything unparseable or unresolvable
    means "not converged", so an unreadable page holds the chip rather than gambling it.
    """
    if not fix_squad:
        return False, "the reveal page gave no squad to compare against"
    if len(fix_squad) != 15:
        return False, f"parsed {len(fix_squad)} players from the reveal page, expected 15"
    target, unresolved = set(), []
    for nm in fix_squad:
        m = match_player(idx, nm, bootstrap)
        if m and m != "ambiguous":
            target.add(m["id"])
        else:
            unresolved.append(nm)
    if unresolved:
        return False, f"could not resolve {unresolved} to FPL players"
    if len(target) != 15:
        return False, f"resolved to {len(target)} distinct players, expected 15"
    if target == projected_ids:
        return True, "projected squad matches the target manager exactly"
    missing = [elements_by_id[i]["web_name"] for i in sorted(target - projected_ids)]
    extra = [elements_by_id[i]["web_name"] for i in sorted(projected_ids - target)]
    return False, f"would still differ - missing {missing}, holding {extra}"


@dataclass
class PlanResult:
    to_apply: list = field(default_factory=list)   # (tr, p_out, p_in, sell, cost, key)
    skipped: list = field(default_factory=list)    # (tr, reason)
    projected_ids: set = field(default_factory=set)
    bank_left: int = 0


def plan_transfers(pending, *, state, event_id, idx, bootstrap, elements_by_id,
                   squad_ids, sell_price, bank, club_counts, already_in,
                   debug=False) -> PlanResult:
    """Turn out/in name pairs into concrete, affordable, legal transfers.

    Affordability is order-dependent: each swap is funded by its own sale plus the
    bank, so a cash-releasing transfer later in the list can pay for tight ones
    earlier in it. Evaluating once, in list order, discarded transfers the squad could
    actually afford (GW3: 1 of 3 applied when all 3 fitted with 1.2 to spare). So
    affordability failures are deferred and retried until a pass applies nothing;
    whatever is still deferred then is genuinely unaffordable.
    """
    res = PlanResult(projected_ids=set(squad_ids), bank_left=bank)
    squad_ids = set(squad_ids)
    sell_price = dict(sell_price)
    club_counts = dict(club_counts)
    pending = list(pending)
    while pending:
        deferred = []
        applied_this_pass = False
        for tr in pending:
            key = f"gw{event_id}:{tr['out']}->{tr['in']}"
            if key in state["processed"]:
                continue
            # OUT player must be someone I own -> restrict candidates to my squad
            p_out = match_player(idx, tr["out"], bootstrap, restrict_ids=squad_ids)
            if debug:
                log(f"[debug] OUT '{tr['out']}' restricted -> {_desc(p_out)}")
                log(f"[debug] OUT '{tr['out']}' unrestricted -> "
                    f"{_desc(match_player(idx, tr['out'], bootstrap))}")
            if p_out == "ambiguous":
                res.skipped.append((tr, f"two players in my squad match the name '{tr['out']}'"))
                continue
            if not p_out:
                # maybe I don't own them at all - resolve without restriction for the report
                p_out_any = match_player(idx, tr["out"], bootstrap)
                if p_out_any and p_out_any != "ambiguous":
                    res.skipped.append((tr, f"I don't own {p_out_any['web_name']}"))
                else:
                    res.skipped.append((tr, f"could not identify '{tr['out']}' in FPL data"))
                continue
            # IN player must play the same position as the OUT player (FPL rule)
            p_in = match_player(idx, tr["in"], bootstrap, etype=p_out["element_type"])
            if debug:
                log(f"[debug] IN  '{tr['in']}' etype={p_out['element_type']} -> {_desc(p_in)}")
            if p_in == "ambiguous":
                res.skipped.append((tr, f"multiple FPL players match the name '{tr['in']}' - "
                                        "not guessing"))
                continue
            if not p_in:
                res.skipped.append((tr, f"could not identify '{tr['in']}' in FPL data"))
                continue
            if p_in["id"] in squad_ids or p_in["id"] in already_in:
                log(f"Already mirrored/own {p_in['web_name']}; marking done.")
                state["processed"].append(key)
                continue
            if p_out["id"] not in squad_ids:
                res.skipped.append((tr, f"I don't own {p_out['web_name']}"))
                continue
            if p_in["status"] == "u":  # unavailable/removed
                res.skipped.append((tr, f"{p_in['web_name']} unavailable in FPL"))
                continue
            cost = p_in["now_cost"]
            sell = sell_price.get(p_out["id"], p_out["now_cost"])
            if res.bank_left + sell < cost:
                deferred.append(tr)   # a later sale may fund this; retried next pass
                continue
            cc = dict(club_counts)
            cc[p_out["team"]] -= 1
            cc[p_in["team"]] = cc.get(p_in["team"], 0) + 1
            if cc[p_in["team"]] > 3:
                res.skipped.append((tr, f"would exceed 3 players from the same club "
                                        f"({p_in['web_name']})"))
                continue
            if p_out["element_type"] != p_in["element_type"]:
                res.skipped.append((tr, "position mismatch between out/in players"))
                continue
            res.to_apply.append((tr, p_out, p_in, sell, cost, key))
            applied_this_pass = True
            res.bank_left = res.bank_left + sell - cost
            club_counts = cc
            squad_ids = (squad_ids - {p_out["id"]}) | {p_in["id"]}
            sell_price[p_in["id"]] = cost

        if not applied_this_pass:
            # No pass can free up more money, so the rest are truly unaffordable.
            for tr in deferred:
                res.skipped.append((tr, "can't afford it even after the other "
                                        "transfers free up money"))
            break
        pending = deferred
    res.projected_ids = squad_ids
    return res


def decide_transfer_chip(active_chip: Optional[str], target_squad, projected_ids, idx,
                         bootstrap, elements_by_id, *, allow_override: bool, event_id: int,
                         notifier: Notifier) -> Optional[str]:
    """Play a whole-squad chip (WC/FH) only if it actually buys the target's squad.
    Returns the chip to attach to the transfer payload, or None if held back."""
    if active_chip not in TRANSFER_CHIPS:
        return active_chip
    held = active_chip
    converged, why = squad_converges(target_squad, projected_ids, idx, bootstrap, elements_by_id)
    if converged:
        log(f"Playing chip {held}: {why}.")
        return held
    if allow_override:
        log(f"Playing chip {held} by ALLOW_TRANSFER_CHIP override, despite: {why}.")
        return held
    log(f"HOLDING chip {held}: {why}. A whole-squad chip is only worth playing "
        f"when the mirror lands on the target manager's exact squad. "
        f"Set ALLOW_TRANSFER_CHIP=1 to override.")
    notifier.notify(f"FPL Copycat GW{event_id}: {held} held back, not played",
                    f"The target manager has {held} active, but playing it would not "
                    f"reproduce their squad: {why}.\n\n"
                    f"{held} gives a full-squad rebuild for one gameweek, so it was NOT "
                    f"played and the transfers fell back to the normal free-transfer "
                    f"budget.\n\nTo play it anyway, re-run with ALLOW_TRANSFER_CHIP=1.")
    return None


def apply_hit_policy(to_apply: list, skipped: list, *, unlimited: bool,
                     active_chip: Optional[str], free_transfers, made: int,
                     allow_hits: bool) -> list:
    """Free-transfer budget unless WC/FH is active or the window is unlimited.
    With allow_hits the extra transfers go through at -4 each; otherwise they are
    skipped and reported."""
    if unlimited or active_chip in TRANSFER_CHIPS:
        return to_apply
    allowed = max((free_transfers or 0) - made, 0)
    if len(to_apply) <= allowed:
        return to_apply
    if allow_hits:
        extra = len(to_apply) - allowed
        log(f"TAKING HITS: {len(to_apply)} transfer(s) with {allowed} free -> "
            f"{extra} x -4 = -{extra * 4} points (ALLOW_HITS=1).")
        return to_apply
    for tr, _, _, _, _, _ in to_apply[allowed:]:
        skipped.append((tr, f"would need a -4 hit "
                            f"(only {allowed} free transfer(s) left)"))
    return to_apply[:allowed]
