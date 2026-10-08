"""
Tradeify live-chat giveaway monitor.

Patrol mode : checks whether the channel is live (every PATROL_SECONDS).
Sniper mode : once live, reads chat every SNIPER_SECONDS and alerts Discord when
              THRESHOLD identical messages appear inside the last WINDOW messages.

It only ALERTS you. Entering the giveaway stays manual.

Setup:
    pip install pytchat requests
    export DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."
    python giveaway_monitor.py
Run it under systemd / tmux / Docker on an always-on machine, OR set ONESHOT=1 and
let a GitHub Actions cron (giveaway.yml) call it every ~5 minutes.
"""

import os
import re
import time
from collections import Counter, deque

import pytchat
import requests

CHANNEL_LIVE_URL = "https://www.youtube.com/@TradeifyTV/live"
WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

PATROL_SECONDS = 120      # how often to check if the channel is live
SNIPER_SECONDS = 5        # chat polling loop while live
WINDOW = 50               # sliding window size (messages)
THRESHOLD = 10            # identical messages needed to trigger
COOLDOWN = 600            # don't re-alert the same phrase within 10 minutes
MIN_LEN = 3               # ignore "gm", "+1", single emoji, etc.
HEARTBEAT_HOURS = 24      # "still alive" ping so silent failures get noticed
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME_SECONDS", 5 * 3600 + 1800))  # stop before GitHub's 6h job cap
ONESHOT = os.environ.get("ONESHOT") == "1"  # True on GitHub Actions: check once, watch if live, exit

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def notify(text: str) -> None:
    print(text, flush=True)
    if not WEBHOOK:
        return
    try:
        requests.post(WEBHOOK, json={"content": text}, timeout=10)
    except requests.RequestException as e:
        print(f"[discord] failed: {e}", flush=True)


def check_live():
    """Return (video_id or None, diagnostic string)."""
    info = []
    try:
        r = requests.get(
            CHANNEL_LIVE_URL,
            headers=HEADERS,
            cookies={"CONSENT": "YES+1", "SOCS": "CAI"},
            timeout=15,
        )
    except requests.RequestException as e:
        return None, f"request failed: {e}"

    html = r.text
    info.append(f"HTTP {r.status_code}")
    info.append(f"final URL: {r.url}")
    info.append(f"page size: {len(html)}")
    if "consent.youtube.com" in r.url:
        info.append("WARNING: landed on YouTube consent page")

    m = re.search(r"[?&]v=([\w-]{11})", r.url)
    if not m:
        m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', html)
    vid = m.group(1) if m else None
    is_live = '"isLiveNow":true' in html
    info.append(f"video id: {vid}")
    info.append(f"isLiveNow: {is_live}")
    return (vid if (vid and is_live) else None), " | ".join(info)


def normalize(msg: str) -> str:
    return re.sub(r"\s+", " ", msg.strip().lower())


def sniper(video_id: str) -> None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    notify(f"Tradeify is LIVE, sniper mode on: {url}")

    chat = pytchat.create(video_id=video_id)
    window = deque(maxlen=WINDOW)
    last_alert = {}  # phrase -> timestamp
    started = time.time()

    while chat.is_alive() and time.time() - started < MAX_RUNTIME:
        try:
            for c in chat.get().sync_items():
                text = normalize(c.message)
                if len(text) >= MIN_LEN:
                    window.append(text)
        except Exception as e:  # pytchat is unofficial; don't die on a hiccup
            print(f"[sniper] chat error: {e}", flush=True)

        if window:
            phrase, count = Counter(window).most_common(1)[0]
            now = time.time()
            if count >= THRESHOLD and now - last_alert.get(phrase, 0) > COOLDOWN:
                last_alert[phrase] = now
                notify(f"POSSIBLE GIVEAWAY: {count} people typed \"{phrase}\"\n{url}")

        time.sleep(SNIPER_SECONDS)

    notify("Stream chat ended, back to patrol.")


def main_oneshot() -> None:
    """For GitHub Actions: the cron schedule is the patrol loop."""
    vid, info = check_live()
    print(info, flush=True)
    print(f"Discord webhook configured: {bool(WEBHOOK)}", flush=True)
    # Manual runs ("Run workflow" button) always report to Discord, so you can test.
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        notify(f"Manual check: {'LIVE' if vid else 'not live'}\n{info}")
    if not vid:
        print("Not live. Exiting.", flush=True)
        return
    sniper(vid)


def main() -> None:
    if ONESHOT:
        main_oneshot()
        return
    if not WEBHOOK:
        print("WARNING: DISCORD_WEBHOOK not set; alerts will only print.", flush=True)
    notify("Giveaway monitor started.")
    last_heartbeat = time.time()

    while True:
        vid, info = check_live()
        print(info, flush=True)
        if vid:
            try:
                sniper(vid)
            except Exception as e:
                notify(f"Sniper crashed: {e}")
        if time.time() - last_heartbeat > HEARTBEAT_HOURS * 3600:
            notify("Monitor heartbeat: still running.")
            last_heartbeat = time.time()
        time.sleep(PATROL_SECONDS)


if __name__ == "__main__":
    main()
