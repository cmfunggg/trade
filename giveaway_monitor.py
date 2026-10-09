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

import json
import os
import re
import time
from collections import Counter, deque

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
        r = requests.post(WEBHOOK, json={"content": text}, timeout=10)
        print(f"[discord] HTTP {r.status_code}", flush=True)
    except requests.RequestException as e:
        print(f"[discord] failed: {e}", flush=True)


BOT_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"}
COOKIES = {"CONSENT": "YES+1", "SOCS": "CAI"}
LIVE_SIGNALS = (
    '"isLiveNow":true',
    'itemprop="isLiveBroadcast" content="True"',
    "BADGE_STYLE_TYPE_LIVE_NOW",
)


def fetch(url, headers):
    return requests.get(url, headers=headers, cookies=COOKIES, timeout=15)


def page_title(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    return (m.group(1).strip() if m else "")[:60]


def video_id_from_head(html: str):
    """Find a watch/embed video id inside <meta>/<link> tags only (not the whole page)."""
    for tag in re.findall(r"<(?:meta|link)\b[^>]*>", html[:300000]):
        if re.search(r'og:url|og:video|twitter:player|rel="canonical"|itemprop="url"', tag):
            m = re.search(r"(?:watch\?v=|/embed/)([\w-]{11})", tag)
            if m:
                return m.group(1)
    return None


def check_live():
    """Return (video_id or None, multi-line diagnostic string)."""
    info = []
    vid = None

    # Step 1: does the channel's /live URL resolve to a video? Try as a browser, then as a link-preview bot.
    for label, hdrs in (("browser", HEADERS), ("bot", BOT_HEADERS)):
        try:
            r = fetch(CHANNEL_LIVE_URL, hdrs)
        except requests.RequestException as e:
            info.append(f"{label}: request failed: {e}")
            continue
        html = r.text
        m = re.search(r"[?&]v=([\w-]{11})", r.url)
        cand = m.group(1) if m else video_id_from_head(html)
        info.append(f"{label}: HTTP {r.status_code}, size {len(html)}, id={cand}, title={page_title(html)!r}")
        if cand:
            vid = cand
            break

    if not vid:
        return None, "\n".join(info + ["result: /live did not resolve to a video"])

    # Step 2: confirm the video is actually live now.
    try:
        r = fetch(f"https://www.youtube.com/watch?v={vid}", HEADERS)
        html = r.text
    except requests.RequestException as e:
        info.append(f"watch page request failed: {e}")
        return vid, "\n".join(info + ["result: assumed LIVE (could not confirm)"])

    confirmed = any(sig in html for sig in LIVE_SIGNALS)
    ended = 'itemprop="endDate"' in html
    upcoming = '"isUpcoming":true' in html
    info.append(f"watch page: confirmed_live={confirmed}, ended={ended}, upcoming={upcoming}")

    if confirmed:
        return vid, "\n".join(info + ["result: LIVE (confirmed)"])
    if ended or upcoming:
        return None, "\n".join(info + ["result: video is not live right now"])
    return vid, "\n".join(info + ["result: assumed LIVE (unconfirmed)"])


def normalize(msg: str) -> str:
    return re.sub(r"\s+", " ", msg.strip().lower())


class ChatEnded(Exception):
    pass


def runs_to_text(runs) -> str:
    parts = []
    for run in runs:
        if "text" in run:
            parts.append(run["text"])
        elif "emoji" in run:
            e = run["emoji"]
            parts.append((e.get("shortcuts") or [e.get("emojiId", "")])[0])
    return "".join(parts)


def parse_chat_page(html: str):
    """Pull (api_key, client_version, first_continuation_token) out of the live_chat popout page."""
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    idx = html.find("ytInitialData")
    if not (key and ver) or idx < 0:
        raise RuntimeError(f"chat page unusable (size {len(html)}, key={bool(key)}, data={idx >= 0})")
    data, _ = json.JSONDecoder().raw_decode(html[html.index("{", idx):])
    renderer = data["contents"]["liveChatRenderer"]
    token = None
    try:  # prefer "Live chat" (all messages) over "Top chat" (filtered)
        items = renderer["header"]["liveChatHeaderRenderer"]["viewSelector"]["sortFilterSubMenuRenderer"]["subMenuItems"]
        for it in items:
            if it.get("title", "").lower().startswith("live chat"):
                token = it["continuation"]["reloadContinuationData"]["continuation"]
    except (KeyError, TypeError):
        token = None
    if token is None:
        cont = renderer["continuations"][0]
        token = next(v["continuation"] for v in cont.values() if isinstance(v, dict) and "continuation" in v)
    return key.group(1), ver.group(1), token


def parse_chat_response(payload: dict):
    """Return (messages, next_token). Raises ChatEnded when the chat is over."""
    lcc = payload.get("continuationContents", {}).get("liveChatContinuation")
    if not lcc:
        raise ChatEnded()
    conts = lcc.get("continuations") or []
    nxt = next((v for v in (conts[0].values() if conts else []) if isinstance(v, dict) and "continuation" in v), None)
    if not nxt:
        raise ChatEnded()
    messages = []
    for action in lcc.get("actions", []):
        item = action.get("addChatItemAction", {}).get("item", {})
        msg = item.get("liveChatTextMessageRenderer")
        if msg:
            messages.append(runs_to_text(msg.get("message", {}).get("runs", [])))
    return messages, nxt["continuation"]


class ChatReader:
    """Minimal YouTube live-chat reader that reuses our consent cookies."""

    def __init__(self, video_id: str):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.cookies.update(COOKIES)
        r = self.session.get(
            "https://www.youtube.com/live_chat",
            params={"is_popout": "1", "v": video_id},
            timeout=15,
        )
        self.key, self.ver, self.token = parse_chat_page(r.text)

    def read_new(self):
        body = {
            "context": {"client": {"clientName": "WEB", "clientVersion": self.ver, "hl": "en", "gl": "US"}},
            "continuation": self.token,
        }
        r = self.session.post(
            "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat",
            params={"key": self.key, "prettyPrint": "false"},
            json=body,
            timeout=15,
        )
        r.raise_for_status()
        messages, self.token = parse_chat_response(r.json())
        return messages


class PytchatReader:
    """Backup reader."""

    def __init__(self, video_id: str):
        import pytchat

        self.chat = pytchat.create(video_id=video_id)

    def read_new(self):
        if not self.chat.is_alive():
            raise ChatEnded()
        return [c.message for c in self.chat.get().sync_items()]


def open_reader(video_id: str):
    try:
        return ChatReader(video_id)
    except Exception as e1:
        try:
            return PytchatReader(video_id)
        except Exception as e2:
            raise RuntimeError(f"own reader: {str(e1)[:150]} | pytchat: {str(e2)[:150]}")


def wait_until_stream_ends(video_id: str) -> None:
    """Keep this job alive while the stream is up, so GitHub doesn't start a new run (and a new alert) every 5 min."""
    started = time.time()
    while time.time() - started < MAX_RUNTIME:
        time.sleep(120)
        vid, _ = check_live()
        if vid != video_id:
            return


def sniper(video_id: str) -> None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    notify(f"Tradeify is LIVE, sniper mode on: {url}")

    try:
        reader = open_reader(video_id)
    except Exception as e:
        notify(f"LIVE, but I cannot read the chat automatically ({e}). Watch it yourself: {url}")
        wait_until_stream_ends(video_id)
        return

    window = deque(maxlen=WINDOW)
    last_alert = {}  # phrase -> timestamp
    started = time.time()
    errors = 0
    total = 0
    last_status = 0.0

    while time.time() - started < MAX_RUNTIME:
        try:
            batch = reader.read_new()
            total += len(batch)
            for raw in batch:
                text = normalize(raw)
                if len(text) >= MIN_LEN:
                    window.append(text)
            errors = 0
        except ChatEnded:
            break
        except Exception as e:
            errors += 1
            print(f"[sniper] chat error ({errors}): {e}", flush=True)
            if errors >= 12:
                notify(f"Chat reader keeps failing, giving up. Watch it yourself: {url}")
                wait_until_stream_ends(video_id)
                return

        if window:
            phrase, count = Counter(window).most_common(1)[0]
            now = time.time()
            if count >= THRESHOLD and now - last_alert.get(phrase, 0) > COOLDOWN:
                last_alert[phrase] = now
                notify(f"POSSIBLE GIVEAWAY: {count} people typed \"{phrase}\"\n{url}")

        if time.time() - last_status > 60:
            last_status = time.time()
            top = Counter(window).most_common(1)
            print(f"[sniper] chat messages read so far: {total}, window: {len(window)}, top phrase: {top[0] if top else None}", flush=True)

        time.sleep(SNIPER_SECONDS)

    notify("Stream chat ended, back to patrol.")


def main_oneshot() -> None:
    """For GitHub Actions: the cron schedule is the patrol loop."""
    vid, info = check_live()
    print(info, flush=True)
    print(f"Discord webhook configured: {bool(WEBHOOK)}", flush=True)
    # Manual runs ("Run workflow" button) always report to Discord, so you can test.
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        notify(f"Manual check: {'LIVE' if vid else 'not live'}\n```\n{info[:1500]}\n```")
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
