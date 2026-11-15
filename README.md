# oss-discord-setup

Builds an international open-source Discord server from `server.yaml`: 45 roles, 50 channels, 4 onboarding questions + 1 in Channels & Roles.
Idempotent: edit the YAML, re-run, it updates in place.

## Setup (once)

1. Create the server → Server Settings → **Enable Community** (let Discord create `#rules` and `#moderator-only`; the script reuses `#rules`).
2. https://discord.com/developers/applications → New Application → **Bot** → Reset Token → copy it.
3. Invite it with Administrator (remove the bot once setup is done):
   `https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot&permissions=8`
4. Discord → Settings → Advanced → Developer Mode on → right-click the server → Copy Server ID.
5. `cp .env.example .env` and fill `DISCORD_TOKEN` + `GUILD_ID`.

## Run

```sh
uv run setup.py --dry-run   # preview
uv run setup.py             # apply
```

## Notes

- Roles are created in YAML order; earlier = higher. Static roles > involvement > languages... so name color follows that order.
- The bot can only edit roles below its own: if a role update fails, drag the bot role to the top.
- Onboarding needs ≥ 7 default channels, ≥ 5 where @everyone can post. Max 12 options per question, max 4 questions at join (5 rejected); overflow options go to a "(more)" question in Channels & Roles.
- `--enable-community` turns on Community mode (verification: verified e-mail, media filter: all members).
- Language/field/lounge channels are private to their role: members only see what they picked (Browse Channels to change).
- AutoMod rules and the #rules message are managed from server.yaml. Not handled: Server Guide.

## Good first issues feed

`gfi_feed.py` posts fresh `good first issue` issues from established repos (≥ `min_stars`, recently pushed, no assignee, no linked PR) to `#good-first-issues`, every 6 hours via `.github/workflows/good-first-issues.yml`. Config: `gfi_feed` in `server.yaml`. Posted URLs are kept in `gfi_posted.json`.

Each issue goes to its language channel (only that role sees it, no ping); a digest goes to `#good-first-issues` with an opt-in ping.

Repo secret needed: `DISCORD_WEBHOOKS` (see below).

```sh
GITHUB_TOKEN=$(gh auth token) uv run gfi_feed.py --dry-run
```

## RSS feeds

`rss_feed.py` posts new items from the feeds in `rss` (server.yaml) every 2 hours via `.github/workflows/rss.yml`. Default: AI news (labs, Hugging Face, newsletters, Hacker News, releases of Ollama/vLLM/Transformers/MCP) into `#ai-news`. Pre-releases are skipped; a new feed only posts its latest item. Seen items: `rss_seen.json`.

Repo secret: `DISCORD_WEBHOOKS` (JSON `{channel: webhook URL}`, written to `.env` by `setup.py`), shared by both feeds.
