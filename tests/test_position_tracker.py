"""
Unit tests for position_tracker.py.

Uses a real SQLite file per test (tmp_path fixture) to avoid :memory: isolation
issues (each sqlite3.connect() call to :memory: creates a separate blank DB).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.database import Database
from src.position_tracker import PositionTracker, _hour_key

_ET = ZoneInfo("America/New_York")


def _et(hour: int, minute: int = 30) -> datetime:
    return datetime(2026, 5, 21, hour, minute, 0, tzinfo=_ET)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def tracker(db):
    return PositionTracker(db)


# ── hour_key helper ───────────────────────────────────────────────────────────

def test_hour_key_format():
    dt = _et(10, 35)
    assert _hour_key(dt) == "2026-05-21T10"


def test_hour_key_changes_each_hour():
    assert _hour_key(_et(10)) != _hour_key(_et(11))


# ── Empty state ───────────────────────────────────────────────────────────────

def test_new_tracker_zero_lines(tracker):
    assert tracker.lines_taken_this_hour(_et(10)) == 0


def test_new_tracker_zero_capital(tracker):
    assert tracker.capital_deployed_this_hour(_et(10)) == 0.0


def test_new_tracker_no_open_positions(tracker):
    assert tracker.open_positions() == []


# ── Line lifecycle ────────────────────────────────────────────────────────────

def test_open_new_line_returns_uuid(tracker):
    line_id = tracker.open_new_line(_et(10), "KXBTCD-TEST", "yes", 8530)
    assert isinstance(line_id, str)
    assert len(line_id) == 36  # UUID format


def test_open_new_line_increments_count(tracker):
    tracker.open_new_line(_et(10), "KXBTCD-TEST", "yes", 8530)
    assert tracker.lines_taken_this_hour(_et(10)) == 1


def test_two_lines_count_is_two(tracker):
    tracker.open_new_line(_et(10), "KXBTCD-A", "yes", 8530)
    tracker.open_new_line(_et(10), "KXBTCD-B", "yes", 8600)
    assert tracker.lines_taken_this_hour(_et(10)) == 2


def test_lines_from_different_hour_not_counted(tracker):
    tracker.open_new_line(_et(9), "KXBTCD-OLD", "yes", 8530)
    assert tracker.lines_taken_this_hour(_et(10)) == 0


def test_lines_from_same_hour_different_minute_counted(tracker):
    tracker.open_new_line(_et(10, 25), "KXBTCD-A", "yes", 8530)
    tracker.open_new_line(_et(10, 45), "KXBTCD-B", "yes", 8600)
    # Both have hour_key "2026-05-21T10"
    assert tracker.lines_taken_this_hour(_et(10, 55)) == 2


# ── Capital tracking ──────────────────────────────────────────────────────────

def test_capital_zero_before_fills(tracker):
    tracker.open_new_line(_et(10), "KXBTCD-TEST", "yes", 8530)
    assert tracker.capital_deployed_this_hour(_et(10)) == 0.0


def test_add_fill_updates_capital(tracker):
    line_id = tracker.open_new_line(_et(10), "KXBTCD-TEST", "yes", 8530)
    tracker.add_fill_to_line(line_id, 500.0)
    assert tracker.capital_deployed_this_hour(_et(10)) == pytest.approx(500.0)


def test_multiple_fills_accumulate(tracker):
    line_id = tracker.open_new_line(_et(10), "KXBTCD-TEST", "yes", 8530)
    tracker.add_fill_to_line(line_id, 300.0)
    tracker.add_fill_to_line(line_id, 400.0)
    assert tracker.capital_deployed_this_hour(_et(10)) == pytest.approx(700.0)


def test_capital_from_two_lines_sums(tracker):
    line_a = tracker.open_new_line(_et(10), "KXBTCD-A", "yes", 8530)
    line_b = tracker.open_new_line(_et(10), "KXBTCD-B", "no", 8700)
    tracker.add_fill_to_line(line_a, 800.0)
    tracker.add_fill_to_line(line_b, 600.0)
    assert tracker.capital_deployed_this_hour(_et(10)) == pytest.approx(1400.0)


def test_fills_from_different_hour_not_counted(tracker):
    line_id = tracker.open_new_line(_et(9), "KXBTCD-OLD", "yes", 8530)
    tracker.add_fill_to_line(line_id, 900.0)
    assert tracker.capital_deployed_this_hour(_et(10)) == 0.0


# ── Open positions ────────────────────────────────────────────────────────────

def test_open_positions_shows_unsettled(tracker):
    tracker.open_new_line(_et(10), "KXBTCD-TEST", "yes", 8530)
    positions = tracker.open_positions()
    assert len(positions) == 1


def test_settled_line_not_in_open_positions(tracker):
    line_id = tracker.open_new_line(_et(10), "KXBTCD-TEST", "yes", 8530)
    tracker.add_fill_to_line(line_id, 990.0)
    tracker.settle_line(line_id, "win", 10.0)
    assert tracker.open_positions() == []


def test_open_positions_mixed_settled_and_not(tracker):
    line_a = tracker.open_new_line(_et(10), "KXBTCD-A", "yes", 8530)
    line_b = tracker.open_new_line(_et(10), "KXBTCD-B", "yes", 8600)
    tracker.settle_line(line_a, "win", 10.0)
    positions = tracker.open_positions()
    assert len(positions) == 1
    assert positions[0]["id"] == line_b


# ── Positions settling this hour ──────────────────────────────────────────────

def test_positions_settling_this_hour(tracker):
    tracker.open_new_line(_et(10), "KXBTCD-A", "yes", 8530)
    rows = tracker.positions_settling_this_hour(_et(10))
    assert len(rows) == 1


def test_positions_settling_different_hour_excluded(tracker):
    tracker.open_new_line(_et(9), "KXBTCD-OLD", "yes", 8530)
    rows = tracker.positions_settling_this_hour(_et(10))
    assert rows == []


# ── Settle ────────────────────────────────────────────────────────────────────

def test_settle_records_outcome(tracker, db):
    line_id = tracker.open_new_line(_et(10), "KXBTCD-TEST", "yes", 8530)
    tracker.settle_line(line_id, "win", 9.90)
    rows = db.get_lines_this_hour("2026-05-21T10")
    assert rows[0]["outcome"] == "win"
    assert rows[0]["final_pnl_usd"] == pytest.approx(9.90)


# ── reset_hourly_counters is a no-op ─────────────────────────────────────────

def test_reset_hourly_counters_noop(tracker):
    tracker.open_new_line(_et(10), "KXBTCD-TEST", "yes", 8530)
    tracker.reset_hourly_counters()
    # Counts should be unchanged since they're DB-derived
    assert tracker.lines_taken_this_hour(_et(10)) == 1
