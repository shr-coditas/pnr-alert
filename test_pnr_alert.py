import pnr_alert as p


def test_parse():
    assert p.parse_status("GNWL17") == ("WL", 17)
    assert p.parse_status("WL/17") == ("WL", 17)
    assert p.parse_status("RAC 3") == ("RAC", 3)
    assert p.parse_status("CNF/B3/42") == ("CNF", None)


def test_compare():
    assert p.compare("GNWL23", "GNWL23") is None
    assert p.compare("GNWL23", "GNWL17")[0] == "Waitlist dropped!"
    assert p.compare("GNWL17", "GNWL20")[0] == "Waitlist went up"
    assert p.compare("GNWL5", "RAC 2")[0] == "Moved to RAC!"
    assert p.compare("RAC 2", "CNF/B3/42")[0] == "PNR CONFIRMED!"


def test_find_statuses():
    data = {"data": {"passengers": [{"currentStatus": "GNWL9"}, {"CurrentStatus": "GNWL10"}]}}
    assert p.find_current_statuses(data) == ["GNWL9", "GNWL10"]


def test_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setenv("MONTHLY_BUDGET", "2")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    s = {}
    assert p.spend_budget(s) and p.spend_budget(s)
    assert not p.spend_budget(s)  # 3rd call blocked
    assert not p.spend_budget(s)  # stays blocked, warns only once
    assert s["_budget"]["used"] == 2


from datetime import datetime, timedelta

DEP = datetime(2026, 11, 20, 18, 30)


def test_schedule_tiers():
    assert p.schedule(DEP - timedelta(days=30), DEP) == (1440, "far")
    assert p.schedule(DEP - timedelta(days=3), DEP) == (720, "week")
    assert p.schedule(DEP - timedelta(hours=3), DEP) == (20, "chart1")
    assert p.schedule(DEP - timedelta(hours=1), DEP) == (15, "chart2")
    assert p.schedule(DEP - timedelta(minutes=10), DEP)[1] == "done"
    assert p.schedule(DEP + timedelta(hours=1), DEP)[1] == "done"


def test_plan_fits_free_quota():
    rows = p.estimate_plan(DEP - timedelta(days=30), DEP)
    assert 40 < len(rows) < 90


def _patch(monkeypatch, tmp_path, calls):
    monkeypatch.setattr(p, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(p, "fetch_statuses", lambda pnr: calls.append(1) or ["GNWL9"])
    monkeypatch.setenv("DEPARTURE", DEP.isoformat(sep=" "))
    monkeypatch.delenv("NTFY_TOPIC", raising=False)


def test_run_if_due_throttles(monkeypatch, tmp_path):
    calls = []
    _patch(monkeypatch, tmp_path, calls)
    t = DEP - timedelta(hours=3)  # chart1: every 20 min
    assert p.run_if_due("X", now=t)
    assert not p.run_if_due("X", now=t + timedelta(minutes=5))   # too soon
    assert p.run_if_due("X", now=t + timedelta(minutes=21))      # due again
    assert len(calls) == 2


def test_reserve_kept_for_chart_window(monkeypatch, tmp_path):
    calls = []
    _patch(monkeypatch, tmp_path, calls)
    monkeypatch.setenv("MONTHLY_BUDGET", "5")
    monkeypatch.setenv("CHART_RESERVE", "3")
    t = DEP - timedelta(days=10)
    for i in range(4):  # early calls may use only 5-3 = 2
        p.run_if_due("X", now=t + timedelta(days=i))
    assert len(calls) == 2
    assert p.run_if_due("X", now=DEP - timedelta(hours=3))  # chart window may use the reserve


def test_no_polling_after_cutoff(monkeypatch, tmp_path):
    calls = []
    _patch(monkeypatch, tmp_path, calls)
    assert not p.run_if_due("X", now=DEP - timedelta(minutes=10))
    assert calls == []


def test_find_statuses_prefers_details():
    # shape of the real irctc-indian-railway-pnr-status response
    data = {"success": True, "data": {"passengerList": [
        {"currentStatus": "WL", "currentStatusDetails": "WL/35"},
        {"currentStatus": "RAC", "currentStatusDetails": "RAC/4"}]}}
    assert p.find_current_statuses(data) == ["WL/35", "RAC/4"]
    assert p.parse_status("WL/35") == ("WL", 35)


def test_stops_when_confirmed(monkeypatch, tmp_path):
    calls = []
    _patch(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(p, "fetch_statuses", lambda pnr: calls.append(1) or ["CNF/B3/42"])
    t = DEP - timedelta(hours=3)
    assert p.run_if_due("X", now=t)
    assert p.load_state()["_finished"]["X"] == "confirmed"
    assert p.is_finished("X")
    assert not p.run_if_due("X", now=t + timedelta(hours=1))  # no more API calls
    assert len(calls) == 1


def test_stops_when_cancelled(monkeypatch, tmp_path):
    calls = []
    _patch(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(p, "fetch_statuses", lambda pnr: ["CAN/CAN"])
    p.run_if_due("X", now=DEP - timedelta(hours=3))
    assert p.load_state()["_finished"]["X"] == "cancelled"


def test_keeps_going_while_waitlisted_or_partly_confirmed(monkeypatch, tmp_path):
    calls = []
    _patch(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(p, "fetch_statuses", lambda pnr: ["CNF/B3/42", "WL/3"])
    p.run_if_due("X", now=DEP - timedelta(hours=3))
    assert "_finished" not in p.load_state()


def test_finished_after_chart_window(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, [])
    monkeypatch.setattr(p, "_now", lambda: DEP - timedelta(minutes=10))
    assert p.is_finished("X")


def test_grace_for_late_cron(monkeypatch, tmp_path):
    calls = []
    _patch(monkeypatch, tmp_path, calls)
    t = DEP - timedelta(hours=3)  # chart1 = 20 min interval
    p.run_if_due("X", now=t)
    assert p.run_if_due("X", now=t + timedelta(minutes=18))  # cron fired 2 min early: still counts
    assert len(calls) == 2


def _raiser(exc):
    def f(pnr):
        raise exc("body")
    return f


def test_flushed_pnr_stops_polling(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, [])
    monkeypatch.setattr(p, "fetch_statuses", _raiser(p.PnrFlushed))
    p.run_if_due("X", now=DEP - timedelta(hours=3))
    assert p.load_state()["_finished"]["X"] == "flushed"
    assert p.is_finished("X")


def test_invalid_pnr_stops_polling(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, [])
    monkeypatch.setattr(p, "fetch_statuses", _raiser(p.PnrInvalid))
    p.run_if_due("X", now=DEP - timedelta(hours=3))
    assert p.load_state()["_finished"]["X"] == "invalid"


def test_not_yet_generated_keeps_polling(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, [])
    monkeypatch.setattr(p, "fetch_statuses", _raiser(p.PnrNotYetGenerated))
    p.run_if_due("X", now=DEP - timedelta(hours=3))
    assert "_finished" not in p.load_state()


def test_failure_text_classification():
    import pytest
    for text, exc in [('{"success":false,"message":"FLUSHED PNR"}', p.PnrFlushed),
                      ("Invalid PNR Number", p.PnrInvalid),
                      ("PNR NOT YET GENERATED", p.PnrNotYetGenerated)]:
        with pytest.raises(exc):
            p.raise_if_known_failure(text)
    p.raise_if_known_failure('{"success": true, "data": {"currentStatusDetails": "WL/35"}}')  # no raise
