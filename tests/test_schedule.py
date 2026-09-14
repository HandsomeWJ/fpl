from datetime import datetime, timedelta, timezone

from copycat_core import log as logmod
from copycat_core.schedule import minutes_to_price_change, should_run_now
from copycat_core.settings import Settings


def _settings(**env):
    return Settings.from_env({"FPL_ENTRY": "1", **env})


def utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_price_change_is_midnight_uk_in_both_dst_states():
    # BST: midnight London = 23:00Z
    assert round(minutes_to_price_change(utc("2026-08-26T12:00:00"))) == 660
    # GMT: midnight London = 00:00Z
    assert round(minutes_to_price_change(utc("2026-12-10T12:00:00"))) == 720


def test_human_dispatch_always_runs_without_reading_the_deadline():
    def boom():
        raise AssertionError("deadline must not be read for a manual run")
    assert should_run_now(_settings(), now=utc("2026-08-26T12:00:00"), deadline_fn=boom) is True


def test_clock_runs_in_price_window():
    s = _settings(SCHEDULED="1")
    assert should_run_now(s, now=utc("2026-08-26T22:00:00"), deadline_fn=lambda: None) is True
    assert any("min to the price change; running" in l for l in logmod.report_lines)


def test_clock_runs_in_deadline_window_and_skips_outside():
    s = _settings(GITHUB_EVENT_NAME="schedule")
    now = utc("2026-08-26T12:00:00")
    assert should_run_now(s, now=now, deadline_fn=lambda: now + timedelta(hours=3)) is True
    logmod.reset_report()
    assert should_run_now(s, now=now, deadline_fn=lambda: now + timedelta(hours=75)) is False
    assert logmod.report_lines[-1].endswith("Skipping.")


def test_gate_fails_open_when_deadline_unreadable():
    def boom():
        raise RuntimeError("api down")
    assert should_run_now(_settings(SCHEDULED="1"), now=utc("2026-08-26T12:00:00"),
                          deadline_fn=boom) is True
