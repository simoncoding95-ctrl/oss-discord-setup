#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.31", "pyyaml>=6"]
# ///
"""Build a Discord server (roles, channels, onboarding) from server.yaml.

Idempotent: roles and channels are matched by name and updated, never duplicated.
Uses the REST API only, no gateway connection, no bot process left running.

    uv run setup.py --dry-run   # show what would change
    uv run setup.py             # apply

Needs DISCORD_TOKEN and GUILD_ID in .env (next to this file) or in the environment.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests
import yaml

API = "https://discord.com/api/v10"
HERE = Path(__file__).resolve().parent

PERMS = {
    "kick": 1 << 1, "ban": 1 << 2, "administrator": 1 << 3, "react": 1 << 6,
    "audit_log": 1 << 7, "view": 1 << 10, "send": 1 << 11, "manage_messages": 1 << 13,
    "mention_everyone": 1 << 17, "connect": 1 << 20, "speak": 1 << 21, "mute": 1 << 22,
    "move": 1 << 24, "manage_nicknames": 1 << 27, "manage_events": 1 << 33,
    "manage_threads": 1 << 34, "create_threads": 1 << 35, "create_private_threads": 1 << 36,
    "send_threads": 1 << 38, "timeout": 1 << 40,
}
WRITE = PERMS["send"] | PERMS["send_threads"] | PERMS["create_threads"] | PERMS["create_private_threads"]
TYPES = {"text": 0, "voice": 2, "category": 4, "announcement": 5, "forum": 15}
DISCORD_EPOCH_MS = 1420070400000
MAX_FORUM_TAGS = 20
MAX_PROMPT_OPTIONS = 12  # Discord rejects more; longer groups are split into several questions
MAX_JOIN_PROMPTS = 4     # Discord rejects a 5th question shown at join (tested); overflow goes to Channels & Roles


def load_env() -> None:
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_seq = 0


def snowflake() -> str:
    """New ID for onboarding prompts/options: Discord expects client-generated snowflakes."""
    global _seq
    _seq += 1
    return str(((int(time.time() * 1000) - DISCORD_EPOCH_MS) << 22) | (_seq & 0xFFF))


def color(value) -> int:
    return int(str(value).lstrip("#"), 16) if value else 0


class Discord:
    def __init__(self, token: str, dry_run: bool):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (oss-discord-setup, 1.0)",
            "X-Audit-Log-Reason": "oss-discord-setup",
        })
        self.dry_run = dry_run
        self._fake = 0

    def req(self, method: str, path: str, body=None, soft: bool = False):
        if self.dry_run and method != "GET":
            self._fake += 1
            return {"id": f"dry-{self._fake}", **(body or {})}
        while True:
            r = self.s.request(method, API + path, json=body)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 1)) + 0.1)
                continue
            if r.status_code == 403 and soft:
                return None
            if r.status_code >= 400:
                sys.exit(f"\n✗ {method} {path} -> {r.status_code}\n{r.text}")
            return r.json() if r.content else {}


# ---------- validation ----------

def validate(cfg: dict) -> None:
    errors = []
    role_names = [r["name"] for r in cfg.get("roles", [])]
    role_names += [r["name"] for g in cfg.get("self_roles", []) for r in g["roles"]]
    chan_names = [c["name"] for cat in cfg.get("categories", []) for c in cat["channels"]]
    chan_names += [r["channel"] for g in cfg.get("self_roles", []) for r in g["roles"] if r.get("channel")]
    for kind, names in (("role", role_names), ("channel", chan_names)):
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            errors.append(f"duplicate {kind} names: {sorted(dupes)}")
    keys = {g["key"] for g in cfg.get("self_roles", [])}
    for cat in cfg.get("categories", []):
        for c in cat["channels"]:
            if c.get("tags_from") and c["tags_from"] not in keys:
                errors.append(f"#{c['name']}: unknown tags_from '{c['tags_from']}'")
            for r in c.get("private_to", []) + cat.get("private_to", []):
                if r not in role_names:
                    errors.append(f"#{c['name']}: unknown role '{r}' in private_to")
    for n in cfg.get("onboarding", {}).get("default_channels", []):
        if n not in chan_names:
            errors.append(f"onboarding default channel '{n}' is not defined")
    if errors:
        sys.exit("✗ server.yaml:\n  " + "\n  ".join(errors))


# ---------- roles ----------

def sync_roles(d: Discord, gid: str, cfg: dict) -> dict[str, str]:
    print("\n== Roles")
    existing = {r["name"]: r for r in d.req("GET", f"/guilds/{gid}/roles")}
    ids = {"@everyone": gid}
    specs = list(cfg.get("roles", [])) + [r for g in cfg.get("self_roles", []) for r in g["roles"]]
    for spec in specs:
        body = {
            "name": spec["name"],
            "color": color(spec.get("color")),
            "hoist": spec.get("hoist", False),
            "mentionable": spec.get("mentionable", False),
            "permissions": str(sum(PERMS[p] for p in spec.get("permissions", []))),
        }
        cur = existing.get(spec["name"])
        if cur is None:
            ids[spec["name"]] = d.req("POST", f"/guilds/{gid}/roles", body)["id"]
            print(f"  + {spec['name']}")
            continue
        ids[spec["name"]] = cur["id"]
        if cur.get("managed"):
            print(f"  ! {spec['name']} is managed by an integration, skipped")
        elif any(str(cur[k]) != str(v) for k, v in body.items()):
            if d.req("PATCH", f"/guilds/{gid}/roles/{cur['id']}", body, soft=True) is None:
                print(f"  ! {spec['name']}: above or level with the bot role, left as is")
            else:
                print(f"  ~ {spec['name']}")
    return ids


# ---------- channels ----------

def overwrites(spec: dict, gid: str, role_ids: dict, mod_roles: list[str], voice: bool) -> list[dict]:
    acc: dict[str, list[int]] = {}

    def add(rid: str, allow: int = 0, deny: int = 0):
        e = acc.setdefault(rid, [0, 0])
        e[0] |= allow
        e[1] |= deny

    if spec.get("private_to"):
        add(gid, deny=PERMS["view"])
        for r in spec["private_to"]:
            add(role_ids[r], allow=PERMS["view"] | (PERMS["connect"] if voice else 0))
    if spec.get("readonly"):
        add(gid, deny=WRITE)
        for r in mod_roles:
            add(role_ids[r], allow=WRITE)
    return [{"id": k, "type": 0, "allow": str(a), "deny": str(dn)} for k, (a, dn) in acc.items()]


def ow_key(ows: list[dict]) -> set:
    return {(o["id"], int(o["allow"]), int(o["deny"])) for o in ows if o.get("type", 0) == 0}


def plan_channels(cfg: dict) -> list[tuple[dict, list[dict]]]:
    """[(category_spec, [channel_spec...])], static categories + one per self_roles group with channels."""
    out = []
    for cat in cfg.get("categories", []):
        inherited = {"private_to": cat["private_to"]} if cat.get("private_to") else {}
        out.append((cat, [{**inherited, **c} for c in cat["channels"]]))
    for g in cfg.get("self_roles", []):
        chans = [
            {"name": r["channel"], "private_to": [r["name"]],
             "topic": f"{r['name']}" + (f" — {r['description']}" if r.get("description") else "")}
            for r in g["roles"] if r.get("channel")
        ]
        if chans:
            out.append(({"name": g["category"]}, chans))
    return out


def sync_channels(d: Discord, gid: str, cfg: dict, role_ids: dict, community: bool) -> dict[str, str]:
    print("\n== Channels")
    existing = d.req("GET", f"/guilds/{gid}/channels")
    by_key = {(c["name"], c["type"]): c for c in existing}
    mod_roles = cfg.get("moderation_roles", [])
    groups = {g["key"]: g for g in cfg.get("self_roles", [])}
    ids: dict[str, str] = {}

    def upsert(spec: dict, parent_id: str | None) -> str:
        kind = spec.get("type", "text")
        if kind == "announcement" and not community:
            print(f"  ! #{spec['name']}: announcement channels need Community mode, created as text")
            kind = "text"
        t = TYPES[kind]
        body: dict = {
            "name": spec["name"], "type": t,
            "permission_overwrites": overwrites(spec, gid, role_ids, mod_roles, t == TYPES["voice"]),
        }
        if parent_id:
            body["parent_id"] = parent_id
        if t in (TYPES["text"], TYPES["announcement"], TYPES["forum"]):
            body["topic"] = spec.get("topic")
        cur = by_key.get((spec["name"], t))
        if cur is None and t in (TYPES["text"], TYPES["announcement"]):
            other = TYPES["announcement"] if t == TYPES["text"] else TYPES["text"]
            cur = by_key.get((spec["name"], other))
            if cur is not None:
                cur = {**cur, "_convert": True}
        if t == TYPES["forum"]:
            names = list(spec.get("tags", []))
            if spec.get("tags_from"):
                names += [r["name"] for r in groups[spec["tags_from"]]["roles"]]
            if len(names) > MAX_FORUM_TAGS:
                print(f"  ! #{spec['name']}: {len(names)} tags, keeping the first {MAX_FORUM_TAGS}")
                names = names[:MAX_FORUM_TAGS]
            old = {tg["name"]: tg for tg in (cur or {}).get("available_tags", [])}
            body["available_tags"] = [
                {"id": old[n]["id"], "name": n} if n in old else {"name": n} for n in names
            ]
        label = spec["name"] if t == TYPES["category"] else f"#{spec['name']}"
        if cur is None:
            new = d.req("POST", f"/guilds/{gid}/channels", body)
            print(f"  + {label}")
            return new["id"]
        changed = (
            (parent_id and cur.get("parent_id") != parent_id)
            or ("topic" in body and (cur.get("topic") or None) != (body["topic"] or None))
            or ow_key(cur.get("permission_overwrites", [])) != ow_key(body["permission_overwrites"])
            or ("available_tags" in body
                and [tg["name"] for tg in cur.get("available_tags", [])] != [tg["name"] for tg in body["available_tags"]])
        )
        if changed or cur.get("_convert"):
            if not cur.get("_convert"):
                body.pop("type")
            d.req("PATCH", f"/channels/{cur['id']}", body)
            print(f"  ~ {label}")
        return cur["id"]

    for cat, chans in plan_channels(cfg):
        cat_spec = {"name": cat["name"], "type": "category", "private_to": cat.get("private_to")}
        cat_id = upsert(cat_spec, None)
        for c in chans:
            ids[c["name"]] = upsert(c, cat_id)
    return ids


# ---------- community ----------

def enable_community(d: Discord, guild: dict, chan_ids: dict, cfg: dict) -> None:
    c = cfg.get("community", {})
    print("\n== Community")
    d.req("PATCH", f"/guilds/{guild['id']}", {
        "features": sorted(set(guild.get("features", [])) | {"COMMUNITY"}),
        "verification_level": max(1, guild.get("verification_level", 0)),  # verified e-mail
        "explicit_content_filter": 2,  # scan media from all members
        "rules_channel_id": chan_ids[c.get("rules_channel", "rules")],
        "public_updates_channel_id": chan_ids[c.get("updates_channel", "mod-chat")],
    })
    print("  + Community mode enabled")


# ---------- automod ----------

AUTOMOD_TRIGGERS = {"keyword": 1, "spam": 3, "preset": 4, "mention": 5}
AUTOMOD_PRESETS = {"profanity": 1, "sexual": 2, "slurs": 3}


def sync_automod(d: Discord, gid: str, cfg: dict, role_ids: dict, chan_ids: dict) -> None:
    am = cfg.get("automod")
    if not am:
        return
    print("\n== AutoMod")
    existing = {r["name"]: r for r in d.req("GET", f"/guilds/{gid}/auto-moderation/rules") or []}
    alert = chan_ids[am["alert_channel"]]
    for rule in am["rules"]:
        meta: dict = {}
        if rule.get("keywords"):
            meta["keyword_filter"] = rule["keywords"]
        if rule.get("regex"):
            meta["regex_patterns"] = rule["regex"]
        if rule.get("presets"):
            meta["presets"] = [AUTOMOD_PRESETS[p] for p in rule["presets"]]
        if rule["trigger"] == "mention":
            meta["mention_total_limit"] = rule.get("mention_limit", 6)
            meta["mention_raid_protection_enabled"] = rule.get("raid_protection", True)
        actions = []
        for a in rule["actions"]:
            name, arg = (next(iter(a.items())) if isinstance(a, dict) else (a, None))
            if name == "block":
                actions.append({"type": 1, "metadata": {"custom_message": arg} if arg else {}})
            elif name == "alert":
                actions.append({"type": 2, "metadata": {"channel_id": alert}})
            elif name == "timeout":
                actions.append({"type": 3, "metadata": {"duration_seconds": int(arg)}})
        body = {
            "name": rule["name"],
            "event_type": 1,  # message send
            "trigger_type": AUTOMOD_TRIGGERS[rule["trigger"]],
            "trigger_metadata": meta,
            "actions": actions,
            "enabled": True,
            "exempt_roles": [role_ids[r] for r in am.get("exempt_roles", [])],
            "exempt_channels": [chan_ids[c] for c in rule.get("exempt_channels", [])],
        }
        cur = existing.get(rule["name"])
        if cur is None:
            d.req("POST", f"/guilds/{gid}/auto-moderation/rules", body)
            print(f"  + {rule['name']}")
        else:
            body.pop("trigger_type")  # immutable
            d.req("PATCH", f"/guilds/{gid}/auto-moderation/rules/{cur['id']}", body)
            print(f"  ~ {rule['name']}")


def render(text: str, role_ids: dict, chan_ids: dict) -> str:
    import re
    def sub(m):
        kind, name = m.group(1), m.group(2)
        return f"<#{chan_ids[name]}>" if kind == "channel" else f"<@&{role_ids[name]}>"
    return re.sub(r"\{(channel|role):([^}]+)\}", sub, text).strip()


def sync_messages(d: Discord, cfg: dict, role_ids: dict, chan_ids: dict) -> None:
    msgs = cfg.get("messages")
    if not msgs:
        return
    print("\n== Messages")
    me = d.req("GET", "/users/@me")["id"]
    for chan, text in msgs.items():
        content = render(text, role_ids, chan_ids)
        if len(content) > 2000:
            sys.exit(f"✗ message for #{chan} is {len(content)} chars (max 2000)")
        cid = chan_ids[chan]
        mine = [m for m in (d.req("GET", f"/channels/{cid}/messages?limit=50") or []) if m["author"]["id"] == me]
        body = {"content": content, "allowed_mentions": {"parse": []}, "flags": 4}  # 4 = no link embeds
        if not mine:
            d.req("POST", f"/channels/{cid}/messages", body)
            print(f"  + #{chan}")
        elif mine[-1]["content"] != content:
            d.req("PATCH", f"/channels/{cid}/messages/{mine[-1]['id']}", body)
            print(f"  ~ #{chan}")


def ensure_gfi_webhooks(d: Discord, cfg: dict, chan_ids: dict) -> None:
    """One webhook per feed destination (digest + each language channel), saved as JSON in .env (GFI_WEBHOOKS)."""
    import json
    feed = cfg.get("gfi_feed")
    if not feed:
        return
    lang_chans = {r["name"]: r["channel"] for g in cfg["self_roles"] for r in g["roles"] if r.get("channel")}
    dests = [feed["channel"]] + [lang_chans[role] for role in feed["languages"] if role in lang_chans]
    current = json.loads(os.environ.get("GFI_WEBHOOKS") or "{}")
    missing = [c for c in dests if c not in current]
    if not missing:
        return
    print("\n== Feed webhooks")
    for chan in missing:
        cid = chan_ids[chan]
        hooks = d.req("GET", f"/channels/{cid}/webhooks") or []
        hook = next((h for h in hooks if h.get("name") == feed["webhook_name"] and h.get("token")), None)
        if hook is None:
            hook = d.req("POST", f"/channels/{cid}/webhooks", {"name": feed["webhook_name"]})
            print(f"  + #{chan}")
        if not d.dry_run:
            current[chan] = f"https://discord.com/api/webhooks/{hook['id']}/{hook['token']}"
    if d.dry_run:
        return
    env = HERE / ".env"
    lines = [l for l in env.read_text().splitlines() if not l.startswith(("GFI_WEBHOOKS=", "GFI_WEBHOOK_URL="))]
    lines.append("GFI_WEBHOOKS=" + json.dumps(current, separators=(",", ":")))
    env.write_text("\n".join(lines) + "\n")
    os.environ["GFI_WEBHOOKS"] = lines[-1].split("=", 1)[1]
    print(f"  {len(current)} webhook URLs written to .env (GFI_WEBHOOKS)")


def make_invite(d: Discord, chan_id: str) -> None:
    inv = d.req("POST", f"/channels/{chan_id}/invites", {"max_age": 0, "max_uses": 0, "unique": False})
    print(f"\nPermanent invite: https://discord.gg/{inv.get('code')}")


# ---------- onboarding ----------

def sync_onboarding(d: Discord, gid: str, cfg: dict, role_ids: dict, chan_ids: dict) -> None:
    print("\n== Onboarding")
    cur = d.req("GET", f"/guilds/{gid}/onboarding")
    old_prompts = {p["title"]: p for p in cur.get("prompts", [])}
    prompts = []
    chunks = []
    for g in cfg.get("self_roles", []):
        parts = [g["roles"][i:i + MAX_PROMPT_OPTIONS] for i in range(0, len(g["roles"]), MAX_PROMPT_OPTIONS)]
        for n, part in enumerate(parts, 1):
            title = g["prompt"] if n == 1 else f"{g['prompt']} (more)"
            # overflow chunks are not shown at join, only in Channels & Roles
            chunks.append({**g, "prompt": title, "roles": part, "in_onboarding": g.get("in_onboarding", True) and n == 1})
    joined = [c for c in chunks if c["in_onboarding"]]
    if len(joined) > MAX_JOIN_PROMPTS:
        sys.exit(f"✗ {len(joined)} questions at join, Discord accepts {MAX_JOIN_PROMPTS}: set in_onboarding: false on some groups")
    for g in chunks:
        old = old_prompts.get(g["prompt"], {})
        old_opts = {o["title"]: o for o in old.get("options", [])}
        options = []
        for r in g["roles"]:
            opt = {
                "id": old_opts.get(r["name"], {}).get("id") or snowflake(),
                "title": r["name"],
                "description": r.get("description"),
                "role_ids": [role_ids[r["name"]]],
                "channel_ids": [],
            }
            if r.get("emoji"):
                opt["emoji_name"] = r["emoji"]
            options.append(opt)
        prompts.append({
            "id": old.get("id") or snowflake(),
            "type": 0,  # multiple choice
            "title": g["prompt"],
            "single_select": g.get("single_select", False),
            "required": g.get("required", False),
            "in_onboarding": g.get("in_onboarding", True),
            "options": options,
        })
        print(f"  • {g['prompt']} ({len(options)} options)")
    body = {
        "prompts": prompts,
        "default_channel_ids": [chan_ids[n] for n in cfg["onboarding"]["default_channels"]],
        "enabled": True,
        "mode": 0,
    }
    d.req("PUT", f"/guilds/{gid}/onboarding", body)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "server.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="print changes without applying them")
    ap.add_argument("--invite", metavar="CHANNEL", nargs="?", const="rules",
                    help="print a permanent invite link to CHANNEL (default: rules)")
    ap.add_argument("--enable-community", action="store_true",
                    help="turn on Community mode (needed for onboarding and announcement channels)")
    args = ap.parse_args()

    load_env()
    token, gid = os.environ.get("DISCORD_TOKEN"), os.environ.get("GUILD_ID")
    if not token or not gid:
        sys.exit("✗ DISCORD_TOKEN and GUILD_ID are required (see .env.example)")

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    validate(cfg)

    d = Discord(token, args.dry_run)
    guild = d.req("GET", f"/guilds/{gid}")
    community = "COMMUNITY" in guild.get("features", [])
    print(f"Server: {guild['name']}{'  [DRY RUN]' if args.dry_run else ''}")
    if not community and not args.enable_community:
        print("! Community mode is off: onboarding will be skipped. Re-run with --enable-community.")

    role_ids = sync_roles(d, gid, cfg)
    chan_ids = sync_channels(d, gid, cfg, role_ids, community)
    if not community and args.enable_community:
        enable_community(d, guild, chan_ids, cfg)
        community = True
        if not args.dry_run:  # second pass: text -> announcement channels
            chan_ids = sync_channels(d, gid, cfg, role_ids, community)
    if community:
        sync_onboarding(d, gid, cfg, role_ids, chan_ids)
    sync_automod(d, gid, cfg, role_ids, chan_ids)
    sync_messages(d, cfg, role_ids, chan_ids)
    ensure_gfi_webhooks(d, cfg, chan_ids)
    if args.invite:
        make_invite(d, chan_ids[args.invite])
    print("\n✓ Done" + (" (nothing was changed)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
