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
