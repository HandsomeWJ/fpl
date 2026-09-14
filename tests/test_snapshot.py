import gzip
import json
import os
from datetime import datetime, timezone

from copycat_core import log as logmod
from copycat_core.snapshot import (POST_WINDOW_MIN, PRE_WINDOW_MIN, safe_take_snapshot, slot_for,
                                   take_snapshot)
from tests.conftest import FakeResponse, mk_bootstrap


def utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_slots_follow_midnight_uk_across_dst():
    # BST: change at 23:00Z. Late on 26 Aug describes 26 Aug; just after describes 27 Aug.
    assert slot_for(utc("2026-08-26T22:40:00")) == ("pre", "2026-08-26")
    assert slot_for(utc("2026-08-26T23:20:00")) == ("post", "2026-08-27")
    assert slot_for(utc("2026-08-26T12:00:00")) == (None, None)
    # GMT: change at 00:00Z.
    assert slot_for(utc("2026-12-10T23:50:00")) == ("pre", "2026-12-10")
    assert slot_for(utc("2026-12-11T00:30:00")) == ("post", "2026-12-11")
    # Window edges.
    assert slot_for(utc("2026-08-26T23:00:00") - __import__("datetime").timedelta(minutes=PRE_WINDOW_MIN))[0] == "pre"
    assert slot_for(utc("2026-08-26T23:00:00") + __import__("datetime").timedelta(minutes=POST_WINDOW_MIN))[0] == "post"
    assert slot_for(utc("2026-08-26T23:00:00") + __import__("datetime").timedelta(minutes=POST_WINDOW_MIN + 1))[0] is None


class _Http:
    def __init__(self, bootstrap):
        self.bootstrap = bootstrap
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        return FakeResponse(200, self.bootstrap)


def test_writes_slim_gz_and_latest(tmp_path):
    bs = mk_bootstrap()
    bs["teams"] = [{"id": 1, "name": "Arsenal", "short_name": "ARS", "strength": 5}]
    http = _Http(bs)
    path = take_snapshot(utc("2026-08-26T23:20:00"), str(tmp_path), http=http)
    assert path.endswith(os.path.join("prices", "2026-08-27", "post.json.gz"))
    with gzip.open(path, "rt") as f:
        doc = json.load(f)
    assert doc["meta"]["slot"] == "post" and doc["meta"]["uk_date"] == "2026-08-27"
    assert doc["meta"]["event_next"] == 3
    assert len(doc["players"]) == len(bs["elements"])
    assert set(doc["players"][0]) >= {"id", "web_name", "now_cost", "transfers_in_event"}
    assert "photo" not in doc["players"][0]                       # slim, not the full record
    assert doc["teams"][0]["short_name"] == "ARS"
    latest = json.load(open(tmp_path / "prices" / "latest.json"))
    assert latest["meta"]["fetched_at"] == doc["meta"]["fetched_at"]
    assert logmod.report_lines[-1].startswith("[snapshot] wrote ")


def test_post_is_written_once_but_pre_is_refreshed(tmp_path):
    http = _Http(mk_bootstrap())
    assert take_snapshot(utc("2026-08-26T23:20:00"), str(tmp_path), http=http)
    assert take_snapshot(utc("2026-08-26T23:35:00"), str(tmp_path), http=http) is None
    assert http.calls == 1
    assert take_snapshot(utc("2026-08-27T22:30:00"), str(tmp_path), http=http)
    assert take_snapshot(utc("2026-08-27T22:45:00"), str(tmp_path), http=http)   # latest wins
    assert http.calls == 3


def test_outside_windows_does_nothing_unless_forced(tmp_path):
    http = _Http(mk_bootstrap())
    assert take_snapshot(utc("2026-08-26T12:00:00"), str(tmp_path), http=http) is None
    assert http.calls == 0
    path = take_snapshot(utc("2026-08-26T12:00:00"), str(tmp_path), http=http, force=True)
    assert path.endswith("manual.json.gz")


def test_failure_never_propagates(tmp_path):
    class Boom:
        def get(self, url, **kw):
            raise RuntimeError("fpl down")
    assert safe_take_snapshot(utc("2026-08-26T23:20:00"), str(tmp_path), http=Boom()) is None
    assert logmod.report_lines[-1] == "[snapshot] failed: fpl down"
