"""The three things the core needs from the outside world, and their adapters.

StateStore  - dedup record (processed transfers, chips done, skips already reported)
TokenStore  - the rotating FPL refresh/access token pair
Notifier    - how the owner hears about applied transfers, skips and failures

Today: a JSON file committed to git, a GitHub Actions variable, and GitHub issues.
The app swaps in Postgres, encrypted DB rows and Telegram without touching the core.
In-memory versions exist for tests.
"""

from __future__ import annotations

import json
import os
from typing import Optional, Protocol

import requests

from .log import log, report_lines

EMPTY_STATE = {"processed": [], "chips_done": [], "notified": [], "downgrades": []}


class StateStore(Protocol):
    def load(self) -> dict: ...
    def save(self, state: dict) -> None: ...


class TokenStore(Protocol):
    def load(self) -> Optional[dict]: ...
    def save(self, tokens: dict) -> bool: ...


class Notifier(Protocol):
    def notify(self, title: str, body: str = "") -> None: ...


# ---------------------------------------------------------------- state
class JsonFileState:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except Exception:
            return {k: list(v) for k, v in EMPTY_STATE.items()}

    def save(self, state: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(state, f, indent=1)


class MemoryState:
    def __init__(self, initial: Optional[dict] = None):
        self.state = initial or {k: list(v) for k, v in EMPTY_STATE.items()}
        self.saved: list[dict] = []

    def load(self) -> dict:
        return self.state

    def save(self, state: dict) -> None:
        self.state = state
        self.saved.append(json.loads(json.dumps(state)))


# ---------------------------------------------------------------- tokens
def _gh_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


class GitHubVariableTokenStore:
    """Rotated FPL tokens in the repo Actions variable FPL_TOKENS.

    Needs a PAT with Variables: read/write. The workflow GITHUB_TOKEN cannot write
    Actions variables no matter what `permissions:` grants ("Resource not accessible
    by integration") - that silent 403 killed the automation after one run in Aug 2026.
    """

    VAR = "FPL_TOKENS"

    def __init__(self, repo: str, pat: str, http=requests):
        self.repo, self.pat, self.http = repo, pat, http

    def _url(self, name: str = "") -> str:
        return (f"https://api.github.com/repos/{self.repo}/actions/variables"
                + (f"/{name}" if name else ""))

    def load(self) -> Optional[dict]:
        try:
            r = self.http.get(self._url(self.VAR), headers=_gh_headers(self.pat), timeout=30)
            if r.status_code == 200:
                return json.loads(r.json()["value"])
        except Exception as e:
            log(f"[tokens] could not load saved tokens: {e}")
        return None

    def save(self, tokens: dict) -> bool:
        try:
            body = {"name": self.VAR, "value": json.dumps(tokens)}
            r = self.http.patch(self._url(self.VAR), headers=_gh_headers(self.pat),
                                json=body, timeout=30)
            if r.status_code == 404:
                r = self.http.post(self._url(), headers=_gh_headers(self.pat),
                                   json=body, timeout=30)
            if r.status_code >= 400:
                log(f"[tokens] could not persist tokens: {r.status_code} {r.text[:200]}")
                return False
            return True
        except Exception as e:
            log(f"[tokens] could not persist tokens: {e}")
            return False

    def probe(self) -> bool:
        """Write and delete a throwaway variable to prove the PAT works, without
        spending an FPL token. Run after issuing or renewing the PAT."""
        probe = "FPL_PAT_PROBE"
        body = {"name": probe, "value": "ok"}
        h = _gh_headers(self.pat)
        r = self.http.post(self._url(), headers=h, json=body, timeout=30)
        if r.status_code == 409:  # already there from an earlier probe
            r = self.http.patch(self._url(probe), headers=h, json=body, timeout=30)
        if r.status_code >= 400:
            log(f"[check-pat] FAIL: write returned {r.status_code} {r.text[:200]}")
            log("[check-pat] GH_PAT needs 'Variables: read and write' on this repo.")
            return False
        d = self.http.delete(self._url(probe), headers=h, timeout=30)
        log(f"[check-pat] OK: GH_PAT can write Actions variables (cleanup {d.status_code}).")
        return True


class MemoryTokenStore:
    def __init__(self, tokens: Optional[dict] = None, fail_save: bool = False):
        self.tokens = tokens
        self.fail_save = fail_save
        self.saved: list[dict] = []

    def load(self) -> Optional[dict]:
        return self.tokens

    def save(self, tokens: dict) -> bool:
        if self.fail_save:
            return False
        self.tokens = tokens
        self.saved.append(dict(tokens))
        return True


class NullTokenStore:
    """No persistence available (e.g. running outside GitHub). Loads nothing and
    reports saves as successful so a seed token can still be used once."""

    def load(self) -> Optional[dict]:
        return None

    def save(self, tokens: dict) -> bool:
        return True


# ---------------------------------------------------------------- notifier
def find_open_issue(repo: str, token: str, title: str, http=requests) -> Optional[dict]:
    """Return the first open issue with exactly this title, or None.

    Paginates the open-issue list (not the fuzzy search API) so a recurring
    failure like an auth outage matches its existing issue exactly.
    """
    headers = _gh_headers(token)
    for page in range(1, 4):  # up to 300 open issues
        r = http.get(f"https://api.github.com/repos/{repo}/issues", headers=headers,
                     params={"state": "open", "per_page": 100, "page": page}, timeout=30)
        if r.status_code != 200:
            return None
        items = r.json()
        for it in items:
            if "pull_request" not in it and it.get("title") == title:
                return it
        if len(items) < 100:
            return None
    return None


class GitHubIssueNotifier:
    """Open a GitHub issue so the owner gets an email/notification.

    Deduped: if an open issue with the identical title already exists (e.g. the
    same failure every 15-min run), do nothing instead of piling up copies.
    """

    def __init__(self, repo: str, token: str, http=requests):
        self.repo, self.token, self.http = repo, token, http

    def notify(self, title: str, body: str = "") -> None:
        try:
            existing = find_open_issue(self.repo, self.token, title, http=self.http)
            if existing:
                log(f"[notify] open issue #{existing.get('number')} already has this "
                    f"title; not opening a duplicate.")
                return
            self.http.post(f"https://api.github.com/repos/{self.repo}/issues",
                           headers=_gh_headers(self.token),
                           json={"title": title, "body": body or "\n".join(report_lines)},
                           timeout=30)
        except Exception as e:
            log(f"[notify] failed: {e}")


class LogNotifier:
    """No repo/token configured: the notification is the run log itself."""

    def notify(self, title: str, body: str = "") -> None:
        log(f"[notify] {title}\n{body}")


class ListNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def notify(self, title: str, body: str = "") -> None:
        self.sent.append((title, body))
