"""PNR waitlist alert: pings your phone (via ntfy.sh) when your waitlist number drops.

Usage:
    python pnr_alert.py --once            # check one time (use with Task Scheduler / cron)
    python pnr_alert.py --loop            # keep checking every POLL_MINUTES
    python pnr_alert.py --test-notify     # send a test push to your phone
    python pnr_alert.py --demo            # simulate a waitlist dropping (no API needed)

Config comes from environment variables or a .env file (see .env.example).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

STATE_FILE = Path(__file__).with_name("state.json")


# ----------------------------------------------------------------- config
def load_env() -> None:
    """Tiny .env loader (avoids an extra dependency)."""
    # Windows tools often add a hidden BOM (﻿) to files and piped text; strip it everywhere.
    for k, v in list(os.environ.items()):
        if "﻿" in v or v != v.strip():
            os.environ[k] = v.replace("﻿", "").strip()
    env = Path(__file__).with_name(".env")
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ------------------------------------------------------- status parsing
def parse_status(raw: str) -> tuple[str, int | None]:
    """'GNWL17' / 'WL/17' / 'RLWL 5' -> ('WL', 17); 'RAC 3' -> ('RAC', 3); 'CNF/B3/42' -> ('CNF', None)."""
    s = (raw or "").upper().replace(" ", "")
    if s.startswith(("CNF", "CONFIRM")):
        return "CNF", None
    m = re.search(r"RAC/?(\d+)", s)
    if m:
        return "RAC", int(m.group(1))
    m = re.search(r"WL/?(\d+)", s)
    if m:
        return "WL", int(m.group(1))
    if s.startswith(("CAN", "FLUSH", "REGRET")):
        return "CANCELLED", None
    return "OTHER", None


def describe(raw: str) -> str:
    kind, n = parse_status(raw)
    return f"{kind} {n}" if n is not None else (raw or "unknown")


def compare(old: str, new: str) -> tuple[str, str, str] | None:
    """Return (title, message, ntfy_tags) if the change deserves a notification, else None."""
    if old == new:
        return None
    ok, on = parse_status(old)
    nk, nn = parse_status(new)

    if nk == "CNF":
        return ("PNR CONFIRMED!", f"Confirmed! {old} -> {new}", "tada,train")
    if nk == "RAC" and ok == "WL":
        return ("Moved to RAC!", f"{describe(old)} -> {describe(new)}", "tada")
    if nk == ok and nn is not None and on is not None:
        if nn < on:
            return ("Waitlist dropped!", f"{describe(old)} -> {describe(new)}", "tada")
        if nn > on:
            return ("Waitlist went up", f"{describe(old)} -> {describe(new)}", "warning")
    if nk == "CANCELLED":
        return ("Ticket cancelled/flushed", f"{old} -> {new}", "x")
    return ("PNR status changed", f"{old} -> {new}", "information_source")


# ------------------------------------------------------------ fetching
def find_current_statuses(obj) -> list[str]:
    """Recursively collect values of any key that looks like 'currentStatus' / 'Current_Status'."""
    found: list[str] = []
    if isinstance(obj, dict):
        norm = {k.replace("-", "_").lower(): v for k, v in obj.items()}
        # Prefer 'currentStatusDetails' ("WL/35") over 'currentStatus' ("WL", number missing).
        for key in ("current_?status_?details", "current_?status"):
            hit = next((v for k, v in norm.items() if re.fullmatch(key, k) and isinstance(v, str) and v), None)
            if hit:
                return [hit]
        for v in obj.values():
            found += find_current_statuses(v)
    elif isinstance(obj, list):
        for item in obj:
            found += find_current_statuses(item)
    return found


class PnrFlushed(Exception):
    """API says 'FLUSHED PNR': journey over or ticket cancelled and purged. Nothing left to watch."""


class PnrInvalid(Exception):
    """API says 'Invalid PNR Number': our config is wrong, so retrying would just burn quota."""


class PnrNotYetGenerated(Exception):
    """API says 'PNR NOT YET GENERATED': temporary; keep polling."""


class ApiHttpError(RuntimeError):
    """The PNR API answered with an HTTP error status."""

    def __init__(self, status: int, body: str):
        super().__init__(f"PNR API returned HTTP {status}: {body}")
        self.status = status
        self.body = body


def raise_if_known_failure(text: str) -> None:
    t = (text or "").upper()
    if "FLUSHED" in t:
        raise PnrFlushed(text[:200])
    if "INVALID PNR" in t:
        raise PnrInvalid(text[:200])
    if "NOT YET GENERATED" in t:
        raise PnrNotYetGenerated(text[:200])


def fetch_statuses(pnr: str) -> list[str]:
    """Call the PNR API configured in env and return one current-status string per passenger.

    PNR_API_URL     e.g. https://some-pnr-api.p.rapidapi.com/pnr/{pnr}   ({pnr} gets replaced)
    PNR_API_HEADERS e.g. {"X-RapidAPI-Key": "...", "X-RapidAPI-Host": "..."}
    """
    url = os.environ.get("PNR_API_URL")
    if not url:
        sys.exit("PNR_API_URL is not set. See .env.example (or run --demo / --test-notify).")
    headers = json.loads(os.environ.get("PNR_API_HEADERS", "{}"))
    resp = requests.get(url.replace("{pnr}", pnr), headers=headers, timeout=30)
    raise_if_known_failure(resp.text)  # these arrive as text, with either an HTTP 200 or an error code
    if not resp.ok:  # don't use raise_for_status(): its message includes the URL (and your PNR)
        raise ApiHttpError(resp.status_code, resp.text[:300])
    statuses = find_current_statuses(resp.json())
    if not statuses:
        raise RuntimeError(
            "No 'currentStatus' field found in API response. Adjust find_current_statuses() "
            f"for your provider. Response was: {resp.text[:500]}"
        )
    return statuses


# ------------------------------------------------------------- notify
def notify(title: str, message: str, tags: str = "tada", priority: str = "high") -> None:
    """priority: 'high' buzzes the phone; 'min' lands silently in the ntfy app (no sound, no popup)."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print(f"[no NTFY_TOPIC set] {title}: {message}")
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    r = requests.post(
        f"{server}/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Tags": tags, "Priority": priority},
        timeout=15,
    )
    r.raise_for_status()
    print(f"notified: {title}: {message}")


