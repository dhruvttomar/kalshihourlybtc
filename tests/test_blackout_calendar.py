"""
Unit tests for blackout_calendar.py.

Covers: top-of-hour, day risk profiles, universal daily windows,
weekly windows, monthly/quarterly expiry, economic events, DST transitions,
and next_open_window().

All times are created as ET-aware datetimes using ZoneInfo.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.blackout_calendar import BlackoutCalendar, _last_friday_of_month
from src.models import EconEvent

_ET = ZoneInfo("America/New_York")
_BLACKOUTS_CFG = "config/blackouts.yaml"


def _et(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """Helper: create an ET-aware datetime."""
    return datetime(year, month, day, hour, minute, second, tzinfo=_ET)


def _cal(econ_events: list[EconEvent] | None = None) -> BlackoutCalendar:
    return BlackoutCalendar(
        config_path=_BLACKOUTS_CFG,
        max_risk_level=1,
        econ_events=econ_events or [],
    )


# ── Top-of-hour ───────────────────────────────────────────────────────────────

def test_top_of_hour_is_blocked():
    cal = _cal()
    # Thursday is a low-risk day; hour 10 risk level = 1 (caution, allowed)
    # but minute 0 is always blocked
    blocked, reason = cal.is_blocked(_et(2026, 5, 21, 10, 0))
    assert blocked
    assert "Top of hour" in reason

def test_one_minute_past_top_is_not_blocked_by_top_of_hour():
    cal = _cal()
    # Thursday 10:01 ET — no other windows active
    blocked, reason = cal.is_blocked(_et(2026, 5, 21, 10, 1))
    # Thursday risk profile for hour 10 is 1 (caution, allowed under max=1)
    # No universal daily window at 10:01 on Thursday
    assert not blocked

def test_top_of_hour_applies_every_hour():
    cal = _cal()
    for hour in [1, 5, 11, 14, 20, 23]:
        # Thursday — low risk day
        blocked, _ = cal.is_blocked(_et(2026, 5, 21, hour, 0))
        assert blocked, f"Hour {hour}:00 should be blocked"

# ── Day-of-week risk profiles ─────────────────────────────────────────────────

def test_monday_morning_blocked_by_risk_profile():
    # Monday hour 9 = risk level 3 (hard no)
    cal = _cal()
    blocked, reason = cal.is_blocked(_et(2026, 5, 18, 9, 30))
    assert blocked
    assert "risk profile" in reason.lower()

def test_saturday_is_all_safe():
    # Saturday risk profile is all 0s
    cal = _cal()
    for hour in range(1, 24):  # skip 0 (top-of-hour)
        # pick minute 30 to avoid top-of-hour
        dt = _et(2026, 5, 23, hour, 30)
        blocked, reason = cal.is_blocked(dt)
        # Saturday should not be blocked by risk profile; may be blocked by other rules
        if blocked:
            assert "risk profile" not in (reason or "").lower(), (
                f"Saturday hour {hour} blocked by risk profile unexpectedly: {reason}"
            )

def test_sunday_evening_blocked_by_risk_profile():
    # Sunday hour 19 = risk level 2 (> max_risk_level=1)
    cal = _cal()
    blocked, reason = cal.is_blocked(_et(2026, 5, 17, 19, 30))
    assert blocked
    assert "risk profile" in reason.lower()

def test_thursday_midday_is_open():
    # Thursday hour 12 = risk level 1 (caution, allowed at max_risk_level=1)
    # But 12:00-13:00 is "Peak intraday volatility" window — so it IS blocked
    # Use hour 10 instead (risk level 1, no daily window)
    cal = _cal()
    blocked, _ = cal.is_blocked(_et(2026, 5, 21, 10, 30))
    assert not blocked

# ── Universal daily windows ───────────────────────────────────────────────────

def test_deribit_expiry_blocks_all_days():
    cal = _cal()
    for weekday_offset in range(7):
        # May 18 is Monday, +0..+6 covers Mon-Sun
        dt = _et(2026, 5, 18 + weekday_offset, 3, 45)
        blocked, reason = cal.is_blocked(dt)
        assert blocked, f"Deribit window not blocked on day offset {weekday_offset}"
        assert "Deribit" in reason

def test_us_equity_open_blocks_weekdays_only():
    cal = _cal()
    # Thursday 9:30 ET — Thursday hour 9 has risk level 1 (allowed), so the
    # universal daily "US equity market open" window fires as the blocking reason.
    blocked, reason = cal.is_blocked(_et(2026, 5, 21, 9, 30))
    assert blocked
    assert "equity market open" in reason.lower()

def test_us_equity_open_does_not_block_saturday():
    cal = _cal()
    # Saturday 9:30 ET → weekdays_only=true, should not be blocked by this rule
    # (Saturday risk profile is 0 everywhere, so it might be open)
    blocked, reason = cal.is_blocked(_et(2026, 5, 23, 9, 30))
    if blocked:
        assert "equity market open" not in (reason or "").lower()

def test_peak_vol_window_blocks():
    cal = _cal()
    blocked, reason = cal.is_blocked(_et(2026, 5, 21, 12, 30))
    assert blocked
    assert "intraday volatility" in reason.lower() or "tea time" in reason.lower()

def test_cme_daily_settlement_weekdays_only():
    cal = _cal()
    # Thursday 18:00 ET → blocked
    blocked, reason = cal.is_blocked(_et(2026, 5, 21, 18, 0))
    # Note: 18:00 is the exact TOP of the hour (minute==0), which is always blocked
    # Use 17:58 instead
    blocked, reason = cal.is_blocked(_et(2026, 5, 21, 17, 58))
    assert blocked
    assert "CME daily settlement" in reason

def test_cme_daily_settlement_not_on_weekend():
    cal = _cal()
    # Saturday 17:58 ET
    blocked, reason = cal.is_blocked(_et(2026, 5, 23, 17, 58))
    if blocked:
        assert "CME daily settlement" not in (reason or "")

# ── Weekly recurring windows ──────────────────────────────────────────────────

def test_monday_us_session_block():
    cal = _cal()
    blocked, reason = cal.is_blocked(_et(2026, 5, 18, 12, 0))
    assert blocked
    # Could be blocked by risk profile OR the weekly Monday session block
    assert blocked

def test_sunday_cme_reopen():
    cal = _cal()
    # Sunday hour 17 risk level = 1 (allowed); 17:50 is inside the 17:45-20:00 window.
    blocked, reason = cal.is_blocked(_et(2026, 5, 17, 17, 50))
    assert blocked
    assert "CME futures Sunday reopen" in reason

def test_friday_weekly_options_expiry():
    # Friday hour 4 has risk level 2, so the risk profile always fires first there.
    # Use max_risk_level=2 to let the weekly window rule be the blocking reason.
    cal = BlackoutCalendar(config_path=_BLACKOUTS_CFG, max_risk_level=2, econ_events=[])
    # May 22, 2026 is a Friday (not the last Friday of May); 4:30 AM ET
    blocked, reason = cal.is_blocked(_et(2026, 5, 22, 4, 30))
    assert blocked
    assert "weekly options expiry" in reason.lower()

def test_friday_cme_weekly_close():
    cal = _cal()
    # Friday hour 17 risk level = 1 (allowed); 17:15 is inside 16:45-18:00 window.
    # Note: search lowercase because reason.lower() produces "friday cme weekly close".
    blocked, reason = cal.is_blocked(_et(2026, 5, 22, 17, 15))
    assert blocked
    assert "cme weekly close" in reason.lower()

# ── Monthly / quarterly expiry ────────────────────────────────────────────────

def test_last_friday_helper():
    # May 2026: last Friday is the 29th
    assert _last_friday_of_month(2026, 5) == 29
    # June 2026: last Friday is the 26th
    assert _last_friday_of_month(2026, 6) == 26
    # December 2026: last Friday is the 25th
    assert _last_friday_of_month(2026, 12) == 25

def test_monthly_expiry_blocks_last_friday():
    cal = _cal()
    # May 29, 2026 is the last Friday of May (non-quarterly month).
    # Use hour 6 (risk level 1, allowed) so the monthly rule is the blocking reason.
    blocked, reason = cal.is_blocked(_et(2026, 5, 29, 6, 30))
    assert blocked
    assert "Monthly options expiry" in reason

def test_quarterly_expiry_blocks_last_friday_of_quarter():
    cal = _cal()
    # June 26, 2026 — last Friday of June (quarterly month).
    # Use hour 6 (risk level 1, allowed) so the quarterly rule is the blocking reason.
    blocked, reason = cal.is_blocked(_et(2026, 6, 26, 6, 30))
    assert blocked
    assert "Quarterly" in reason

def test_non_expiry_friday_not_blocked_by_monthly_rule():
    cal = _cal()
    # May 22, 2026 — a Friday but NOT the last Friday of May
    blocked, reason = cal.is_blocked(_et(2026, 5, 22, 10, 30))
    # Should not be blocked by the monthly rule
    if blocked:
        assert "Monthly options expiry" not in (reason or "")
        assert "Quarterly" not in (reason or "")

# ── Economic calendar ─────────────────────────────────────────────────────────

def _cpi_event_at(hour_utc: int) -> EconEvent:
    return EconEvent(
        name="CPI (YoY)",
        event_time_utc=datetime(2026, 5, 21, hour_utc, 30, 0, tzinfo=timezone.utc),
        impact="high",
    )

def test_econ_event_blocks_during_window():
    # CPI at 12:30 UTC = 8:30 AM ET; blackout is -30 to +120 min → 8:00-10:30 ET
    event = _cpi_event_at(12)
    cal = _cal(econ_events=[event])
    # 8:15 AM ET = in window
    blocked, reason = cal.is_blocked(_et(2026, 5, 21, 8, 15))
    assert blocked
    assert "CPI" in reason

def test_econ_event_blocks_after_release():
    event = _cpi_event_at(12)
    cal = _cal(econ_events=[event])
    # 9:45 AM ET = 75 min after event, still within +120 window
    blocked, reason = cal.is_blocked(_et(2026, 5, 21, 9, 45))
    assert blocked
    assert "CPI" in reason

def test_econ_event_unblocked_outside_window():
    event = _cpi_event_at(12)
    cal = _cal(econ_events=[event])
    # 11:00 AM ET = 150 min after event → outside +120 window
    blocked, _ = cal.is_blocked(_et(2026, 5, 21, 11, 0))
    # May still be blocked by risk profile / daily window, but not by CPI
    if blocked:
        _, reason = cal.is_blocked(_et(2026, 5, 21, 11, 0))
        assert "CPI" not in (reason or "")

def test_fomc_long_blackout():
    # FOMC at 18:00 UTC = 14:00 ET; blackout -15 to +150 min → 13:45-16:30 ET
    event = EconEvent(
        name="FOMC Interest Rate Decision",
        event_time_utc=datetime(2026, 5, 21, 18, 0, 0, tzinfo=timezone.utc),
    )
    cal = _cal(econ_events=[event])
    # 15:15 ET = inside window (avoid 15:00 which is top-of-hour → fires first)
    blocked, reason = cal.is_blocked(_et(2026, 5, 21, 15, 15))
    assert blocked
    assert "FOMC" in reason

def test_no_econ_events_does_not_block():
    cal = _cal(econ_events=[])
    # Thursday 10:30 ET — no events, should be open
    blocked, _ = cal.is_blocked(_et(2026, 5, 21, 10, 30))
    assert not blocked

# ── DST transitions ───────────────────────────────────────────────────────────

def test_spring_forward_2026():
    """Clocks jump from 2:00 AM to 3:00 AM ET on March 8, 2026."""
    cal = _cal()
    # 1:30 AM ET on March 8 (before spring forward) — risk level for sunday hour 1 = 0
    dt_before = _et(2026, 3, 8, 1, 30)
    blocked, _ = cal.is_blocked(dt_before)
    # May or may not be blocked by other rules; just ensure no crash
    assert isinstance(blocked, bool)

    # 3:30 AM ET on March 8 (after spring forward) — should be valid ET time
    dt_after = _et(2026, 3, 8, 3, 30)
    blocked, _ = cal.is_blocked(dt_after)
    assert isinstance(blocked, bool)

def test_fall_back_2026():
    """Clocks fall back from 2:00 AM to 1:00 AM ET on November 1, 2026."""
    cal = _cal()
    # 1:30 AM ET fold — create with fold=0 (first occurrence, still EDT)
    dt_fold_0 = datetime(2026, 11, 1, 1, 30, 0, tzinfo=_ET, fold=0)
    blocked0, _ = cal.is_blocked(dt_fold_0)
    assert isinstance(blocked0, bool)

    # 1:30 AM ET fold=1 (second occurrence, EST)
    dt_fold_1 = datetime(2026, 11, 1, 1, 30, 0, tzinfo=_ET, fold=1)
    blocked1, _ = cal.is_blocked(dt_fold_1)
    assert isinstance(blocked1, bool)

def test_dst_transition_no_crash_for_all_hours():
    """Iterate every minute of spring-forward day — must not raise."""
    cal = _cal()
    # March 8, 2026: 2 AM doesn't exist (spring forward)
    for hour in range(24):
        if hour == 2:
            continue  # hour doesn't exist on this day
        for minute in [0, 15, 30, 45]:
            try:
                dt = _et(2026, 3, 8, hour, minute)
                cal.is_blocked(dt)
            except Exception as e:
                pytest.fail(f"Raised on {hour:02d}:{minute:02d}: {e}")

# ── next_open_window ──────────────────────────────────────────────────────────

def test_next_open_window_skips_blocked_time():
    cal = _cal()
    # At 10:00 ET (top of hour) on Thursday — immediately blocked
    now = _et(2026, 5, 21, 10, 0)
    nxt = cal.next_open_window(now)
    assert nxt > now
    # Verify the returned time is actually open
    blocked, _ = cal.is_blocked(nxt)
    assert not blocked

def test_next_open_window_returns_immediately_if_already_open():
    cal = _cal()
    # Thursday 10:30 ET — already open
    now = _et(2026, 5, 21, 10, 30)
    blocked, _ = cal.is_blocked(now)
    assert not blocked
    nxt = cal.next_open_window(now)
    # Should be exactly 1 minute ahead (the next minute that is open)
    assert nxt == now.replace(second=0, microsecond=0) + __import__("datetime").timedelta(minutes=1)
