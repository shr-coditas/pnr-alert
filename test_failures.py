from datetime import datetime, timedelta

import requests

import pnr_alert as p

DEP = datetime(2026, 11, 20, 18, 30)
T = DEP - timedelta(hours=3, minutes=55)  # chart window, well before the 30-min cutoff


def setup(monkeypatch, tmp_path, fetch):
    sent = []
    monkeypatch.setattr(p, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(p, "fetch_statuses", fetch)
    monkeypatch.setattr(p, "notify", lambda title, msg, tags="": sent.append((title, msg)))
    monkeypatch.setenv("DEPARTURE", DEP.isoformat(sep=" "))
    return sent


def test_classify_names_the_specific_error():
    t, m = p.classify_failure(p.ApiHttpError(429, "You have exceeded the MONTHLY quota for Requests"))
    assert "HTTP 429" in t and "quota" in t.lower() and "MONTHLY quota" in m
    assert "HTTP 401" in p.classify_failure(p.ApiHttpError(401, "bad key"))[0]
    assert "HTTP 404" in p.classify_failure(p.ApiHttpError(404, "nope"))[0]
    assert "HTTP 503" in p.classify_failure(p.ApiHttpError(503, "down"))[0]
    assert "timed out" in p.classify_failure(requests.Timeout("slow"))[0]
    assert "Network" in p.classify_failure(requests.ConnectionError("dns"))[0]
    assert "ValueError" in p.classify_failure(ValueError("odd"))[0]


def test_pnr_is_masked_in_error_text():
    _, m = p.classify_failure(p.ApiHttpError(404, "Endpoint '/pnr/1234567890' does not exist"))
    assert "1234567890" not in m and "<PNR>" in m


def test_failure_push_is_rate_limited_and_recovers(monkeypatch, tmp_path):
    state = {"fail": True}

    def fetch(pnr):
        if state["fail"]:
            raise p.ApiHttpError(429, "quota exceeded")
        return ["GNWL9"]

    sent = setup(monkeypatch, tmp_path, fetch)
    for i in range(7):  # 7 failures in a row, each one a due check
        p.run_if_due("X", now=T + timedelta(minutes=20 * i))
    titles = [t for t, _ in sent]
    # alerts on the 1st and 6th failure only, not all seven
    assert len(titles) == 2 and all("HTTP 429" in t for t in titles)
    assert "failing 6 checks" in sent[1][1]

    state["fail"] = False
    p.run_if_due("X", now=T + timedelta(minutes=20 * 7))
    assert sent[-2][0] == "Tracking started"  # the normal first-success push...
    assert sent[-1][0] == "PNR checks working again"  # ...then the recovery notice
    assert "_failures" in p.load_state() and "X" not in p.load_state()["_failures"]


def test_known_failures_do_not_trigger_the_error_push(monkeypatch, tmp_path):
    def fetch(pnr):
        raise p.PnrNotYetGenerated("x")

    sent = setup(monkeypatch, tmp_path, fetch)
    p.run_if_due("X", now=T)
    assert sent == [] and "_failures" not in p.load_state()
