"""End-to-end runs with a fake reveal page and a fake FPL session. These pin the exact
log lines the owner reads in Actions, so a refactor that changes behaviour fails here."""

from datetime import datetime, timezone

import pytest

from copycat_core import log as logmod
from copycat_core.fix import parse_reveal
from copycat_core.ports import ListNotifier, MemoryState, MemoryTokenStore
from copycat_core.runner import Deps, RunOutcome, SubmitFailed, run
from copycat_core.settings import Settings
from tests.conftest import BY_NAME, FakeSession, OUR_SQUAD, TOM_SQUAD, ids, mk_bootstrap, mk_picks

TOKENS = {"access_token": "at", "refresh_token": "rt", "expires_at": 4e9}


def _deps(session, html, tokens=TOKENS):
    return Deps(state=MemoryState(), tokens=MemoryTokenStore(dict(tokens)), notifier=ListNotifier(),
                session_factory=lambda token, cookie: session,
                fetch_fix=lambda name: parse_reveal(html, name),
                now_fn=lambda: datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc))


import os
import tempfile

TMP = tempfile.mkdtemp(prefix="copycat-test-")


def _settings(**env):
    # state/data under a temp dir so tests never write into the repo's data/
    return Settings.from_env({"FPL_ENTRY": "7953181", **env},
                             state_path=os.path.join(TMP, "state", "state.json"))


def test_dry_run_plans_gw3_exactly(reveal_html):
    s = FakeSession(mk_bootstrap(), mk_picks(OUR_SQUAD), bank=0, free=2, made=5)
    deps = _deps(s, reveal_html)
    out = run(_settings(DRY_RUN="1"), deps)
    lines = logmod.report_lines
    assert lines[0].startswith("Fix reveal for Tom Dollimore: chips={'WC1': 'available'")
    assert "[plan] 3 net transfer(s) to match the target squad" in lines
    assert "Playing chip freehit: projected squad matches the target manager exactly." in lines
    sub = next(l for l in lines if l.startswith("Submitting 3 transfer(s): "))
    assert sub.endswith(" with chip freehit")
    for pair in ("Mbeumo -> Elanga", "Calvert-Lewin -> Wissa", "Maguire -> Gvardiol"):
        assert pair in sub
    assert "[dry-run] would have opened an issue: 'FPL Copycat GW3: 3 transfer(s) applied' (not opening)" in lines
    assert lines[-1] == "Done. changed=True dry_run=True"
    assert s.posts == [] and deps.state.saved == [] and deps.notifier.sent == []
    assert isinstance(out, RunOutcome) and out.changed


def test_live_run_applies_transfers_survives_duplicate_confirm_and_syncs_lineup(reveal_html):
    s = FakeSession(mk_bootstrap(), mk_picks(OUR_SQUAD), bank=0, free=2, made=5, confirm_status=400)
    deps = _deps(s, reveal_html)
    run(_settings(), deps)
    lines = logmod.report_lines
    assert any(l.startswith("Confirm phase rejected (400) but the squad already shows") for l in lines)
    assert {p["element"] for p in s.picks} == set(ids(TOM_SQUAD))
    assert s.chips["freehit"] == "active"
    assert "[lineup] XI/bench reordered, captain B.Fernandes, vice Wissa" in lines
    cap = [p for p in s.picks if p["is_captain"]]
    assert len(cap) == 1 and cap[0]["element"] == BY_NAME["B.Fernandes"][0]
    state = deps.state.saved[-1]
    assert sorted(state["processed"]) == sorted(
        ["gw3:Mbeumo->Elanga", "gw3:Calvert-Lewin->Wissa", "gw3:Maguire->Gvardiol"])
    assert deps.notifier.sent[0][0] == "FPL Copycat GW3: 3 transfer(s) applied"
    assert lines[-1] == "Done. changed=True dry_run=False"


def test_already_matching_does_nothing(reveal_html):
    s = FakeSession(mk_bootstrap(), mk_picks(TOM_SQUAD, captain="B.Fernandes", vice="Wissa"),
                    chips={"freehit": "active", "wildcard": "unavailable",
                           "bboost": "unavailable", "3xc": "unavailable"})
    deps = _deps(s, reveal_html)
    run(_settings(), deps)
    assert "[plan] already matching" in logmod.report_lines
    assert "[lineup] lineup already matches" in logmod.report_lines
    assert s.posts == [] and deps.notifier.sent == []
    assert logmod.report_lines[-1] == "Done. changed=False dry_run=False"


