"""Shared fakes: a bootstrap builder and an FPL session that behaves like the real one
in the ways that bit us (confirmed:false commits, my-team reflects transfers)."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from copycat_core.log import reset_report  # noqa: E402

GK, DEF, MID, FWD = 1, 2, 3, 4

# (id, type, web_name, team, now_cost) - names are real FPL players (public data),
# ids/teams/prices are synthetic and chosen to make the GW3 arithmetic reproducible.
PLAYERS = [
    (1, GK, "Raya", 1, 60), (564, GK, "Scherpen", 2, 45), (82, GK, "Kelleher", 3, 50),
    (4, DEF, "Gabriel", 1, 80), (391, DEF, "Gvardiol", 15, 56), (229, DEF, "Tarkowski", 5, 60),
    (469, DEF, "N.Williams", 6, 50), (418, DEF, "Maguire", 16, 50), (304, DEF, "O'Shea", 7, 40),
    (426, MID, "B.Fernandes", 16, 120), (237, MID, "Ndiaye", 5, 60), (454, MID, "Elanga", 17, 61),
    (368, MID, "Szoboszlai", 8, 70), (154, MID, "Palmer", 9, 95), (427, MID, "Mbeumo", 16, 80),
    (295, FWD, "McBurnie", 10, 55), (464, FWD, "Wissa", 17, 61), (165, FWD, "João Pedro", 9, 76),
    (346, FWD, "Calvert-Lewin", 13, 60), (12, MID, "Saka", 1, 95),
]
BY_NAME = {p[2]: p for p in PLAYERS}

# Tom's 15 (GW3 target). Starters GK-first, bench reserve-keeper-first.
TOM_STARTERS = ["Raya", "Gabriel", "Gvardiol", "Tarkowski", "N.Williams",
                "B.Fernandes", "Ndiaye", "Elanga", "Szoboszlai", "McBurnie", "Wissa"]
TOM_BENCH = ["Scherpen", "Palmer", "João Pedro", "O'Shea"]
TOM_SQUAD = TOM_STARTERS + TOM_BENCH
# Our GW3 squad before mirroring: differs by exactly three players.
OUR_SQUAD = ["Raya", "N.Williams", "Gabriel", "Maguire", "O'Shea", "Palmer", "B.Fernandes",
             "Ndiaye", "Mbeumo", "João Pedro", "Calvert-Lewin", "Scherpen", "Tarkowski",
             "Szoboszlai", "McBurnie"]


def mk_el(pid, etype, name, team, cost, **kw):
    e = {"id": pid, "web_name": name, "first_name": "F", "second_name": name,
         "element_type": etype, "team": team, "now_cost": cost,
         "selected_by_percent": "5.0", "status": "a"}
    e.update(kw)
    return e


def mk_bootstrap(players=PLAYERS, event_id=3, deadline="2099-01-01T17:30:00Z"):
    return {"elements": [mk_el(*p) for p in players],
            "events": [{"id": event_id, "name": f"Gameweek {event_id}", "is_next": True,
                        "is_current": False, "deadline_time": deadline}]}


def ids(names):
    return [BY_NAME[n][0] for n in names]


def mk_picks(names, captain=None, vice=None):
    return [{"element": BY_NAME[n][0], "position": i + 1, "is_captain": n == captain,
             "is_vice_captain": n == vice, "selling_price": BY_NAME[n][4]}
            for i, n in enumerate(names)]


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text if text else (json.dumps(json_data) if json_data is not None else "")

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Stands in for requests.Session against FPL.

    Mimics the behaviour that matters: the confirmed:false POST ALREADY applies the
    transfers to my-team, and the confirmed:true POST then 400s as a duplicate.
    """

    def __init__(self, bootstrap, picks, bank=0, free=2, made=0, chips=None,
                 gw_transfers=None, confirm_status=400):
        self.bootstrap = bootstrap
        self.picks = picks
        self.bank, self.free, self.made = bank, free, made
        self.chips = chips or {"wildcard": "available", "freehit": "available",
                               "bboost": "available", "3xc": "available"}
        self.gw_transfers = gw_transfers or []
        self.confirm_status = confirm_status
        self.posts = []

    def _team(self):
        return {"picks": self.picks,
                "transfers": {"bank": self.bank, "limit": self.free, "made": self.made},
                "chips": [{"name": n, "status_for_entry": s} for n, s in self.chips.items()]}

    def get(self, url, **kw):
        if url.endswith("/api/bootstrap-static/"):
            return FakeResponse(200, self.bootstrap)
        if "/api/my-team/" in url:
            return FakeResponse(200, self._team())
        if url.endswith("/transfers/"):
            return FakeResponse(200, self.gw_transfers)
        return FakeResponse(404, {}, "not found")

    def post(self, url, json=None, **kw):
        self.posts.append((url, json))
        if url.endswith("/api/transfers/"):
            if not json.get("confirmed"):
                # FPL applies the batch on the "validation" call.
                by_id = {p["element"]: p for p in self.picks}
                for t in json["transfers"]:
                    old = by_id.pop(t["element_out"])
                    by_id[t["element_in"]] = dict(old, element=t["element_in"],
                                                  selling_price=t["purchase_price"])
                self.picks = list(by_id.values())
                self.made += len(json["transfers"])
                if json.get("chip") in ("freehit", "wildcard"):
                    self.chips[json["chip"]] = "active"
                return FakeResponse(200, {}, "")
            return FakeResponse(self.confirm_status, None,
                                '{"transfers":[{"element_in":[{"message":"Element in is '
                                'already picked"}],"element_out":[{"message":"Element out '
                                'is not a current pick"}]}]}')
        if "/api/my-team/" in url:
            if json.get("chip"):
                self.chips[json["chip"]] = "active"
            if "picks" in json:
                by_id = {p["element"]: p for p in self.picks}
                self.picks = [dict(by_id[p["element"]], **p) for p in json["picks"]]
            return FakeResponse(200, {}, "")
        return FakeResponse(404, {}, "not found")


@pytest.fixture(autouse=True)
def _fresh_report():
    reset_report()
    yield
    reset_report()


@pytest.fixture
def bootstrap():
    return mk_bootstrap()


@pytest.fixture
def reveal_html():
    with open(os.path.join(os.path.dirname(__file__), "fixtures", "reveal_synthetic.html")) as f:
        return f.read()
