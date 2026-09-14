from copycat_core import log as logmod
from copycat_core.execute import (activate_team_chip, submit_transfers, sync_lineup,
                                  verify_transfers_applied)
from copycat_core.plan import build_player_index
from tests.conftest import (BY_NAME, FakeResponse, FakeSession, OUR_SQUAD, TOM_BENCH,
                            TOM_SQUAD, TOM_STARTERS, ids, mk_bootstrap, mk_picks)


def _ctx():
    bs = mk_bootstrap()
    return bs, build_player_index(bs), {e["id"]: e for e in bs["elements"]}


def _apply(out, inn):
    o, i = BY_NAME[out], BY_NAME[inn]
    return ({"out": out, "in": inn}, {"id": o[0], "web_name": out},
            {"id": i[0], "web_name": inn}, o[4], i[4], f"gw3:{out}->{inn}")


def test_verify_against_live_squad():
    def team(ids_):
        return lambda s, e: {"picks": [{"element": i} for i in ids_]}
    ta = [_apply("Mbeumo", "Elanga")]
    assert verify_transfers_applied(None, 1, ta, get_team=team(ids(TOM_SQUAD))) is True
    assert verify_transfers_applied(None, 1, ta, get_team=team(ids(OUR_SQUAD))) is False

    def boom(s, e):
        raise RuntimeError("down")
    assert verify_transfers_applied(None, 1, ta, get_team=boom) is False


def test_duplicate_confirm_is_treated_as_applied():
    """confirmed:false already commits; confirmed:true then 400s. Must not be a failure."""
    bs, idx, by_id = _ctx()
    s = FakeSession(bs, mk_picks(OUR_SQUAD), confirm_status=400)
    ta = [_apply("Mbeumo", "Elanga"), _apply("Calvert-Lewin", "Wissa"), _apply("Maguire", "Gvardiol")]
    assert submit_transfers(s, 1, 3, ta, "freehit", dry=False) is True
    assert any(l.startswith("Confirm phase rejected (400) but the squad already shows")
               for l in logmod.report_lines)
    assert {p["element"] for p in s.picks} == set(ids(TOM_SQUAD))
    assert s.posts[0][1]["freehit"] is True and s.posts[0][1]["chip"] == "freehit"


def test_real_rejection_is_a_failure():
    bs, idx, by_id = _ctx()
    s = FakeSession(bs, mk_picks(OUR_SQUAD))
    s.post = lambda url, json=None, **kw: FakeResponse(400, None, '{"non_form_errors":["nope"]}')
    assert submit_transfers(s, 1, 3, [_apply("Mbeumo", "Elanga")], None, dry=False) is False
    assert any(l.startswith("ERROR submitting transfers: 400") for l in logmod.report_lines)


def test_dry_run_submits_nothing_but_logs_the_plan():
    bs, idx, by_id = _ctx()
    s = FakeSession(bs, mk_picks(OUR_SQUAD))
    assert submit_transfers(s, 1, 3, [_apply("Mbeumo", "Elanga")], None, dry=True, debug=True)
    assert s.posts == []
    assert logmod.report_lines[0] == "Submitting 1 transfer(s): Mbeumo -> Elanga"
    assert logmod.report_lines[1].startswith("[debug] payload transfers=")


def test_lineup_sync_carries_the_active_chip():
    bs, idx, by_id = _ctx()
    fix = {"starters": TOM_STARTERS, "bench": TOM_BENCH, "captain": "B.Fernandes", "vice": "Wissa"}
    s = FakeSession(bs, mk_picks(TOM_BENCH + TOM_STARTERS))   # wrong order, no armbands
    changed, note = sync_lineup(s, 1, s.picks, fix, idx, bs, by_id, dry=False, chip="3xc")
    assert changed and note == "XI/bench reordered, captain B.Fernandes, vice Wissa"
    url, payload = s.posts[-1]
    assert payload["chip"] == "3xc"                      # a picks POST without it cancels TC
    assert payload["picks"][0]["element"] == BY_NAME["Raya"][0]        # GK at position 1
    assert payload["picks"][11]["element"] == BY_NAME["Scherpen"][0]   # reserve GK at 12
    cap = [p for p in payload["picks"] if p["is_captain"]]
    assert len(cap) == 1 and cap[0]["element"] == BY_NAME["B.Fernandes"][0]


def test_lineup_sync_refusals():
    bs, idx, by_id = _ctx()
    good = {"starters": TOM_STARTERS, "bench": TOM_BENCH, "captain": "B.Fernandes", "vice": "Wissa"}
    picks = mk_picks(TOM_SQUAD, captain="B.Fernandes", vice="Wissa")
    assert sync_lineup(None, 1, picks, good, idx, bs, by_id, dry=False) == (False, "lineup already matches")
    assert sync_lineup(None, 1, picks, dict(good, captain="Scherpen"), idx, bs, by_id, dry=False) == \
        (False, "captain is not in the starting XI")
    assert sync_lineup(None, 1, picks, dict(good, bench=["Scherpen"]), idx, bs, by_id, dry=False)[1] \
        .startswith("lineup unusable")
    assert sync_lineup(None, 1, picks, dict(good, captain="Nobody"), idx, bs, by_id, dry=False) == \
        (False, "could not resolve captain 'Nobody'")
    assert sync_lineup(None, 1, mk_picks(OUR_SQUAD), good, idx, bs, by_id, dry=False) == \
        (False, "lineup names do not match the squad we own")
    ok, note = sync_lineup(None, 1, mk_picks(TOM_SQUAD), good, idx, bs, by_id, dry=True)
    assert ok is False and note.startswith("[dry-run] would set XI/bench reordered")


def test_team_chip_activation_posts_current_picks():
    bs, idx, by_id = _ctx()
    s = FakeSession(bs, mk_picks(TOM_SQUAD))
    assert activate_team_chip(s, 1, "3xc", s.picks) is True
    url, payload = s.posts[-1]
    assert payload["chip"] == "3xc" and len(payload["picks"]) == 15
    s.post = lambda url, json=None, **kw: FakeResponse(400, None, "no")
    assert activate_team_chip(s, 1, "3xc", s.picks) is False
    assert any(l.startswith("ERROR activating 3xc: 400") for l in logmod.report_lines)
