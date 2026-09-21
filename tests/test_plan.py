from copycat_core import log as logmod
from copycat_core.plan import (apply_hit_policy, build_player_index, decide_transfer_chip,
                               net_out_transfer_log, plan_net_transfers, plan_transfers,
                               resolve_target_squad, squad_converges)
from copycat_core.ports import ListNotifier
from tests.conftest import BY_NAME, OUR_SQUAD, TOM_SQUAD, ids, mk_bootstrap, mk_picks


def _ctx(bootstrap):
    return build_player_index(bootstrap), {e["id"]: e for e in bootstrap["elements"]}


def test_net_diff_ignores_the_route():
    bs = mk_bootstrap(); idx, by_id = _ctx(bs)
    pairs, note = plan_net_transfers(TOM_SQUAD, set(ids(OUR_SQUAD)), idx, bs, by_id)
    assert note == "3 net transfer(s) to match the target squad"
    assert {(p["out"], p["in"]) for p in pairs} == {
        ("Calvert-Lewin", "Wissa"), ("Maguire", "Gvardiol"), ("Mbeumo", "Elanga")}


def test_net_diff_already_matching_and_unusable():
    bs = mk_bootstrap(); idx, by_id = _ctx(bs)
    assert plan_net_transfers(TOM_SQUAD, set(ids(TOM_SQUAD)), idx, bs, by_id) == ([], "already matching")
    pairs, note = plan_net_transfers(TOM_SQUAD[:14], set(ids(OUR_SQUAD)), idx, bs, by_id)
    assert pairs is None and "unusable" in note
    pairs, note = plan_net_transfers(TOM_SQUAD[:14] + ["Nobody"], set(ids(OUR_SQUAD)), idx, bs, by_id)
    assert pairs is None and "Nobody" in note


def test_transfer_log_is_netted_not_replayed():
    log = [{"out": "B.Fernandes", "in": "Enzo"}, {"out": "Enzo", "in": "B.Fernandes"},
           {"out": "B.Fernandes", "in": "Cherki"}]
    assert net_out_transfer_log(log) == [{"out": "B.Fernandes", "in": "Cherki"}]
    assert net_out_transfer_log([{"out": "A", "in": "B"}, {"out": "B", "in": "A"}]) == []
    assert net_out_transfer_log([{"out": "A", "in": "B"}, {"out": "B", "in": "C"}]) == \
        [{"out": "A", "in": "C"}]


def test_affordability_retries_after_sales_free_cash():
    """GW3 numbers: bank 0; CL 6.0->Wissa 6.1 and Maguire 5.0->Gvardiol 5.6 only fit
    once Mbeumo 8.0->Elanga 6.1 has released cash. One pass applied 1 of 3."""
    bs = mk_bootstrap(); idx, by_id = _ctx(bs)
    state = {"processed": [], "chips_done": [], "notified": []}
    picks = mk_picks(OUR_SQUAD)
    squad = set(ids(OUR_SQUAD))
    club = {}
    for pid in squad:
        club[by_id[pid]["team"]] = club.get(by_id[pid]["team"], 0) + 1
    pending = [{"out": "Calvert-Lewin", "in": "Wissa"}, {"out": "Maguire", "in": "Gvardiol"},
               {"out": "Mbeumo", "in": "Elanga"}]
    res = plan_transfers(pending, state=state, event_id=3, idx=idx, bootstrap=bs,
                         elements_by_id=by_id, squad_ids=squad,
                         sell_price={p["element"]: p["selling_price"] for p in picks},
                         bank=0, club_counts=club, already_in=set())
    assert len(res.to_apply) == 3 and res.skipped == []
    assert res.to_apply[0][1]["web_name"] == "Mbeumo"      # the cash-releasing one first
    assert res.bank_left == 12                              # 19.0 in, 17.8 out
    assert res.projected_ids == set(ids(TOM_SQUAD))


def test_plan_marks_already_owned_and_reports_unowned():
    bs = mk_bootstrap(); idx, by_id = _ctx(bs)
    state = {"processed": [], "chips_done": [], "notified": []}
    squad = set(ids(TOM_SQUAD))
    res = plan_transfers([{"out": "Maguire", "in": "Gvardiol"},      # we don't own Maguire
                          {"out": "Raya", "in": "Scherpen"}],        # already own Scherpen
                         state=state, event_id=3, idx=idx, bootstrap=bs, elements_by_id=by_id,
                         squad_ids=squad, sell_price={}, bank=0, club_counts={}, already_in=set())
    assert res.to_apply == []
    assert res.skipped == [({"out": "Maguire", "in": "Gvardiol"}, "I don't own Maguire")]
    assert "gw3:Raya->Scherpen" in state["processed"]
    assert "Already mirrored/own Scherpen; marking done." in logmod.report_lines


