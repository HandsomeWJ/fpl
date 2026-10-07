"""TokenManager: transient endpoint failures retry and are never reported as dead
credentials. 2026-10-07: one 504 from account.premierleague.com paged the owner to log
in again, although the stored token was fine and the next tick refreshed normally."""

import pytest
import requests

from copycat_core.fpl import TokenEndpointUnavailable, TokenManager


class Resp:
    def __init__(self, status_code, body=None, text=""):
        self.status_code, self._body, self.text = status_code, body, text

    def json(self):
        return self._body


class Http:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(data["refresh_token"])
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


class Store:
    def __init__(self, tokens=None):
        self.tokens = tokens

    def load(self):
        return self.tokens

    def save(self, tokens):
        self.tokens = tokens
        return True


class Notes:
    def __init__(self):
        self.sent = []

    def notify(self, title, body=""):
        self.sent.append(title)


def ok():
    return Resp(200, {"access_token": "acc", "refresh_token": "rt2", "expires_in": 3600})


def manager(http, tokens=None, seed=None, **kw):
    sleeps = []
    m = TokenManager(Store(tokens), seed, Notes(), http=http, now_fn=lambda: 1000.0,
                     sleep_fn=sleeps.append, **kw)
    return m, sleeps


def test_transient_5xx_is_retried_and_recovers():
    http = Http([Resp(504, text='{"message": "Network error communicating with endpoint"}'), ok()])
    m, sleeps = manager(http, tokens={"refresh_token": "rt1"})
    assert m.access_token() == "acc"
    assert http.calls == ["rt1", "rt1"] and sleeps == [2]
    assert m.store.tokens["refresh_token"] == "rt2"


def test_network_errors_are_retried_too():
    http = Http([requests.ConnectionError("boom"), requests.Timeout("slow"), ok()])
    m, sleeps = manager(http, tokens={"refresh_token": "rt1"})
    assert m.access_token() == "acc" and sleeps == [2, 5]


def test_endpoint_down_is_not_a_credential_failure_and_spares_the_seed():
    http = Http([Resp(504), Resp(502), Resp(503)])
    m, _ = manager(http, tokens={"refresh_token": "rt1"}, seed="seed-token")
    with pytest.raises(TokenEndpointUnavailable) as e:
        m.access_token()
    assert "Not a credential problem" in str(e.value) and "log in" not in str(e.value)
    assert http.calls == ["rt1", "rt1", "rt1"]          # the seed was never spent


def test_dead_tokens_still_ask_for_a_reseed_with_the_callers_hint():
    dead = '{"error": "invalid_grant"}'
    http = Http([Resp(400, text=dead), Resp(400, text=dead)])
    m, sleeps = manager(http, tokens={"refresh_token": "rt1"}, seed="seed-token",
                        reseed_hint="seed a new token in Dugout")
    with pytest.raises(RuntimeError) as e:
        m.access_token()
    assert not isinstance(e.value, TokenEndpointUnavailable)
    assert "log in to fantasy.premierleague.com" in str(e.value)
    assert str(e.value).endswith("seed a new token in Dugout.")
    assert http.calls == ["rt1", "seed-token"] and sleeps == []


def test_valid_access_token_is_reused_without_calling_the_endpoint():
    http = Http([])
    m, _ = manager(http, tokens={"access_token": "acc", "refresh_token": "rt1",
                                 "expires_at": 1000.0 + 3600})
    assert m.access_token() == "acc" and http.calls == []