# --------------------------------------------------------------- state
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ------------------------------------------------------------ schedule
def parse_departure() -> datetime | None:
    """DEPARTURE='2026-11-20 18:30' = departure from YOUR boarding station, local time."""
    raw = os.environ.get("DEPARTURE")
    return datetime.fromisoformat(raw.strip()) if raw else None


FINISHED_EXIT = 3  # exit code of `--once` meaning "nothing left to watch; stop scheduling me"


def _now() -> datetime:
    """Naive wall-clock time in the ticket's timezone (default IST = UTC+5:30), so a cloud runner
    that lives in UTC still compares correctly against DEPARTURE."""
    off = int(os.environ.get("TZ_OFFSET_MINUTES", "330"))
    return datetime.now(timezone(timedelta(minutes=off))).replace(tzinfo=None)


def is_finished(pnr: str) -> bool:
    """True once the ticket is confirmed/cancelled, or the final chart window has passed."""
    if load_state().get("_finished", {}).get(pnr):
        return True
    return schedule(_now(), parse_departure())[1] == "done"


def schedule(now: datetime, dep: datetime | None) -> tuple[int | None, str]:
    """Poll interval for `now`. BASE_POLL_MINUTES (e.g. 60) caps the slow early tiers so a train
    that is only a couple of days away is checked at least that often; chart windows are unaffected."""
    interval, phase = _tier(now, dep)
    base = os.environ.get("BASE_POLL_MINUTES")
    if base and interval is not None and phase not in ("chart1", "chart2"):
        interval = min(interval, int(base))
    return interval, phase


def _tier(now: datetime, dep: datetime | None) -> tuple[int | None, str]:
    """(minutes between API calls, phase name). Waitlists move mostly via cancellations and at the
    two chart preparations (~4h and ~2h before departure), so poll rarely early and often then."""
    if dep is None:
        return 360, "no-departure"
    h = (dep - now).total_seconds() / 3600
    if h < 0.5:
        return None, "done"      # status is frozen after the final chart
    chart = os.environ.get("CHART_POLL_MINUTES")  # optional override for both chart windows
    if h < 2:
        return int(chart or 15), "chart2"
    if h < 4:
        return int(chart or 20), "chart1"
    if h < 6:
        return 60, "pre-chart"
    if h < 24:
        return 180, "day-of"
    if h < 48:
        return 360, "2-days"
    if h < 168:
        return 720, "week"
    return 1440, "far"


def estimate_plan(now: datetime, dep: datetime) -> list[tuple[datetime, str]]:
    rows, t = [], now
    while True:
        interval, phase = schedule(t, dep)
        if interval is None:
            return rows
        rows.append((t, phase))
        t += timedelta(minutes=interval)