def test_no_free_transfers_and_no_chip_skips_instead_of_hitting(reveal_html):
    html = reveal_html.replace('<span class="rchip__chip">FH</span><span class="rchip__status">Active</span>',
                               '<span class="rchip__chip">FH</span><span class="rchip__status">GW 3</span>')
    s = FakeSession(mk_bootstrap(), mk_picks(OUR_SQUAD), bank=0, free=2, made=2)
    deps = _deps(s, html)
    run(_settings(), deps)
    assert s.posts == []
    title, body = deps.notifier.sent[0]
    assert title == "FPL Copycat GW3: action needed"
    assert body.count("would need a -4 hit (only 0 free transfer(s) left)") == 3


def test_allow_hits_takes_them(reveal_html):
    html = reveal_html.replace('<span class="rchip__chip">FH</span><span class="rchip__status">Active</span>',
                               '<span class="rchip__chip">FH</span><span class="rchip__status">GW 3</span>')
    s = FakeSession(mk_bootstrap(), mk_picks(OUR_SQUAD), bank=0, free=2, made=2)
    run(_settings(ALLOW_HITS="1"), _deps(s, html))
    assert "TAKING HITS: 3 transfer(s) with 0 free -> 3 x -4 = -12 points (ALLOW_HITS=1)." in logmod.report_lines
    assert {p["element"] for p in s.picks} == set(ids(TOM_SQUAD))


def test_team_chip_is_activated_then_carried_on_lineup_save(reveal_html):
    html = reveal_html.replace('<span class="rchip__chip">FH</span><span class="rchip__status">Active</span>',
                               '<span class="rchip__chip">FH</span><span class="rchip__status">GW 3</span>') \
        .replace('<span class="rchip__chip">TC</span><span class="rchip__status">Available</span>',
                 '<span class="rchip__chip">TC</span><span class="rchip__status">Active</span>')
    s = FakeSession(mk_bootstrap(), mk_picks(TOM_SQUAD))   # squad matches; lineup does not
    deps = _deps(s, html)
    run(_settings(), deps)
    assert "Activating chip 3xc (target manager has TC active)." in logmod.report_lines
    lineup_post = s.posts[-1][1]
    assert "picks" in lineup_post and lineup_post["chip"] == "3xc"
    assert s.chips["3xc"] == "active"
    assert "gw3:3xc" in deps.state.saved[-1]["chips_done"]


def test_real_submit_failure_notifies_and_raises(reveal_html):
    s = FakeSession(mk_bootstrap(), mk_picks(OUR_SQUAD))
    real_post = s.post

    def reject(url, json=None, **kw):
        if url.endswith("/api/transfers/"):
            from tests.conftest import FakeResponse
            return FakeResponse(400, None, '{"non_form_errors":["Insufficient funds"]}')
        return real_post(url, json=json, **kw)
    s.post = reject
    deps = _deps(s, reveal_html)
    with pytest.raises(SubmitFailed):
        run(_settings(), deps)
    assert deps.notifier.sent[0][0] == "FPL Copycat: transfer submission FAILED (GW3)"
    assert deps.state.saved and deps.state.saved[-1]["processed"] == []


def test_deadline_passed_and_gw_mismatch_short_circuit(reveal_html):
    s = FakeSession(mk_bootstrap(deadline="2026-01-01T00:00:00Z"), mk_picks(OUR_SQUAD))
    run(_settings(), _deps(s, reveal_html))
    assert "GW3 deadline has passed; nothing to do until next GW opens." in logmod.report_lines
    logmod.reset_report()
    s = FakeSession(mk_bootstrap(event_id=4), mk_picks(OUR_SQUAD))
    run(_settings(), _deps(s, reveal_html))
    assert any("waiting for the reveal page to roll over" in l for l in logmod.report_lines)


def test_token_rotation_persist_failure_is_escalated(reveal_html):
    s = FakeSession(mk_bootstrap(), mk_picks(TOM_SQUAD, captain="B.Fernandes", vice="Wissa"),
                    chips={"freehit": "active", "wildcard": "unavailable",
                           "bboost": "unavailable", "3xc": "unavailable"})
    deps = _deps(s, reveal_html, tokens={"refresh_token": "old", "expires_at": 0})
    deps.tokens = MemoryTokenStore({"refresh_token": "old", "expires_at": 0}, fail_save=True)
    from tests.conftest import FakeResponse
    deps.http = type("H", (), {"post": staticmethod(lambda url, **kw: FakeResponse(
        200, {"access_token": "new-at", "refresh_token": "new-rt", "expires_in": 3600}))})()
    out = run(_settings(), deps)
    assert out.persist_failed is True
    assert deps.notifier.sent[0][0] == "FPL Copycat: token rotation could not be saved"


