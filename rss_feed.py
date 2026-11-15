#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["feedparser>=6", "requests>=2.31", "pyyaml>=6"]
# ///
"""Post new RSS/Atom items to Discord channels through webhooks.

Reads `rss` in server.yaml. Remembers seen item IDs per feed in rss_seen.json.
A feed seen for the first time only posts its latest item, so adding a feed never floods the channel.
A feed that fails (timeout, 5xx, bad XML) is skipped for this run, never fatal.

    uv run rss_feed.py --dry-run
    uv run rss_feed.py

Env: DISCORD_WEBHOOKS (JSON {channel: webhook URL}, from setup.py).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
import yaml

HERE = Path(__file__).resolve().parent
STATE = HERE / "rss_seen.json"
RECENT = 30          # only the newest RECENT entries of a feed are ever considered (some feeds hold 1000+ items)
SEEN_PER_FEED = 300  # must stay > RECENT
PRERELEASE = re.compile(r"(?i)(\d[-.]?rc\d*\b|\brc\d+\b|\balpha\b|\bbeta\b|\bnightly\b|\.dev\d)")
UA = "oss-discord-setup feed reader (+https://github.com/simoncoding95-ctrl/oss-discord-setup)"


def load_env() -> None:
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def text(value: str, limit: int) -> str:
    plain = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    plain = " ".join(plain.split())
    return plain[:limit - 1] + "…" if len(plain) > limit else plain


def fetch(url: str):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  ! {url}: {e}")
        return None
    parsed = feedparser.parse(r.content)
    if parsed.bozo and not parsed.entries:
        print(f"  ! {url}: unreadable feed")
        return None
    return parsed


def entry_time(e) -> datetime | None:
    t = e.get("published_parsed") or e.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def embed(feed_name: str, e, color: int) -> dict:
    ts = entry_time(e)
    out = {
        "title": text(e.get("title", "(untitled)"), 250),
        "url": e.get("link"),
        "description": text(e.get("summary", ""), 300) or None,
        "color": color,
        "author": {"name": feed_name},
    }
    if ts:
        out["timestamp"] = ts.isoformat()
    return out


def send(hook: str, name: str, embeds: list[dict]) -> None:
    for i in range(0, len(embeds), 5):  # 6000 chars per message cap
        while True:
            r = requests.post(hook + "?wait=true", json={"username": name, "embeds": embeds[i:i + 5],
                                                         "allowed_mentions": {"parse": []}}, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                continue
            if r.status_code >= 400:
                sys.exit(f"✗ webhook {r.status_code}: {r.text[:500]}")
            break
        time.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_env()
    cfg = yaml.safe_load((HERE / "server.yaml").read_text(encoding="utf-8"))
    hooks = json.loads(os.environ.get("DISCORD_WEBHOOKS") or "{}")
    if not hooks and not args.dry_run:
        sys.exit("✗ DISCORD_WEBHOOKS is required")
    state: dict[str, list[str]] = json.loads(STATE.read_text()) if STATE.exists() else {}

    for block in cfg.get("rss", []):
        color = int(str(block.get("color", "#5865F2")).lstrip("#"), 16)
        new = []  # (time, feed name, entry, feed url, id)
        for f in block["feeds"]:
            parsed = fetch(f["url"])
            if parsed is None:
                continue
            seen = state.get(f["url"])
            entries = [e for e in parsed.entries if not PRERELEASE.search(e.get("title", ""))]
            entries = sorted(entries, key=lambda e: entry_time(e) or datetime.min.replace(tzinfo=timezone.utc),
                             reverse=True)[:RECENT]
            ids = [e.get("id") or e.get("link") for e in entries]
            if seen is None:  # first time: only the latest item, mark the rest as seen
                fresh = entries[:1]
                state[f["url"]] = ids[1:SEEN_PER_FEED]
            else:
                fresh = [e for e, i in zip(entries, ids) if i not in set(seen)]
            for e in fresh:
                new.append((entry_time(e) or datetime.now(timezone.utc), f["name"], e, f["url"],
                            e.get("id") or e.get("link")))
        new.sort(key=lambda x: x[0])  # oldest first, so the channel reads chronologically
        overflow = new[:-block.get("max_per_run", 8)] if len(new) > block.get("max_per_run", 8) else []
        new = new[-block.get("max_per_run", 8):]
        for _, name, e, _, _ in new:
            print(f"  #{block['channel']} ← [{name}] {text(e.get('title', ''), 90)}")
        if overflow:
            print(f"  ({len(overflow)} older items skipped, max_per_run reached)")
        if args.dry_run:
            continue
        if new:
            send(hooks[block["channel"]], block["webhook_name"], [embed(n, e, color) for _, n, e, _, _ in new])
        for _, _, _, url, i in new + overflow:  # overflow is marked seen too: never post stale backlog
            state[url] = ([i] + state.get(url, []))[:SEEN_PER_FEED]

    if not args.dry_run:
        STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")
        print("✓ done")


if __name__ == "__main__":
    main()
