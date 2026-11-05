from datetime import datetime

import pnr_alert as p


def setup(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(p, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(p, "_now", lambda: datetime(2026, 11, 5, 22, 20))
    monkeypatch.setattr(
        p, "notify", lambda title, msg, tags="", priority="high": sent.append((title, msg, priority))
    )
    monkeypatch.delenv("HEARTBEAT", raising=False)
    return sent


def test_no_change_sends_silent_heartbeat(monkeypatch, tmp_path):
    sent = setup(monkeypatch, tmp_path)
    p.check_once("X", ["WL/34"])
    p.check_once("X", ["WL/34"])
    assert sent[0][0] == "Tracking started"
    assert sent[1] == ("Checked: no change", "Checked 10:20 PM: WL 34 (no change)", "min")


def test_change_sends_loud_push_and_no_heartbeat(monkeypatch, tmp_path):
    sent = setup(monkeypatch, tmp_path)
    p.check_once("X", ["WL/35"])
    p.check_once("X", ["WL/34"])
    assert sent[1] == ("Waitlist dropped!", "WL 35 -> WL 34", "high")
    assert len(sent) == 2


def test_heartbeat_lists_each_passenger(monkeypatch, tmp_path):
    sent = setup(monkeypatch, tmp_path)
    p.check_once("X", ["CNF/B3/42", "WL/3"])
    p.check_once("X", ["CNF/B3/42", "WL/3"])
    assert sent[1][1] == "Checked 10:20 PM: P1 CNF/B3/42 | P2 WL 3 (no change)"


def test_heartbeat_can_be_turned_off(monkeypatch, tmp_path):
    sent = setup(monkeypatch, tmp_path)
    monkeypatch.setenv("HEARTBEAT", "0")
    p.check_once("X", ["WL/34"])
    p.check_once("X", ["WL/34"])
    assert len(sent) == 1
