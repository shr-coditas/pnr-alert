from datetime import datetime, timedelta

import pnr_alert as p

DEP = datetime(2026, 11, 7, 22, 0)


def clean(monkeypatch):
    monkeypatch.delenv("BASE_POLL_MINUTES", raising=False)
    monkeypatch.delenv("CHART_POLL_MINUTES", raising=False)


def test_default_tiers_unchanged_without_base(monkeypatch):
    clean(monkeypatch)
    assert p.schedule(DEP - timedelta(days=3), DEP) == (720, "week")
    assert p.schedule(DEP - timedelta(hours=10), DEP) == (180, "day-of")


def test_base_makes_early_tiers_hourly_but_never_slower(monkeypatch):
    clean(monkeypatch)
    monkeypatch.setenv("BASE_POLL_MINUTES", "60")
    assert p.schedule(DEP - timedelta(days=30), DEP) == (60, "far")
    assert p.schedule(DEP - timedelta(hours=30), DEP) == (60, "2-days")
    assert p.schedule(DEP - timedelta(hours=10), DEP) == (60, "day-of")
    assert p.schedule(DEP - timedelta(hours=5), DEP) == (60, "pre-chart")  # already hourly, not slowed


def test_base_leaves_chart_windows_and_cutoff_alone(monkeypatch):
    clean(monkeypatch)
    monkeypatch.setenv("BASE_POLL_MINUTES", "60")
    monkeypatch.setenv("CHART_POLL_MINUTES", "10")
    assert p.schedule(DEP - timedelta(hours=3), DEP) == (10, "chart1")
    assert p.schedule(DEP - timedelta(hours=1), DEP) == (10, "chart2")
    assert p.schedule(DEP - timedelta(minutes=10), DEP) == (None, "done")


def test_hourly_plan_for_the_real_trip_fits_the_free_quota(monkeypatch):
    clean(monkeypatch)
    monkeypatch.setenv("BASE_POLL_MINUTES", "60")
    monkeypatch.setenv("CHART_POLL_MINUTES", "10")
    now = datetime(2026, 11, 5, 17, 30)  # about when this was set up
    rows = p.estimate_plan(now, DEP)
    early = [r for r in rows if r[1] not in ("chart1", "chart2")]
    assert len(early) <= 70   # early polling cap = MONTHLY_BUDGET 85 - CHART_RESERVE 15
    assert len(rows) <= 80    # whole trip, well under the 85 cap and the plan's 100