def test_live_run_writes_preview_and_ledger_records(reveal_html):
    import glob, json as _json
    s = FakeSession(mk_bootstrap(), mk_picks(OUR_SQUAD), bank=0, free=2, made=5, confirm_status=400)
    settings = _settings()
    for f in glob.glob(os.path.join(settings.data_dir, "ledger", "*.json")):
        os.remove(f)
    run(settings, _deps(s, reveal_html))
    prev = _json.load(open(os.path.join(settings.data_dir, "preview", "latest.json")))
    assert prev["kind"] == "run" and prev["result"] == "applied" and prev["event_id"] == 3
    assert {t["out"] for t in prev["to_apply"]} == {"Mbeumo", "Calvert-Lewin", "Maguire"}
    assert prev["transfer_chip"] == "freehit" and prev["hits"] == {"count": 0, "points": 0}
    assert prev["fix"]["captain"] == "B.Fernandes" and len(prev["team"]["squad"]) == 15
    assert len(prev["team"]["players"]) == 15 and {"id", "selling_price", "now_cost"} <= set(prev["team"]["players"][0])
    assert any(l.startswith("Submitting 3 transfer(s)") for l in prev["log"])
    ledgers = glob.glob(os.path.join(settings.data_dir, "ledger", "*_gw3.json"))
    assert len(ledgers) == 1
    assert "[record] ledger  -> " in " ".join(logmod.report_lines)


def test_dry_run_writes_preview_only(reveal_html):
    import glob, json as _json
    s = FakeSession(mk_bootstrap(), mk_picks(TOM_SQUAD, captain="B.Fernandes", vice="Wissa"),
                    chips={"freehit": "active", "wildcard": "unavailable",
                           "bboost": "unavailable", "3xc": "unavailable"})
    settings = _settings(DRY_RUN="1")
    for f in glob.glob(os.path.join(settings.data_dir, "ledger", "*.json")):
        os.remove(f)
    run(settings, _deps(s, reveal_html))
    prev = _json.load(open(os.path.join(settings.data_dir, "preview", "latest.json")))
    assert prev["kind"] == "preview" and prev["result"] == "dry" and prev["to_apply"] == []
    assert prev["plan_note"] == "already matching"
    assert glob.glob(os.path.join(settings.data_dir, "ledger", "*.json")) == []


# ---------------------------------------------------------------- enabling downgrades
from tests.conftest import GK, MID, PLAYERS, TOM_BENCH  # noqa: E402

NEARLY = [n if n != "Wissa" else "Calvert-Lewin" for n in TOM_SQUAD]   # 0.1 short of Wissa


def _bs_market():
    return mk_bootstrap(list(PLAYERS) + [(901, GK, "Cheapo", 11, 40), (902, MID, "Fodder", 12, 92)])


def test_live_run_funds_an_unaffordable_transfer_by_downgrading_a_bench_player(reveal_html):
    s = FakeSession(_bs_market(), mk_picks(NEARLY), bank=0, free=2, made=0,
                    chips={"wildcard": "available", "freehit": "played", "bboost": "available", "3xc": "available"})
    deps = _deps(s, reveal_html)
    run(_settings(ALLOW_DOWNGRADE="1", ALLOW_HITS="1"), deps)
    lines = logmod.report_lines
    assert "[plan] 1 net transfer(s) to match the target squad" in lines
    assert any(l.startswith("[downgrade] Calvert-Lewin -> Wissa is 0.1 short; funding it by downgrading Palmer (9.5, on their bench) -> Fodder (9.2") for l in lines)
    sub = next(l for l in lines if l.startswith("Submitting 2 transfer(s): "))
    assert "Palmer -> Fodder" in sub and "Calvert-Lewin -> Wissa" in sub
    owned = {p["element"] for p in s.picks}
    assert owned == (set(ids(TOM_SQUAD)) - {BY_NAME["Palmer"][0]}) | {902}
    # lineup mirrored with Fodder in Palmer's bench slot
    assert "[lineup] XI/bench reordered, captain B.Fernandes, vice Wissa" in lines
    picks_post = [p for u, p in s.posts if "/api/my-team/" in u and "picks" in p][-1]
    assert picks_post["picks"][12]["element"] == 902
    state = deps.state.saved[-1]
    assert state["downgrades"] == [{"gw": 3, "held": BY_NAME["Palmer"][0], "held_name": "Palmer", "have": 902,
                                    "have_name": "Fodder", "enabled": "Calvert-Lewin -> Wissa"}]
    assert "gw3:Palmer->Fodder" in state["processed"] and "gw3:Calvert-Lewin->Wissa" in state["processed"]
    assert deps.notifier.sent[0][0] == "FPL Copycat GW3: 2 transfer(s) applied"

    # next run: the hold keeps us from buying Palmer back; nothing to do, lineup fine
    logmod.reset_report()
    run(_settings(ALLOW_DOWNGRADE="1", ALLOW_HITS="1"), deps)
    lines = logmod.report_lines
    assert "[plan] already matching (holding Fodder for Palmer)" in lines
    assert not any(l.startswith("Submitting") for l in lines)
    assert "[lineup] lineup already matches" in lines
    assert deps.state.saved[-1]["downgrades"][0]["have"] == 902


