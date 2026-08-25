#!/usr/bin/env python3
"""Local mocked tests for copycat.py — run with: python3 test_local.py

Covers notify() issue dedup: an open issue with the identical title must
suppress a new one; anything else (no match, listing failure, PRs, closed
issues implicitly) still opens an issue so failures stay loud.
"""

import os
import sys

import copycat


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []
        self.text = ""

    def json(self):
        return self._json


class FakeRequests:
    """Stands in for the requests module inside copycat."""

    def __init__(self, list_pages=None, list_status=200):
        # list_pages: list of page payloads returned by successive GETs
        self.list_pages = list_pages or [[]]
        self.list_status = list_status
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        page = kwargs.get("params", {}).get("page", 1)
        if self.list_status != 200:
            return FakeResponse(self.list_status)
        idx = page - 1
        data = self.list_pages[idx] if idx < len(self.list_pages) else []
        return FakeResponse(200, data)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return FakeResponse(201, {"number": 999})


def run_notify(fake, title="FPL Copycat: run failed - FPL auth failed (403)",
               env=True):
    old_requests = copycat.requests
    old_env = {k: os.environ.get(k) for k in ("GITHUB_REPOSITORY", "GITHUB_TOKEN")}
    try:
        copycat.requests = fake
        if env:
            os.environ["GITHUB_REPOSITORY"] = "HandsomeWJ/fpl"
            os.environ["GITHUB_TOKEN"] = "test-token"
        else:
            os.environ.pop("GITHUB_REPOSITORY", None)
            os.environ.pop("GITHUB_TOKEN", None)
        copycat.notify(title, "body")
    finally:
        copycat.requests = old_requests
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


TITLE = "FPL Copycat: run failed - FPL auth failed (403)"
failures = []


def check(name, cond):
    status = "ok" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        failures.append(name)


# 1. identical open issue exists -> no new issue
fake = FakeRequests(list_pages=[[{"number": 1, "title": TITLE}]])
run_notify(fake)
check("duplicate title suppresses new issue", len(fake.post_calls) == 0)

# 2. no matching issue -> issue is opened with the right title
fake = FakeRequests(list_pages=[[{"number": 1, "title": "something else"}]])
run_notify(fake)
check("no duplicate -> issue opened",
      len(fake.post_calls) == 1
      and fake.post_calls[0][1]["json"]["title"] == TITLE)

# 3. duplicate found on page 2 (pagination past 100 issues)
page1 = [{"number": i, "title": f"other {i}"} for i in range(100)]
fake = FakeRequests(list_pages=[page1, [{"number": 190, "title": TITLE}]])
run_notify(fake)
check("duplicate on page 2 suppresses new issue",
      len(fake.post_calls) == 0 and len(fake.get_calls) == 2)

# 4. listing fails -> still opens the issue (failures stay loud)
fake = FakeRequests(list_status=500)
run_notify(fake)
check("listing failure still opens issue", len(fake.post_calls) == 1)

# 5. a PR with the same title does not count as a duplicate
fake = FakeRequests(list_pages=[[{"number": 5, "title": TITLE,
                                  "pull_request": {"url": "x"}}]])
run_notify(fake)
check("same-title PR ignored -> issue opened", len(fake.post_calls) == 1)

# 6. no repo/token -> logs only, no HTTP calls at all
fake = FakeRequests()
run_notify(fake, env=False)
check("no env -> no HTTP", len(fake.get_calls) == 0 and len(fake.post_calls) == 0)

print()
if failures:
    print(f"{len(failures)} test(s) failed: {failures}")
    sys.exit(1)
print("All tests passed.")