def test_squad_converges_fails_closed():
    bs = mk_bootstrap(); idx, by_id = _ctx(bs)
    tom = set(ids(TOM_SQUAD))
    assert squad_converges(TOM_SQUAD, tom, idx, bs, by_id)[0] is True
    ok, why = squad_converges(TOM_SQUAD, set(ids(OUR_SQUAD)), idx, bs, by_id)
    assert ok is False and "missing" in why and "holding" in why
    assert squad_converges([], tom, idx, bs, by_id)[0] is False
    assert squad_converges(TOM_SQUAD[:14], tom, idx, bs, by_id)[0] is False
    assert squad_converges(TOM_SQUAD[:14] + ["Nobody"], tom, idx, bs, by_id)[0] is False


def test_hold_latest_rewinds_the_targets_newest_transfer():
    fix = {"squad": TOM_SQUAD, "transfers": [{"out": "B.Fernandes", "in": "Elanga"}]}
    target = resolve_target_squad(fix, hold_latest=1)
    assert "Elanga" not in target and "B.Fernandes" in target and len(target) == 15
    assert resolve_target_squad(fix, hold_latest=0) == TOM_SQUAD


def test_transfer_chip_only_on_exact_convergence():
    bs = mk_bootstrap(); idx, by_id = _ctx(bs)
    n = ListNotifier()
    tom = set(ids(TOM_SQUAD))
    assert decide_transfer_chip("freehit", TOM_SQUAD, tom, idx, bs, by_id,
                                allow_override=False, event_id=3, notifier=n) == "freehit"
    held = decide_transfer_chip("freehit", TOM_SQUAD, set(ids(OUR_SQUAD)), idx, bs, by_id,
                                allow_override=False, event_id=3, notifier=n)
    assert held is None and n.sent[0][0] == "FPL Copycat GW3: freehit held back, not played"
    assert decide_transfer_chip("freehit", TOM_SQUAD, set(ids(OUR_SQUAD)), idx, bs, by_id,
                                allow_override=True, event_id=3, notifier=n) == "freehit"
    assert decide_transfer_chip(None, TOM_SQUAD, tom, idx, bs, by_id,
                                allow_override=False, event_id=3, notifier=n) is None


def test_hit_policy():
    fake = [(({"out": f"o{i}", "in": f"i{i}"}), None, None, 0, 0, f"k{i}") for i in range(3)]
    skipped = []
    kept = apply_hit_policy(list(fake), skipped, unlimited=False, active_chip=None,
                            free_transfers=2, made=1, allow_hits=False)
    assert len(kept) == 1 and len(skipped) == 2 and "-4 hit" in skipped[0][1]
    skipped = []
    kept = apply_hit_policy(list(fake), skipped, unlimited=False, active_chip=None,
                            free_transfers=2, made=1, allow_hits=True)
    assert len(kept) == 3 and skipped == []
    assert any(l.startswith("TAKING HITS: 3 transfer(s) with 1 free -> 2 x -4 = -8")
               for l in logmod.report_lines)
    assert apply_hit_policy(list(fake), [], unlimited=False, active_chip="freehit",
                            free_transfers=0, made=5, allow_hits=False) == fake


# ---------------------------------------------------------------- enabling downgrades
from copycat_core.plan import (UNAFFORDABLE, active_downgrades, plan_enabling_downgrades,  # noqa: E402
                               resolve_ids)
from copycat_core.plan import PlanResult  # noqa: E402
from tests.conftest import GK, MID, PLAYERS, TOM_BENCH, TOM_STARTERS, mk_el  # noqa: E402

