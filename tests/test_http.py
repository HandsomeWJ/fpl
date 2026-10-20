"""Upstream hiccups retry inside the tick and end as UpstreamUnavailable, never as a
page saying something is broken (FPL my-team 500 on 2026-10-20, reveal-site read
timeouts on 2026-10-18)."""

import pytest
import requests

from copycat_core import http as core_http
from copycat_core.fpl import TokenEndpointUnavailable, get_my_team
from copycat_core.http import UpstreamUnavailable, get_with_retry


class Resp:
    def __init__(self, status_code, body=None, text=""):
        self.status_code, self._body, self.text = status_code, body, text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class Session:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def get(self, url, **kw):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(core_http, "SLEEP", sleeps.append)
    return sleeps


def test_a_500_then_a_200_is_one_retry(no_sleep):
    s = Session([Resp(500, text="Internal Server Error"), Resp(200, {"picks": []})])
    assert get_my_team(s, 7953181) == {"picks": []}
    assert s.calls == 2 and no_sleep == [2]


def test_persistent_outage_is_transient_not_fatal(no_sleep):
    s = Session([requests.ConnectionError("boom"), Resp(503), requests.Timeout("slow")])
    with pytest.raises(UpstreamUnavailable) as e:
        get_my_team(s, 7953181)
    assert "FPL my-team unavailable after 3 attempts" in str(e.value) and no_sleep == [2, 5]


def test_client_errors_are_not_retried(no_sleep):
    s = Session([Resp(403, text="forbidden")])
    with pytest.raises(RuntimeError) as e:
        get_my_team(s, 7953181)
    assert "FPL auth failed (403)" in str(e.value) and s.calls == 1 and no_sleep == []
    assert get_with_retry(lambda: Resp(404), "x").status_code == 404


def test_token_outage_is_an_upstream_outage():
    assert issubclass(TokenEndpointUnavailable, UpstreamUnavailable)
