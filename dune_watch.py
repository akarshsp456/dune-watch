#!/usr/bin/env python3
"""
Dune: Part Three @ AMB Victory (IMAX, RTC X Roads, Hyderabad) - BookMyShow watcher.

Checks BookMyShow with a real headless browser and pushes a notification to
your phone (ntfy app, optionally Telegram) when:
  1. Dune: Part Three bookings open anywhere in Hyderabad  (heads-up, sent once)
  2. AMB Victory / AMB Classic Victory shows up with shows (urgent, repeated a few times)

Run once:          python dune_watch.py
Run in a loop:     python dune_watch.py --loop 300        (every ~5 min)
Test your phone:   python dune_watch.py --test-notify

Config is through environment variables (see README.md).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import parse, request

# ----------------------------------------------------------------- config ---
BMS = os.getenv("BMS_BASE", "https://in.bookmyshow.com").rstrip("/")
CITY = os.getenv("BMS_CITY", "hyderabad")
CITY_CODE = os.getenv("BMS_CITY_CODE", "hyd")
DEFAULT_SLUG = os.getenv("MOVIE_SLUG", "dune-part-three")

# Optional but recommended: paste the BookMyShow movie link(s) for Dune: Part Three
# (comma separated), e.g. https://in.bookmyshow.com/movies/hyderabad/dune-part-three/ET00123456
MOVIE_URLS = [u.strip() for u in os.getenv("BMS_MOVIE_URLS", "").split(",") if u.strip()]

# Dates to check once bookings are open (release is 18 Dec 2026; some Indian
# shows may start 17 Dec). The checker looks at this many days from that date.
FIRST_DATE = dt.date.fromisoformat(os.getenv("FIRST_SHOW_DATE", "2026-12-17"))
CHECK_DAYS = int(os.getenv("CHECK_DAYS", "7"))

# Matches "AMB Victory", "AMB Classic Victory", "AMB Cinemas: Victory, RTC X Roads" ...
# but NOT the old "AMB Cinemas: Gachibowli".
VENUE_RE = re.compile(
    os.getenv(
        "VENUE_REGEX",
        r"AMB[^\n\"<>]{0,40}?(?:Victory|RTC)|Victory[^\n\"<>]{0,25}?AMB",
    ),
    re.I,
)
AMB_MAX_ALERTS = int(os.getenv("AMB_MAX_ALERTS", "3"))
STATE_FILE = Path(os.getenv("STATE_FILE", ".dune_state.json"))

SHOWTIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s?(?:AM|PM)\b", re.I)
BLOCK_MARKERS = (
    "access denied", "just a moment", "attention required", "request unsuccessful",
    "pardon our interruption", "verify you are human", "are you a robot",
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def log(*a):
    print(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------- notifications ---
def notify(title: str, message: str, url: str | None = None,
           priority: str = "default", tags: str = "") -> bool:
    sent = False
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        req = request.Request(f"{server}/{topic}", data=message.encode("utf-8"), method="POST")
        req.add_header("Title", title.encode("ascii", "ignore").decode())
        req.add_header("Priority", priority)
        if tags:
            req.add_header("Tags", tags)
        if url:
            req.add_header("Click", url)
        try:
            request.urlopen(req, timeout=20).read()
            sent = True
        except Exception as e:  # noqa: BLE001
            log("ntfy failed:", e)

    tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        text = f"{title}\n\n{message}" + (f"\n\n{url}" if url else "")
        data = parse.urlencode({"chat_id": tg_chat, "text": text}).encode()
        try:
            request.urlopen(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                            data=data, timeout=20).read()
            sent = True
        except Exception as e:  # noqa: BLE001
            log("telegram failed:", e)

    if not (topic or (tg_token and tg_chat)):
        log("No NTFY_TOPIC or TELEGRAM_* set, printing instead.")
    log(f"NOTIFY [{priority}] {title} | {message} | {url or ''}")
    return sent


# ------------------------------------------------------------------ state ---
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------- browser ---
@dataclass
class Snap:
    url: str
    final_url: str
    status: int | None
    text: str
    html: str
    xhr: str
    blocked: bool

    @property
    def everything(self) -> str:
        return "\n".join((self.text, self.html, self.xhr))


def load(page, url: str) -> Snap:
    responses = []

    def handler(r):
        responses.append(r)

    page.on("response", handler)
    status = None
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        status = resp.status if resp else None
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1500)
        text = page.inner_text("body") if page.query_selector("body") else ""
        html = page.content()
        title = page.title()
    finally:
        page.remove_listener("response", handler)

    xhr_parts = []
    for r in responses:
        try:
            ctype = (r.headers or {}).get("content-type", "")
            if "json" in ctype and "bookmyshow" in r.url:
                xhr_parts.append(r.text()[:400_000])
        except Exception:  # noqa: BLE001
            pass

    head = (title + "\n" + text[:3000]).lower()
    blocked = status in (403, 429) or any(m in head for m in BLOCK_MARKERS)
    time.sleep(random.uniform(1.0, 2.5))  # be polite, look less like a bot
    return Snap(url, page.url, status, text, html, "\n".join(xhr_parts), blocked)


# ------------------------------------------------------------------ logic ---
MOVIE_LINK_RE = re.compile(rf"/movies/{CITY}/([a-z0-9-]+)/(?:buytickets/)?(ET\d+)", re.I)
QUERY_BY_ET: dict[str, str] = {}  # keeps e.g. ?language=english from a pasted link


def is_dune_three(slug: str) -> bool:
    s = slug.lower()
    # exclude Dune Part One/Two IMAX re-releases
    return "dune" in s and bool(re.search(r"three|part-?3|dune-?3", s))


def discover_movies(page, state: dict) -> list[tuple[str, str]]:
    """Return [(slug, ETcode)] for Dune: Part Three in the city."""
    found: dict[str, str] = {}
    for u in MOVIE_URLS:
        # accepts .../movies/hyderabad/dune-part-three/ET00526390
        #     and .../movies/hyderabad/dune-part-three/buytickets/ET00526390/20261218?language=english
        m = re.search(r"/movies/[^/]+/([a-z0-9-]+)/(?:buytickets/)?(ET\d+)", u, re.I)
        if m:
            found[m.group(2)] = m.group(1)
            if "?" in u:
                QUERY_BY_ET[m.group(2)] = "?" + u.split("?", 1)[1]
        elif re.search(r"ET\d+", u):
            found[re.search(r"ET\d+", u).group(0)] = DEFAULT_SLUG
        else:
            log("Couldn't read a movie code from BMS_MOVIE_URLS entry:", u)
    if found:
        return [(s, e) for e, s in found.items()]

    for e, s in state.get("known_movies", {}).items():
        found[e] = s

    for listing in (f"{BMS}/explore/movies-{CITY}", f"{BMS}/explore/upcoming-movies-{CITY}"):
        snap = load(page, listing)
        if snap.blocked:
            raise BlockedError(listing)
        for slug, et in MOVIE_LINK_RE.findall(snap.everything):
            if is_dune_three(slug):
                found[et] = slug
    state["known_movies"] = found
    return [(s, e) for e, s in found.items()]


class BlockedError(Exception):
    pass


def window_dates() -> list[str]:
    start = max(FIRST_DATE, dt.date.today())
    return [(start + dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(CHECK_DAYS)]


def venue_hit(snap: Snap) -> tuple[str, bool] | None:
    # Look in visible text first (most reliable), then embedded/XHR JSON.
    for blob in (snap.text, snap.xhr, snap.html):
        m = VENUE_RE.search(blob)
        if m:
            nearby = blob[m.start(): m.end() + 600]
            return m.group(0).strip(), bool(re.search(r"IMAX", nearby, re.I))
    return None


def check(page, state: dict) -> dict:
    out = {"movies": [], "hyd_open": [], "amb": []}
    movies = discover_movies(page, state)
    out["movies"] = movies
    if not movies:
        log("Dune: Part Three not listed for", CITY, "on BookMyShow yet.")
        return out

    for slug, et in movies:
        base = f"{BMS}/movies/{CITY}/{slug}/buytickets/{et}"
        q = QUERY_BY_ET.get(et, "")
        open_dates = []
        for d in window_dates():
            url = f"{base}/{d}{q}"
            snap = load(page, url)
            if snap.blocked:
                raise BlockedError(url)
            if d not in snap.final_url or not SHOWTIME_RE.search(snap.text):
                continue  # no shows on this date (yet)
            open_dates.append(d)
            out["hyd_open"].append(snap.final_url)
            hit = venue_hit(snap)
            log(f"  {d}: shows listed; AMB Victory {'FOUND' if hit else 'not listed'}")
            if hit:
                out["amb"].append({"url": snap.final_url, "venue": hit[0], "imax": hit[1], "date": d})
        log(f"{et} ({slug}): " + (f"BOOKING OPEN on {', '.join(open_dates)}" if open_dates
                                    else "no shows listed yet"))
    return out


def run_once() -> int:
    from playwright.sync_api import sync_playwright

    state = load_state()
    now = time.time()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(
                user_agent=UA, locale="en-IN", timezone_id="Asia/Kolkata",
                viewport={"width": 1366, "height": 900})
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = ctx.new_page()
            try:
                res = check(page, state)
            finally:
                browser.close()
    except BlockedError as e:
        log("BookMyShow blocked this request:", e)
        if now - state.get("blocked_warned_at", 0) > 6 * 3600:
            notify("Dune watcher is being blocked",
                   "BookMyShow is blocking this machine, so the watcher can't see "
                   "showtimes. If this keeps happening on GitHub, run it on your laptop "
                   "instead: python dune_watch.py --loop 300",
                   priority="default", tags="warning")
            state["blocked_warned_at"] = now
        save_state(state)
        return 0  # don't mark the GitHub run as failed (avoids an email every 10 min)

    state["last_ok"] = dt.datetime.now().isoformat(timespec="seconds")

    if res["hyd_open"] and not res["amb"] and not state.get("hyd_open_notified"):
        notify("Dune: Part Three booking is OPEN in Hyderabad",
               "Bookings have opened on BookMyShow. AMB Victory not listed yet; "
               "keep watching for the next alert.",
               url=res["hyd_open"][0], priority="high", tags="movie_camera")
        state["hyd_open_notified"] = True

    if res["amb"]:
        count = state.get("amb_alerts", 0)
        if count < AMB_MAX_ALERTS:
            first = sorted(res["amb"], key=lambda h: h["date"])[0]
            dates = [dt.datetime.strptime(d, "%Y%m%d").strftime("%d %b")
                     for d in sorted({h["date"] for h in res["amb"] })]
            imax = any(h["imax"] for h in res["amb"])
            notify("BOOK NOW: Dune 3 at AMB Victory",
                   f"{first['venue']} is live on BookMyShow"
                   f"{' (IMAX listed)' if imax else ''}. "
                   f"Dates with shows: {', '.join(dates) or 'see app'}. Tap to open.",
                   url=first["url"], priority="urgent", tags="rotating_light,movie_camera")
            state["amb_alerts"] = count + 1
        else:
            log("AMB already alerted", count, "times; staying quiet. Disable the watcher once booked.")
    save_state(state)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, metavar="SECONDS",
                    help="keep running, checking every SECONDS (with jitter)")
    ap.add_argument("--test-notify", action="store_true", help="send a test push and exit")
    ap.add_argument("--reset", action="store_true", help="forget previous alerts")
    a = ap.parse_args()

    if a.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
    if a.test_notify:
        ok = notify("Dune watcher test", "If you see this, phone alerts work.",
                    url=f"{BMS}/explore/movies-{CITY}", priority="high", tags="white_check_mark")
        sys.exit(0 if ok else 1)
    if not a.loop:
        sys.exit(run_once())
    while True:
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            log("check failed:", repr(e))
        time.sleep(a.loop + random.uniform(0, a.loop * 0.2))


if __name__ == "__main__":
    main()
