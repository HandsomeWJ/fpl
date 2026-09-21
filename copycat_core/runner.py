"""Orchestration: one run of the copycat, and the CLI entry the workflow calls.

run() is the old main() with its I/O behind Deps, so a test can drive a full dry run
with a fake reveal page and a fake FPL session and assert the exact log lines.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

import dataclasses

from . import execute, fix as fixmod, record, snapshot
from .fpl import (TokenManager, fpl_session, get_bootstrap, get_my_gw_transfers,
                  get_my_team, open_gameweek, public_next_deadline)
from .log import log
from .plan import (CHIP_MAP, TRANSFER_CHIPS, active_downgrades, apply_hit_policy,
                   build_player_index, decide_transfer_chip, net_out_transfer_log,
                   plan_enabling_downgrades, plan_net_transfers, plan_transfers,
                   resolve_ids, resolve_target_squad, squad_converges)
from .ports import (GitHubIssueNotifier, GitHubVariableTokenStore, JsonFileState,
                    LogNotifier, Notifier, NullTokenStore, StateStore, TokenStore)
from .schedule import should_run_now
from .settings import Settings


@dataclass
class Deps:
    state: StateStore
    tokens: TokenStore
    notifier: Notifier
    http: object = requests
    # (token, fpl_cookie) -> session with .get/.post
    session_factory: Callable = fpl_session
    # (target name) -> parsed reveal dict; default fetches with the Fix cookie
    fetch_fix: Optional[Callable[[str], dict]] = None
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    deadline_fn: Callable = public_next_deadline


@dataclass
class RunOutcome:
    changed: bool = False
    persist_failed: bool = False
    ran: bool = True


def default_deps(settings: Settings) -> Deps:
    """Production wiring: git-committed state, Actions variable tokens, GitHub issues."""
    if settings.gh_repo and settings.gh_token:
        notifier: Notifier = GitHubIssueNotifier(settings.gh_repo, settings.gh_token)
    else:
        notifier = LogNotifier()
    tokens: TokenStore = (GitHubVariableTokenStore(settings.gh_repo, settings.gh_pat or
                                                   settings.gh_token or "")
                          if settings.gh_repo else NullTokenStore())
    return Deps(state=JsonFileState(settings.state_path), tokens=tokens, notifier=notifier)


class SubmitFailed(Exception):
    """Transfers were rejected and the squad does not show them."""


def _finish(deps: Deps, state: dict, dry: bool, changed: bool,
            rec: Optional["record.RunRecord"] = None, settings: Optional[Settings] = None) -> None:
    if not dry:
        deps.state.save(state)
    # Records are written before the final "Done." line so that line stays the
    # end-of-run marker the owner reads in Actions.
    if rec is not None and settings is not None and settings.data_dir:
        from .log import report_lines
        record.safe_write_records(rec, settings.data_dir, report_lines)
    log(f"Done. changed={changed} dry_run={dry}")


def run(settings: Settings, deps: Deps) -> RunOutcome:
    if settings.entry is None:
        raise RuntimeError("FPL_ENTRY is not set.")
    entry, target, dry, debug = settings.entry, settings.target, settings.dry, settings.debug
    out = RunOutcome()
    rec = record.RunRecord(ts=deps.now_fn().isoformat(timespec="seconds"),
                           kind="preview" if dry else "run", dry=dry, entry=entry, target=target)

    state = deps.state.load()
    fetch_fix = deps.fetch_fix or (lambda name: fixmod.fetch_fix_manager(
        name, settings.fix_cookie or "", http=deps.http))
    fix = fetch_fix(target)
    log(f"Fix reveal for {target}: chips={fix['chips']}, transfers={fix['transfers']}, "
        f"last updated: {fix['updated']}")
    rec.fix = {k: fix.get(k) for k in ("updated", "gw", "chips", "transfers", "squad",
                                       "starters", "bench", "captain", "vice")}

    tm = TokenManager(deps.tokens, settings.fpl_refresh_seed, deps.notifier, http=deps.http)
    s = deps.session_factory(tm.access_token(), settings.fpl_cookie)
    out.persist_failed = tm.persist_failed

    bootstrap = get_bootstrap(s)
    current = open_gameweek(bootstrap)
    if current is None:
        raise RuntimeError("Could not determine the upcoming gameweek.")
    event_id = current["id"]
    deadline = datetime.fromisoformat(current["deadline_time"].replace("Z", "+00:00"))
    rec.event_id, rec.deadline = event_id, current["deadline_time"]
    if deps.now_fn() > deadline:
        log(f"GW{event_id} deadline has passed; nothing to do until next GW opens.")
        rec.result, rec.early_exit = "early-exit", "deadline passed"
        _finish(deps, state, dry, changed=False, rec=rec, settings=settings)
        return out

    if fix["gw"] is not None and fix["gw"] != event_id:
        log(f"Fix reveal shows GW{fix['gw']} transfers but the open FPL gameweek is "
            f"GW{event_id}; waiting for the reveal page to roll over.")
        rec.result, rec.early_exit = "early-exit", "reveal page not rolled over"
        _finish(deps, state, dry, changed=False, rec=rec, settings=settings)
        return out

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
    rec.team = {"bank": bank, "free_transfers": free_transfers, "made": made,
                "squad": [elements_by_id[p["element"]]["web_name"] for p in picks],
                # per-player prices are private (my-team only); the app's value tracker
                # reads them from the preview record. Tenths of GBPm, like FPL.
                "players": [{"id": p["element"], "name": elements_by_id[p["element"]]["web_name"],
                             "position": p.get("position"),
                             "purchase_price": p.get("purchase_price"),
                             "selling_price": p.get("selling_price"),
                             "now_cost": elements_by_id[p["element"]].get("now_cost")} for p in picks],
                "chips": {k: v.get("status_for_entry") for k, v in my_chips.items()}}

    if debug:
        log(f"[debug] event_id={event_id} fix_gw={fix['gw']} bank={bank} "
            f"free_transfers={free_transfers} made={made} unlimited={unlimited}")
        log("[debug] live squad: " + ", ".join(
            f"{elements_by_id[p['element']]['web_name']}#{p['element']}"
            f"(sell={p['selling_price']/10})" for p in picks))
        log("[debug] my chips: " + str({k: v.get("status_for_entry")
                                        for k, v in my_chips.items()}))

    changed = False

    # ---------- 1) chips first (VERY IMPORTANT: before any transfers)
    active_chip = None
    # my-team chip (3xc/bboost) that must ride along on every later picks POST -
    # a picks save without it silently CANCELS the chip (GW4: TC activated, then the
    # lineup save wiped it).
    team_chip = next((n for n, c in my_chips.items()
                      if n not in TRANSFER_CHIPS and c.get("status_for_entry") == "active"),
                     None)
    for fix_label, status in fix["chips"].items():
        if status != "active":
            continue
        chip_name = CHIP_MAP.get(fix_label)
        if not chip_name:
            continue
        chip_key = f"gw{event_id}:{chip_name}"
        mine = my_chips.get(chip_name)
        if chip_name in TRANSFER_CHIPS:
            active_chip = chip_name  # attach to transfer payload below
            if mine and mine.get("status_for_entry") == "active":
                log(f"Chip {chip_name} already active on my team.")
                active_chip = chip_name
            elif mine and mine.get("status_for_entry") not in ("available",):
                log(f"SKIP chip {chip_name}: my chip status is "
                    f"'{mine.get('status_for_entry')}' (not available).")
                active_chip = None
        else:
            # Live status is authoritative; chips_done is only a record. A chip we
            # "did" can be undone by a later picks POST, so never skip on the record.
            if mine and mine.get("status_for_entry") == "active":
                log(f"Chip {chip_name} already active on my team.")
                team_chip = chip_name
                continue
            if not mine or mine.get("status_for_entry") != "available":
                log(f"SKIP chip {chip_name}: not available on my team "
                    f"(status={mine.get('status_for_entry') if mine else 'missing'}).")
                continue
            log(f"Activating chip {chip_name} (target manager has {fix_label} active).")
            if not dry and not execute.activate_team_chip(s, entry, chip_name, picks):
                continue
            team_chip = chip_name
            rec.chips_activated.append(chip_name)
            if chip_key not in state["chips_done"]:
                state["chips_done"].append(chip_key)
            changed = True

    # ---------- 2) map transfers
    club_counts: dict = {}
    for pid in squad_ids:
        t = elements_by_id[pid]["team"]
        club_counts[t] = club_counts.get(t, 0) + 1
    sell_price = {p["element"]: p["selling_price"] for p in picks}
    my_gw_transfers = get_my_gw_transfers(s, entry, event_id)
    already_in = {t["element_in"] for t in my_gw_transfers}
    if debug:
        log(f"[debug] transfers already made this GW: {my_gw_transfers}")

    # Prefer the net diff to the target squad over replaying the transfer log; the log
    # is chronological and can contain reversals FPL will reject as a batch.
    target_squad = resolve_target_squad(fix, settings.hold_latest)
    target_ids = resolve_ids(target_squad, idx, bootstrap) if len(target_squad) == 15 else None
    # Enabling downgrades still in force (see plan_enabling_downgrades). Under a
    # whole-squad chip they are ignored so the mirror can converge exactly.
    holds = active_downgrades(state, squad_ids, target_ids, elements_by_id)
    rec.held_downgrades = list(state.get("downgrades", []))
    use_holds = holds if (active_chip not in TRANSFER_CHIPS and not unlimited) else None
    if holds and use_holds is None:
        log(f"[downgrade] {len(holds)} hold(s) ignored: whole-squad chip / unlimited transfers")
    net_pairs, net_note = plan_net_transfers(target_squad, squad_ids, idx, bootstrap,
                                             elements_by_id, held=use_holds)
    if net_pairs is None:
        pending = net_out_transfer_log(fix["transfers"])
        log(f"[plan] falling back to the transfer log ({net_note}); "
            f"netted {len(fix['transfers'])} log entries to {len(pending)} transfer(s)")
    else:
        log(f"[plan] {net_note}")
        if debug:
            for pr in net_pairs:
                log(f"[plan]   {pr['out']} -> {pr['in']}")
        pending = list(net_pairs)

    plan = plan_transfers(pending, state=state, event_id=event_id, idx=idx,
                          bootstrap=bootstrap, elements_by_id=elements_by_id,
                          squad_ids=squad_ids, sell_price=sell_price, bank=bank,
                          club_counts=club_counts, already_in=already_in, debug=debug)
    new_downgrades: list = []
    if settings.allow_downgrade and net_pairs is not None:
        new_downgrades = plan_enabling_downgrades(
            plan, pending=pending, fix=fix, target_ids=target_ids, event_id=event_id,
            idx=idx, bootstrap=bootstrap, elements_by_id=elements_by_id, sell_price=sell_price)
    to_apply, skipped = plan.to_apply, plan.skipped
    rec.plan_note, rec.pairs = net_note, list(pending) if net_pairs is not None else list(pending)

    # ---------- 2b) play a whole-squad chip only if it actually buys the squad
    wanted_chip = active_chip
    active_chip = decide_transfer_chip(active_chip, target_squad, plan.projected_ids, idx,
                                       bootstrap, elements_by_id,
                                       allow_override=settings.allow_transfer_chip,
                                       event_id=event_id, notifier=deps.notifier)
    rec.transfer_chip = active_chip
    if wanted_chip in TRANSFER_CHIPS and active_chip is None:
        rec.chip_held = {"chip": wanted_chip,
                         "why": squad_converges(target_squad, plan.projected_ids, idx,
                                                bootstrap, elements_by_id)[1]}

    to_apply = apply_hit_policy(to_apply, skipped, unlimited=unlimited,
                                active_chip=active_chip, free_transfers=free_transfers,
                                made=made, allow_hits=settings.allow_hits)

    rec.to_apply = [{"out": p_out["web_name"], "in": p_in["web_name"], "out_id": p_out["id"],
                     "in_id": p_in["id"], "sell": sell, "cost": cost,
                     "role": tr.get("role", "mirror"), "enables": tr.get("enables")}
                    for tr, p_out, p_in, sell, cost, _ in to_apply]
    if not unlimited and active_chip not in TRANSFER_CHIPS:
        extra = max(len(to_apply) - max((free_transfers or 0) - made, 0), 0)
        rec.hits = {"count": extra, "points": -4 * extra}

    # ---------- 3) submit
    if to_apply:
        ok = execute.submit_transfers(s, entry, event_id, to_apply, active_chip,
                                      dry=dry, debug=debug)
        if not ok:
            rec.result = "failed"
            deps.notifier.notify(f"FPL Copycat: transfer submission FAILED (GW{event_id})")
            _finish(deps, state, dry, changed, rec=rec, settings=settings)
            raise SubmitFailed()
        applied_keys = {key for *_, key in to_apply}
        for _, _, _, _, _, key in to_apply:
            state["processed"].append(key)
        for d in new_downgrades:
            if f"gw{event_id}:{d['held_name']}->{d['have_name']}" in applied_keys:
                state.setdefault("downgrades", []).append(d)
        changed = True
        rec.result = "dry" if dry else "applied"

    # ---------- 3b) match their XI, bench order and armbands
    # Re-read the squad: transfers just changed it, and the lineup must be built from
    # what we actually own now. Gated on convergence - reordering a squad that is not
    # theirs would reference players we do not have.
    if not skipped or to_apply:
        try:
            fresh = get_my_team(s, entry)["picks"]
        except Exception as e:
            fresh = picks
            log(f"[lineup] could not re-read squad ({e}); using the pre-transfer picks")
        owned_now = {p["element"] for p in fresh}
        subs = {d["held"]: d["have"] for d in state.get("downgrades", [])
                if d["have"] in owned_now and d["held"] not in owned_now}
        lineup_changed, lineup_note = execute.sync_lineup(
            s, entry, fresh, fix, idx, bootstrap, elements_by_id, dry, chip=team_chip,
            substitutions=subs)
        log(f"[lineup] {lineup_note}")
        rec.lineup = lineup_note
        changed = changed or lineup_changed

    # ---------- 4) report skips (once per skip)
    rec.skipped = [{"out": tr["out"], "in": tr["in"], "reason": reason} for tr, reason in skipped]
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
        from .log import report_lines
        title = f"FPL Copycat GW{event_id}: " + \
                (f"{len(to_apply)} transfer(s) applied" if to_apply else "action needed")
        body = "\n".join(report_lines + ([""] + ["Skipped (need your decision):"] + new_skips
                                         if new_skips else []))
        if dry:
            # A dry run submitted nothing, so an issue saying "applied" would be a lie.
            log(f"[dry-run] would have opened an issue: {title!r} (not opening)")
        else:
            deps.notifier.notify(title, body)

    out.changed = changed or bool(new_skips)
    if rec.result == "nothing" and dry:
        rec.result = "dry"
    _finish(deps, state, dry, out.changed, rec=rec, settings=settings)
    return out


def cli(env=os.environ) -> int:
    """Entry point used by copycat.py and the workflow. Returns the exit code."""
    settings = Settings.from_env(env)
    deps = default_deps(settings)

    if settings.check_pat:
        if not settings.gh_pat:
            log("[check-pat] FAIL: GH_PAT is not set in the environment.")
            return 1
        return 0 if GitHubVariableTokenStore(settings.gh_repo or "", settings.gh_pat).probe() else 1
    if settings.dump_reveal:
        return 0 if fixmod.dump_reveal_structure(settings.target, settings.fix_cookie or "") else 1
    # Price snapshot first: public endpoint, no FPL auth, and it must happen on the
    # clock ticks that the mirror gate is about to skip.
    snapshot.safe_take_snapshot(deps.now_fn(), settings.data_dir, http=deps.http,
                                force=settings.snapshot_force)
    if not should_run_now(settings, deadline_fn=deps.deadline_fn):
        # Once an hour (the first tick after :00), compute the plan as a DRY run so the
        # app's "next run will..." preview is never more than an hour stale. Costs one
        # reveal-page fetch and one FPL read per hour; nothing is submitted.
        if settings.preview_hourly and deps.now_fn().minute < 15:
            log("[preview] hourly dry run for the app's preview")
            try:
                run(dataclasses.replace(settings, dry=True), deps)
            except Exception as e:
                log(f"[preview] failed: {e}")
        return 0
    try:
        outcome = run(settings, deps)
    except SubmitFailed:
        return 1
    except Exception as e:
        log(f"FATAL: {e}")
        deps.notifier.notify(f"FPL Copycat: run failed - {e}")
        raise
    if outcome.persist_failed:
        print("token rotation could not be saved - see the opened issue", file=sys.stderr)
        return 1
    return 0