# Extra market: cheaper like-for-likes for two of Tom's bench players (Scherpen GK 4.5,
# Palmer MID 9.5). Neither side owns them.
EXTRA = [(901, GK, "Cheapo", 11, 40), (902, MID, "Fodder", 12, 92), (903, MID, "Bargain", 12, 80)]
# Our squad: Tom's 15 with Wissa (6.1) still Calvert-Lewin (6.0); bank 0 -> 0.1 short.
NEARLY = [n if n != "Wissa" else "Calvert-Lewin" for n in TOM_SQUAD]
FIX = {"squad": TOM_SQUAD, "starters": TOM_STARTERS, "bench": TOM_BENCH, "transfers": []}


def _bs_extra(**ep):
    players = [p for p in PLAYERS] + EXTRA
    bs = mk_bootstrap(players)
    for e in bs["elements"]:
        if e["web_name"] in ep:
            e["ep_next"] = str(ep[e["web_name"]])
    return bs


def _plan(bs, squad_names, bank, pairs=None):
    idx, by_id = _ctx(bs)
    state = {"processed": [], "chips_done": [], "notified": [], "downgrades": []}
    squad = set(ids(squad_names))
    picks = mk_picks(squad_names)
    sell = {p["element"]: p["selling_price"] for p in picks}
    club = {}
    for pid in squad:
        club[by_id[pid]["team"]] = club.get(by_id[pid]["team"], 0) + 1
    target_ids = resolve_ids(TOM_SQUAD, idx, bs)
    pending = pairs if pairs is not None else plan_net_transfers(TOM_SQUAD, squad, idx, bs, by_id)[0]
    plan = plan_transfers(pending, state=state, event_id=5, idx=idx, bootstrap=bs, elements_by_id=by_id,
                          squad_ids=squad, sell_price=sell, bank=bank, club_counts=club, already_in=set())
    return plan, dict(pending=pending, fix=FIX, target_ids=target_ids, event_id=5, idx=idx,
                      bootstrap=bs, elements_by_id=by_id, sell_price=sell), by_id


def test_downgrade_funds_the_unaffordable_transfer_with_the_smallest_drop():
    bs = _bs_extra(Scherpen=2.0, Cheapo=2.5, Palmer=6.0, Fodder=4.0)
    plan, kw, by_id = _plan(bs, NEARLY, bank=0)
    assert plan.to_apply == [] and plan.skipped[0][1] == UNAFFORDABLE
    recs = plan_enabling_downgrades(plan, **kw)
    # Palmer 9.5 -> Fodder 9.2 (drop 0.3) beats Scherpen 4.5 -> Cheapo 4.0 (drop 0.5)
    assert [(t[0]["out"], t[0]["in"]) for t in plan.to_apply] == [("Palmer", "Fodder"), ("Calvert-Lewin", "Wissa")]
    assert plan.to_apply[0][0]["role"] == "downgrade" and plan.to_apply[0][0]["enables"] == "Calvert-Lewin -> Wissa"
    assert plan.skipped == [] and plan.bank_left == 2          # +3 from the drop, -1 on the swap
    assert plan.projected_ids == (set(ids(TOM_SQUAD)) - {BY_NAME["Palmer"][0]}) | {902}
    assert recs == [{"gw": 5, "held": BY_NAME["Palmer"][0], "held_name": "Palmer", "have": 902,
                     "have_name": "Fodder", "enabled": "Calvert-Lewin -> Wissa"}]
    assert any(l.startswith("[downgrade] Calvert-Lewin -> Wissa is 0.1 short; funding it by downgrading Palmer (9.5, on their bench) -> Fodder (9.2")
               for l in logmod.report_lines)


def test_downgrade_picks_a_bigger_drop_when_the_small_one_does_not_cover():
    bs = _bs_extra()
    for e in bs["elements"]:
        if e["web_name"] == "Wissa":
            e["now_cost"] = 64                     # now 0.4 short: Fodder's 0.3 drop is not enough
    plan, kw, _ = _plan(bs, NEARLY, bank=0)
    plan_enabling_downgrades(plan, **kw)
    assert [(t[0]["out"], t[0]["in"]) for t in plan.to_apply] == [("Scherpen", "Cheapo"), ("Calvert-Lewin", "Wissa")]


def test_downgrade_prefers_smaller_ep_loss_on_equal_drop_and_skips_unavailable():
    bs = _bs_extra(Palmer=6.0, Fodder=5.9, Bargain=6.5)
    for e in bs["elements"]:
        if e["web_name"] == "Fodder":
            e["now_cost"] = 80                     # same 1.5 drop as Bargain
        if e["web_name"] == "Cheapo":
            e["status"] = "i"                      # injured fodder is useless
    plan, kw, _ = _plan(bs, NEARLY, bank=0)
    plan_enabling_downgrades(plan, **kw)
    assert plan.to_apply[0][0]["in"] == "Bargain"  # ep 6.5 loses less than Fodder's 5.9