def test_default_recommends_the_downgrade_instead_of_applying_it(reveal_html):
    import json
    s = FakeSession(_bs_market(), mk_picks(NEARLY), bank=0, free=2, made=0,
                    chips={"wildcard": "available", "freehit": "played", "bboost": "available", "3xc": "available"})
    deps = _deps(s, reveal_html)
    run(_settings(ALLOW_HITS="1"), deps)                                  # ALLOW_DOWNGRADE unset -> recommend
    lines = logmod.report_lines
    assert any(l.startswith("[downgrade] Calvert-Lewin -> Wissa is 0.1 short; SUGGESTED (not applied): downgrade Palmer") for l in lines)
    assert not any(l.startswith("Submitting") for l in lines) and s.posts == []
    assert {p["element"] for p in s.picks} == set(ids(NEARLY))            # nothing changed
    title, body = deps.notifier.sent[0]
    assert title == "FPL Copycat GW3: action needed"
    assert "Suggested downgrade (NOT applied):" in body
    assert "- downgrade Palmer (9.5) -> Fodder (9.2) to fund Calvert-Lewin -> Wissa" in body
    assert "gh workflow run copycat.yml -R HandsomeWJ/fpl -f allow_downgrade=1" in body
    prev = json.load(open(os.path.join(TMP, "data", "preview", "latest.json")))
    assert prev["recommendations"][0]["applied"] is False and prev["recommendations"][0]["in"] == "Fodder"
    assert prev["to_apply"] == [] and "suggested: downgrade Palmer" in prev["skipped"][0]["reason"]
    assert deps.state.saved[-1]["downgrades"] == []


def test_preview_record_marks_downgrade_transfers(reveal_html):
    import json
    s = FakeSession(_bs_market(), mk_picks(NEARLY), bank=0, free=2, made=0,
                    chips={"wildcard": "available", "freehit": "played", "bboost": "available", "3xc": "available"})
    deps = _deps(s, reveal_html)
    run(_settings(DRY_RUN="1", ALLOW_DOWNGRADE="1", ALLOW_HITS="1"), deps)
    prev = json.load(open(os.path.join(TMP, "data", "preview", "latest.json")))
    roles = [(t["out"], t["in"], t["role"], t["enables"]) for t in prev["to_apply"]]
    assert roles == [("Palmer", "Fodder", "downgrade", "Calvert-Lewin -> Wissa"),
                     ("Calvert-Lewin", "Wissa", "mirror", None)]
    assert prev["held_downgrades"] == [] and prev["skipped"] == []


def test_on_record_hook_receives_the_finished_record(reveal_html):
    got = []
    s = FakeSession(mk_bootstrap(), mk_picks(OUR_SQUAD), bank=0, free=2, made=5)
    deps = _deps(s, reveal_html)
    deps.on_record = got.append
    run(_settings(DRY_RUN="1"), deps)
    assert len(got) == 1 and got[0].kind == "preview" and len(got[0].to_apply) == 3
    assert got[0].log and got[0].log[0].startswith("Fix reveal for Tom Dollimore")


def test_repeated_skip_writes_no_second_ledger_file(reveal_html):
    import glob
    s = FakeSession(_bs_market(), mk_picks(NEARLY), bank=0, free=2, made=0,
                    chips={"wildcard": "available", "freehit": "played", "bboost": "available", "3xc": "available"})
    deps = _deps(s, reveal_html)
    deps.now_fn = lambda: datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc)   # unique stamps for this test
    before = set(glob.glob(os.path.join(TMP, "data", "ledger", "*.json")))
    run(_settings(ALLOW_HITS="1"), deps)                       # first time: skip is new -> ledger file
    after_first = set(glob.glob(os.path.join(TMP, "data", "ledger", "*.json")))
    assert len(after_first - before) == 1
    logmod.reset_report()
    deps.now_fn = lambda: datetime(2026, 9, 1, 5, 15, tzinfo=timezone.utc)
    run(_settings(ALLOW_HITS="1"), deps)                       # same skip again -> no new file, no issue
    after_second = set(glob.glob(os.path.join(TMP, "data", "ledger", "*.json")))
    assert after_second == after_first and len(deps.notifier.sent) == 1
