"""GitHub issue notifier dedup (ported from the original test_local.py): an open issue
with the identical title suppresses a new one; anything else still opens an issue so
failures stay loud."""

from copycat_core import log as logmod
from copycat_core.ports import GitHubIssueNotifier, LogNotifier
from tests.conftest import FakeResponse

TITLE = "FPL Copycat: run failed - FPL auth failed (403)"


class FakeRequests:
    def __init__(self, list_pages=None, list_status=200):
        self.list_pages = list_pages or [[]]
        self.list_status = list_status
        self.get_calls, self.post_calls = [], []

    def get(self, url, **kw):
        self.get_calls.append((url, kw))
        if self.list_status != 200:
            return FakeResponse(self.list_status, [])
        page = kw.get("params", {}).get("page", 1) - 1
        return FakeResponse(200, self.list_pages[page] if page < len(self.list_pages) else [])

    def post(self, url, **kw):
        self.post_calls.append((url, kw))
        return FakeResponse(201, {"number": 999})


def _notify(fake):
    GitHubIssueNotifier("HandsomeWJ/fpl", "test-token", http=fake).notify(TITLE, "body")
    return fake


def test_duplicate_title_suppresses_new_issue():
    fake = _notify(FakeRequests([[{"number": 1, "title": TITLE}]]))
    assert fake.post_calls == []
    assert "[notify] open issue #1 already has this title; not opening a duplicate." in logmod.report_lines


def test_no_duplicate_opens_issue_with_title():
    fake = _notify(FakeRequests([[{"number": 1, "title": "something else"}]]))
    assert len(fake.post_calls) == 1 and fake.post_calls[0][1]["json"]["title"] == TITLE


def test_duplicate_on_page_two_is_found():
    page1 = [{"number": i, "title": f"other {i}"} for i in range(100)]
    fake = _notify(FakeRequests([page1, [{"number": 190, "title": TITLE}]]))
    assert fake.post_calls == [] and len(fake.get_calls) == 2


def test_listing_failure_still_opens_issue():
    fake = _notify(FakeRequests(list_status=500))
    assert len(fake.post_calls) == 1


def test_same_title_pull_request_is_not_a_duplicate():
    fake = _notify(FakeRequests([[{"number": 5, "title": TITLE, "pull_request": {"url": "x"}}]]))
    assert len(fake.post_calls) == 1


def test_log_notifier_only_logs():
    LogNotifier().notify(TITLE, "body")
    assert logmod.report_lines == [f"[notify] {TITLE}\nbody"]