def classify_failure(e: Exception) -> tuple[str, str]:
    """(push title, push message) naming the specific error. 10-digit numbers (PNR) are masked."""
    if isinstance(e, ApiHttpError):
        hints = {
            429: "API limit reached (HTTP 429): monthly quota or rate limit may be used up",
            401: "API key rejected (HTTP 401): check the RapidAPI key",
            403: "API access denied (HTTP 403): key or subscription problem",
            404: "API endpoint not found (HTTP 404): check PNR_API_URL",
        }
        title = hints.get(e.status) or (
            f"API server error (HTTP {e.status})" if e.status >= 500 else f"API error (HTTP {e.status})"
        )
        detail = e.body
    elif isinstance(e, requests.Timeout):
        title, detail = "API timed out", str(e)
    elif isinstance(e, requests.ConnectionError):
        title, detail = "Network error reaching the API", str(e)
    else:
        title, detail = f"PNR check failed ({type(e).__name__})", str(e)
    msg = re.sub(r"\b\d{10}\b", "<PNR>", detail)[:200]
    return title[:80], msg


def report_failure(pnr: str, e: Exception) -> None:
    """Push the error. Alerts on the 1st failure in a row, then every 6th (not every 10 minutes)."""
    state = load_state()
    count = state.setdefault("_failures", {}).get(pnr, 0) + 1
    state["_failures"][pnr] = count
    save_state(state)
    title, msg = classify_failure(e)
    print(f"check failed ({count} in a row): {title}: {msg}")
    if count == 1 or count % 6 == 0:
        suffix = f" (failing {count} checks in a row)" if count > 1 else ""
        notify(f"PNR alert problem: {title}", msg + suffix, "rotating_light")


def report_success(pnr: str) -> None:
    state = load_state()
    count = state.get("_failures", {}).pop(pnr, 0)
    if count:
        save_state(state)
        notify("PNR checks working again", f"Recovered after {count} failed check(s).", "white_check_mark")