def test_downgrade_never_touches_starters_or_planned_sales():
    bs = _bs_extra()
    for e in bs["elements"]:
        if e["web_name"] in ("Cheapo", "Fodder", "Bargain", "Mbeumo"):
            e["now_cost"] = 200                    # no bench stand-in is cheaper any more
    # Kelleher (GK 5.0) is still cheaper than Raya (6.0) - but Raya STARTS for Tom
    plan, kw, _ = _plan(bs, NEARLY, bank=0)
    assert plan_enabling_downgrades(plan, **kw) == []
    assert plan.to_apply == [] and plan.skipped[0][1].endswith("0.1 short and no bench downgrade covers it")


def test_downgrade_respects_the_club_limit():
    bs = _bs_extra()
    for e in bs["elements"]:
        if e["web_name"] in ("Fodder", "Bargain"):
            e["team"] = 1                          # Arsenal already has Raya, Gabriel (+ Saka not owned)
    bs["elements"].append(mk_el(905, MID, "Third", 1, 50))
    plan, kw, by_id = _plan(bs, NEARLY, bank=0)
    # make Arsenal count 3 in our squad by swapping a shared player onto team 1
    by_id[BY_NAME["N.Williams"][0]]["team"] = 1
    plan_enabling_downgrades(plan, **kw)
    assert plan.to_apply[0][0]["in"] == "Cheapo"   # Fodder/Bargain would make a 4th Arsenal player


def test_net_diff_holds_a_downgrade_unless_a_chip_needs_the_full_squad():
    bs = _bs_extra(); idx, by_id = _ctx(bs)
    ours = set(ids(TOM_SQUAD)) - {BY_NAME["Palmer"][0]} | {902}
    pairs, note = plan_net_transfers(TOM_SQUAD, ours, idx, bs, by_id, held={BY_NAME["Palmer"][0]: 902})
    assert pairs == [] and note == "already matching (holding Fodder for Palmer)"
    pairs, note = plan_net_transfers(TOM_SQUAD, ours, idx, bs, by_id, held=None)
    assert pairs == [{"out": "Fodder", "in": "Palmer"}]


def test_active_downgrades_prunes_lapsed_holds():
    bs = _bs_extra(); idx, by_id = _ctx(bs)
    palmer = BY_NAME["Palmer"][0]
    rec = {"gw": 5, "held": palmer, "held_name": "Palmer", "have": 902, "have_name": "Fodder", "enabled": "x"}
    tom = set(ids(TOM_SQUAD))
    ours = (tom - {palmer}) | {902}
    st = {"downgrades": [dict(rec)]}
    assert active_downgrades(st, ours, tom, by_id) == {palmer: 902} and st["downgrades"] == [rec]
    st = {"downgrades": [dict(rec)]}
    assert active_downgrades(st, tom, tom, by_id) == {} and st["downgrades"] == []        # bought back
    st = {"downgrades": [dict(rec)]}
    assert active_downgrades(st, tom - {palmer}, tom, by_id) == {}                        # sold the stand-in
    st = {"downgrades": [dict(rec)]}
    assert active_downgrades(st, ours, tom - {palmer}, by_id) == {}                       # Tom sold Palmer


def test_hit_policy_keeps_a_downgrade_and_its_transfer_together():
    dg = ({"out": "Palmer", "in": "Fodder", "role": "downgrade", "enables": "CL -> Wissa"}, None, None, 0, 0, "k1")
    pair = ({"out": "CL", "in": "Wissa"}, None, None, 0, 0, "k2")
    skipped = []
    kept = apply_hit_policy([dg, pair], skipped, unlimited=False, active_chip=None,
                            free_transfers=1, made=0, allow_hits=False)
    assert kept == [] and {r for _, r in skipped} == {
        "would need a -4 hit (only 1 free transfer(s) left)",
        "only needed to fund CL -> Wissa, which needs a -4 hit"}
    assert apply_hit_policy([dg, pair], [], unlimited=False, active_chip=None,
                            free_transfers=2, made=0, allow_hits=False) == [dg, pair]
