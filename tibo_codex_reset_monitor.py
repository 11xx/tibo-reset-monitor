#!/usr/bin/env python3
"""Notify when Tibo posts about a Codex usage-limit reset."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

APP = "tibo-codex-reset-monitor"
FEED_URL = "https://nitter.net/thsottiaux/rss"
FEED_OWNER = "@thsottiaux"
USER_AGENT = f"{APP}/1.0"
STATE_LIMIT = 200

SPACE_RE = re.compile(r"\s+")
TAG_RE = re.compile(r"<[^>]+>")
STATUS_RE = re.compile(r"/status(?:es)?/(\d+)")
RESET_RE = re.compile(
    r"\b(reset|refill(?:ed)?|credit(?:ed)?|banked|quota|rate[- ]?limits?|"
    r"usage limits?)\b",
    re.IGNORECASE,
)
CODEX_RE = re.compile(
    r"\b(codex|chatgpt work|paid users?|paid plans?|all users?|everyone)\b",
    re.IGNORECASE,
)
INCOMING_RE = re.compile(
    r"(?:\b(?:will|going to|plan(?:ning)? to)\b.{0,100}\breset\b|"
    r"\breset\b.{0,100}\b(?:later|tonight|tomorrow|soon|next|coming)\b)",
    re.IGNORECASE,
)
COMPLETED_RE = re.compile(
    r"(?:\b(?:have|has|just|now)\b.{0,100}\breset\b|"
    r"\breset\b.{0,100}\b(?:complete|completed|done|all users?|everyone)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Post:
    key: str
    text: str
    link: str
    published: str


def clean(value: str | None) -> str:
    text = html.unescape(value or "")
    return SPACE_RE.sub(" ", TAG_RE.sub(" ", text)).strip()


def parse_feed(payload: bytes) -> list[Post]:
    root = ET.fromstring(payload)
    channel = root.find("channel")
    if channel is None or FEED_OWNER not in (channel.findtext("title") or "").lower():
        raise ValueError(f"feed is not owned by {FEED_OWNER}")

    posts = []
    for item in channel.findall("item"):
        text = clean(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        status = STATUS_RE.search(f"{guid} {link}")
        key = status.group(1) if status else hashlib.sha256(
            f"{guid}\n{link}\n{published}\n{text}".encode()
        ).hexdigest()
        posts.append(Post(key, text, link, published))
    if not posts:
        raise ValueError("feed contains no posts")
    return posts


def classify(text: str) -> str | None:
    if not RESET_RE.search(text) or not CODEX_RE.search(text):
        return None
    if re.search(r"\bbanked\b", text, re.IGNORECASE):
        return "BANKED RESET"
    if INCOMING_RE.search(text):
        return "RESET INCOMING"
    if COMPLETED_RE.search(text):
        return "RESET COMPLETED"
    return "POSSIBLE RESET SIGNAL"


def state_path() -> Path:
    if directory := os.environ.get("STATE_DIRECTORY"):
        return Path(directory) / "state.json"
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return root / APP / "state.json"


def load_state() -> dict:
    try:
        state = json.loads(state_path().read_text())
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def fetch() -> list[Post]:
    request = urllib.request.Request(
        FEED_URL,
        headers={"Accept": "application/rss+xml", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return parse_feed(response.read(2 * 1024 * 1024))


def notify(summary: str, body: str, critical: bool = False) -> None:
    command = shutil.which("notify-send")
    if not command:
        raise RuntimeError("notify-send is not installed")
    subprocess.run(
        [
            command,
            "--app-name=Tibo Codex monitor",
            f"--urgency={'critical' if critical else 'normal'}",
            f"--expire-time={0 if critical else 15000}",
            summary,
            body,
        ],
        check=True,
        timeout=10,
    )


def run(show_latest: bool = False) -> int:
    state = load_state()
    try:
        posts = fetch()
    except Exception as error:
        failures = int(state.get("failures", 0)) + 1
        state["failures"] = failures
        save_state(state)
        print(f"feed failed: {error}", file=sys.stderr)
        if failures == 12 or (failures > 12 and failures % 288 == 0):
            notify(
                "Tibo Codex monitor is offline",
                "The public feed has failed repeatedly. Check the user service journal.",
                critical=True,
            )
        return 1

    if show_latest:
        for post in posts:
            print(f"[{classify(post.text) or 'ignored'}] {post.text}\n{post.link}\n")
        return 0

    state["failures"] = 0
    seen = list(state.get("seen", []))
    if not state.get("initialized"):
        save_state({"failures": 0, "initialized": True, "seen": [p.key for p in posts]})
        print(f"seeded {len(posts)} existing posts")
        return 0

    seen_set = set(seen)
    for post in reversed(posts):
        if post.key in seen_set:
            continue
        label = classify(post.text)
        if label:
            body = "\n\n".join(part for part in (post.text, post.published, post.link) if part)
            notify(f"Tibo Codex: {label}", body[:1000], critical=label != "POSSIBLE RESET SIGNAL")
            print(f"notified: {label}: {post.text}")
        seen.append(post.key)
        seen_set.add(post.key)

    state.update(initialized=True, failures=0, seen=seen[-STATE_LIMIT:])
    save_state(state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show-latest", action="store_true")
    parser.add_argument("--test-notification", action="store_true")
    args = parser.parse_args()
    if args.test_notification:
        notify("Tibo Codex monitor", "Notifications are working.", critical=True)
        return 0
    return run(args.show_latest)


if __name__ == "__main__":
    raise SystemExit(main())
