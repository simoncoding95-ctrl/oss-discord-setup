#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.31", "pyyaml>=6"]
# ///
"""Post fresh GitHub "good first issue" issues to Discord through webhooks.

Each run: up to `max_per_language` issues go to each language channel (#python, #rust...), seen only by
members with that role; a digest of up to `max_per_run` of them goes to the public feed channel, which
pings the opt-in role. Reads `gfi_feed` in server.yaml, remembers posted URLs in gfi_posted.json.

    uv run gfi_feed.py --dry-run   # print what would be posted
    uv run gfi_feed.py             # post

Env: GITHUB_TOKEN (any token, read-only is enough), GFI_WEBHOOKS (JSON {channel: webhook URL}, from setup.py).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
STATE = HERE / "gfi_posted.json"
STATE_MAX = 5000

QUERY = """
query($q: String!, $labels: [String!]) {
  search(query: $q, type: REPOSITORY, first: 50) {
    nodes {
      ... on Repository {
        nameWithOwner url stargazerCount isFork
        owner { avatarUrl }
        issues(labels: $labels, states: OPEN, first: 3, orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes {
            url title createdAt bodyText
            assignees { totalCount }
            labels(first: 6) { nodes { name } }
            closedByPullRequestsReferences(first: 1, includeClosedPrs: true) { totalCount }
            timelineItems(itemTypes: [CONNECTED_EVENT], first: 1) { totalCount }
          }
        }
      }
    }
  }
}
"""


def load_env() -> None:
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def gh_search(token: str, q: str, labels: list[str]) -> list[dict]:
    for attempt in range(3):
        r = requests.post("https://api.github.com/graphql",
                          json={"query": QUERY, "variables": {"q": q, "labels": labels}},
                          headers={"Authorization": f"bearer {token}"}, timeout=60)
        if r.status_code == 200 and "errors" not in r.json():
            return [n for n in r.json()["data"]["search"]["nodes"] if n]
        time.sleep(5 * (attempt + 1))
    sys.exit(f"✗ GitHub search failed for {q!r}: {r.status_code} {r.text[:300]}")


def mostly_latin(text: str) -> bool:
    """The server is English-first: skip issues whose title is mostly non-Latin script."""
    letters = [c for c in text if c.isalpha()]
    return not letters or sum(c.isascii() for c in letters) / len(letters) >= 0.7


def candidates(token: str, feed: dict, role: str, langs: list[str], posted: set[str]) -> list[dict]:
    """Established repos first (stars, recent push, has good first issues), then their newest labelled issues."""
    since = (datetime.now(timezone.utc) - timedelta(days=feed.get("max_age_days", 14))).date().isoformat()
    block = set(feed.get("blocklist") or [])
    out = []
    for lang in langs:
        q = (f'language:"{lang}" stars:>={feed.get("min_stars", 0)} good-first-issues:>0 '
             f"pushed:>={since} archived:false fork:false sort:updated-desc")
        for repo in gh_search(token, q, feed["labels"]):
            if repo["nameWithOwner"] in block:
                continue
            for it in repo["issues"]["nodes"]:
                if (it["url"] in posted or it["createdAt"][:10] < since or it["assignees"]["totalCount"]
                        or not mostly_latin(it["title"])):
                    continue
                if feed.get("skip_if_linked_pr", True) and (
                        it["closedByPullRequestsReferences"]["totalCount"] or it["timelineItems"]["totalCount"]):
                    continue
                repo_info = {k: repo[k] for k in ("nameWithOwner", "url", "stargazerCount", "owner")}
                out.append({**it, "repository": repo_info, "role": role})
    out.sort(key=lambda i: i["createdAt"], reverse=True)
    return out


def per_language(items: list[dict], limit: int, used_repos: set[str]) -> list[dict]:
    """Newest first, one issue per repo across the whole run."""
    out = []
    for it in items:
        repo = it["repository"]["nameWithOwner"]
        if repo not in used_repos and len(out) < limit:
            used_repos.add(repo)
            out.append(it)
    return out


def pick(per_lang: dict[str, list[dict]], limit: int) -> list[dict]:
    """Digest: round-robin across languages, one issue per repo."""
    chosen, repos = [], set()
    queues = {k: list(v) for k, v in per_lang.items() if v}
    while queues and len(chosen) < limit:
        for role in list(queues):
            q = queues[role]
            while q and q[0]["repository"]["nameWithOwner"] in repos:
                q.pop(0)
            if not q:
                del queues[role]
                continue
            it = q.pop(0)
            repos.add(it["repository"]["nameWithOwner"])
            chosen.append(it)
            if len(chosen) >= limit:
                break
    return chosen


def embed(it: dict, colors: dict[str, int]) -> dict:
    repo = it["repository"]
    body = " ".join(it["bodyText"].split())
    labels = ", ".join(l["name"] for l in it["labels"]["nodes"])
    return {
        "title": it["title"][:250],
        "url": it["url"],
        "description": (body[:280] + "…") if len(body) > 280 else body or None,
        "color": colors.get(it["role"], 0x2ECC71),
        "author": {"name": f"{repo['nameWithOwner']}  ★ {repo['stargazerCount']:,}", "url": repo["url"],
                   "icon_url": repo["owner"]["avatarUrl"]},
        "fields": [{"name": "Language", "value": it["role"], "inline": True},
                   {"name": "Labels", "value": labels[:1000] or "—", "inline": True}],
        "timestamp": it["createdAt"],
    }


def send(hook: str, name: str, content: str, embeds: list[dict], ping_role: str | None = None) -> None:
    # Discord caps embeds at 6000 chars per message: 5 per message stays under it.
    for i in range(0, len(embeds), 5):
        r = requests.post(hook + "?wait=true", json={
            "username": name,
            "content": content if i == 0 else None,
            "embeds": embeds[i:i + 5],
            "allowed_mentions": {"roles": [ping_role] if ping_role and i == 0 else []},
        }, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            return send(hook, name, content, embeds[i:], ping_role if i == 0 else None)
        if r.status_code >= 400:
            sys.exit(f"✗ webhook {r.status_code}: {r.text[:500]}")
        time.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_env()

    cfg = yaml.safe_load((HERE / "server.yaml").read_text(encoding="utf-8"))
    feed = cfg["gfi_feed"]
    colors = {r["name"]: int(str(r["color"]).lstrip("#"), 16)
              for g in cfg["self_roles"] for r in g["roles"] if r.get("color")}
    lang_chans = {r["name"]: r["channel"] for g in cfg["self_roles"] for r in g["roles"] if r.get("channel")}
    token = os.environ.get("GITHUB_TOKEN")
    hooks = json.loads(os.environ.get("GFI_WEBHOOKS") or "{}")
    if not token or (not hooks and not args.dry_run):
        sys.exit("✗ GITHUB_TOKEN and GFI_WEBHOOKS are required")

    posted = json.loads(STATE.read_text()) if STATE.exists() else []
    seen = set(posted)
    used_repos: set[str] = set()
    picked: dict[str, list[dict]] = {}
    for role, langs in feed["languages"].items():
        found = candidates(token, feed, role, langs, seen)
        picked[role] = per_language(found, feed.get("max_per_language", 3), used_repos)
        print(f"  {role}: {len(found)} candidates, {len(picked[role])} picked")
    digest = pick(picked, feed.get("max_per_run", 10))
    total = sum(len(v) for v in picked.values())
    if not total:
        print("Nothing new.")
        return
    for it in digest:
        print(f"  digest → [{it['role']}] {it['repository']['nameWithOwner']} ★{it['repository']['stargazerCount']}: {it['title'][:70]}")
    if args.dry_run:
        return

    name = feed.get("webhook_name", "Good First Issues")
    for role, items in picked.items():
        chan = lang_chans.get(role)
        if not items or chan not in hooks:
            continue
        n = len(items)
        send(hooks[chan], name, f"**{n} new {role} good first issue{'s' if n > 1 else ''}**",
             [embed(it, colors) for it in items])
        print(f"  ✓ #{chan}: {n}")

    if digest and feed["channel"] in hooks:
        role_id = feed.get("ping_role_id")
        n = len(digest)
        content = (f"{f'<@&{role_id}> ' if role_id else ''}**{n} new good first issue{'s' if n > 1 else ''}**"
                   " — more for your languages in your language channels. Comment on the issue before you start.")
        send(hooks[feed["channel"]], name, content, [embed(it, colors) for it in digest], role_id)
        print(f"  ✓ #{feed['channel']}: {n}")

    posted = (posted + [it["url"] for items in picked.values() for it in items])[-STATE_MAX:]
    STATE.write_text(json.dumps(posted, indent=0) + "\n")
    print(f"✓ posted {total} issues")


if __name__ == "__main__":
    main()