def run_if_due(pnr: str, force: bool = False, now: datetime | None = None) -> bool:
    """Make an API call only if the schedule says it's time. Safe to run every few minutes."""
    now = now or _now()
    state = load_state()
    if state.get("_finished", {}).get(pnr) and not force:
        return False  # confirmed or cancelled earlier: no more API calls
    interval, phase = schedule(now, parse_departure())
    if phase == "done" and not force:
        done = state.setdefault("_done", {})
        if not done.get(pnr):
            done[pnr] = True
            save_state(state)
            notify("Polling finished", "Departure is near; charts are done. Safe travels!", "train")
        return False
    last = state.get("_last_poll", {}).get(pnr)
    # 3-minute grace: GitHub's cron can fire a few minutes late, which must not skip a whole slot
    grace = min(3, interval // 3)
    if not force and last and now - datetime.fromisoformat(last) < timedelta(minutes=interval - grace):
        return False
    # keep CHART_RESERVE calls back for the chart windows, where polling matters most
    reserve = 0 if force or phase in ("chart1", "chart2") else int(os.environ.get("CHART_RESERVE", "15"))
    if not spend_budget(state, reserve):
        save_state(state)
        print("API budget exhausted/reserved; skipping this check")
        return False
    state.setdefault("_last_poll", {})[pnr] = now.isoformat(timespec="seconds")
    save_state(state)  # count the call even if the request then fails
    try:
        check_once(pnr)
    except PnrFlushed:
        state = load_state()
        state.setdefault("_finished", {})[pnr] = "flushed"
        save_state(state)
        notify("PNR flushed", "Railways has flushed this PNR (journey over or ticket cancelled). Tracking stopped.", "checkered_flag")
    except PnrInvalid:
        state = load_state()
        state.setdefault("_finished", {})[pnr] = "invalid"
        save_state(state)
        notify("Invalid PNR", "The API says this PNR number is invalid. Check PNR in your settings. Tracking stopped.", "x")
    except PnrNotYetGenerated:
        print("PNR not generated yet; will retry on the next scheduled check")
    except Exception as e:  # API down, quota hit, bad key, network, unexpected response...
        report_failure(pnr, e)
    else:
        report_success(pnr)
    return True


def spend_budget(state: dict, reserve: int = 0) -> bool:
    """Count one API call against MONTHLY_BUDGET (default 90). False once it's used up.
    `reserve` calls are kept back for callers that pass reserve=0 (the chart windows)."""
    month = time.strftime("%Y-%m")
    b = state.setdefault("_budget", {})
    if b.get("month") != month:
        b.clear()
        b.update(month=month, used=0, warned=False)
    limit = int(os.environ.get("MONTHLY_BUDGET", "90"))
    if b["used"] >= limit - reserve:
        if reserve == 0 and not b["warned"]:
            b["warned"] = True
            notify("API quota used up", f"{limit} calls used this month. Paused until next month.", "warning")
        return False
    b["used"] += 1
    return True


def heartbeat_text(statuses: list[str]) -> str:
    """'Checked 10:20 PM: WL 34 (no change)'; time is in the ticket's timezone (IST by default)."""
    when = _now().strftime("%I:%M %p").lstrip("0")
    if len(statuses) == 1:
        now_s = describe(statuses[0])
    else:
        now_s = " | ".join(f"P{i + 1} {describe(s)}" for i, s in enumerate(statuses))
    return f"Checked {when}: {now_s} (no change)"


def check_once(pnr: str, statuses: list[str] | None = None) -> None:
    statuses = statuses if statuses is not None else fetch_statuses(pnr)
    state = load_state()
    previous: list[str] | None = state.get(pnr)

    if previous is None:  # first run: just report where we are
        notify("Tracking started", " | ".join(describe(s) for s in statuses), "eyes")
    else:
        changed = False
        for i, new in enumerate(statuses):
            old = previous[i] if i < len(previous) else new
            change = compare(old, new)
            if change:
                changed = True
                title, msg, tags = change
                who = f"P{i + 1}: " if len(statuses) > 1 else ""
                notify(title, who + msg, tags)
        if not changed and os.environ.get("HEARTBEAT", "1") != "0":
            # Silent "still alive" note, so silence never has to mean "maybe it stopped running"
            notify("Checked: no change", heartbeat_text(statuses), "hourglass", priority="min")
    state[pnr] = statuses
    kinds = [parse_status(s)[0] for s in statuses]
    if kinds and all(k in ("CNF", "CANCELLED") for k in kinds):  # nothing left to wait for
        state.setdefault("_finished", {})[pnr] = "confirmed" if set(kinds) == {"CNF"} else "cancelled"
    save_state(state)
    shown = pnr[:2] + "*" * max(len(pnr) - 4, 0) + pnr[-2:]  # keep the PNR out of logs
    print(time.strftime("%H:%M:%S"), shown, [describe(s) for s in statuses])


# ---------------------------------------------------------------- main
def main() -> None:
    global STATE_FILE
    load_env()
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", action="store_true")
    g.add_argument("--test-notify", action="store_true")
    g.add_argument("--demo", action="store_true")
    g.add_argument("--plan", action="store_true", help="show the polling schedule and estimated API calls")
    ap.add_argument("--force", action="store_true", help="with --once: call the API now, ignoring the schedule")
    args = ap.parse_args()

    if args.plan:
        dep = parse_departure()
        if not dep:
            sys.exit("Set DEPARTURE=YYYY-MM-DD HH:MM in .env first.")
        rows = estimate_plan(datetime.now(), dep)
        counts: dict[str, int] = {}
        for _, phase in rows:
            counts[phase] = counts.get(phase, 0) + 1
        for phase, n in counts.items():
            print(f"{phase:>10}: {n} calls")
        print(f"     total: {len(rows)} calls  (monthly budget: {os.environ.get('MONTHLY_BUDGET', '90')})")
        return

    if args.test_notify:
        notify("Test from pnr-alert", "If you see this, push works!", "white_check_mark")
        return
    if args.demo:  # sandboxed: own state file, console only, never touches your real state or phone
        STATE_FILE = Path(tempfile.gettempdir()) / "pnr_alert_demo_state.json"
        STATE_FILE.unlink(missing_ok=True)
        os.environ.pop("NTFY_TOPIC", None)
        for s in (["GNWL23"], ["GNWL17"], ["GNWL9"], ["RAC 4"], ["CNF/B3/42"]):
            check_once("DEMO", s)
            time.sleep(1)
        STATE_FILE.unlink(missing_ok=True)
        return

    pnr = os.environ.get("PNR")
    if not pnr:
        sys.exit("PNR is not set. See .env.example.")
    if args.once:
        run_if_due(pnr, force=args.force)
        if is_finished(pnr):
            print("ticket resolved or chart window over: nothing left to watch")
            sys.exit(FINISHED_EXIT)
        return
    tick = float(os.environ.get("CHECK_TICK_MINUTES", "5"))  # waking up is free; only API calls cost
    while True:
        try:
            run_if_due(pnr)
        except Exception as e:  # keep looping through transient API/network errors
            print("check failed:", e)
        if is_finished(pnr):
            print("ticket resolved or chart window over: stopping")
            return
        time.sleep(tick * 60)


if __name__ == "__main__":
    main()
