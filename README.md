# Himyar Status Bot

Private monitoring for the Himyar bot suite. Single-server-owner tool — no per-guild
config system, no public invite.

## What it does

Every 60 seconds it edits two pinned messages:

| View | Contents | Where |
|---|---|---|
| **Light** | online/offline, current uptime, last restart | channel on the main Himyar server, every 60s |
| **Full** | the above + servers each bot is in, member reach, totals | DM to the owner, hourly |

`/health` returns the full view on demand, visible only to the owner.

## How it gets the data

No changes to the other bots.

1. **Liveness / uptime / restarts** — `systemctl show` on each bot's unit
   (`ActiveState`, `ActiveEnterTimestamp`, `ActiveExitTimestamp`, `NRestarts`).
   Read-only, no sudo needed.
2. **Server lists** — each bot's token is read from its own `.env` and used for a
   single `GET /users/@me/guilds?with_counts=true` against the Discord API,
   refreshed hourly (and on `/health`).

Because of (2) this bot's host directory holds read access to every bot token on
the box. Keep `.env` at `chmod 600` and the repo free of it.

## Safety rail

The full view defaults to DM, so the sensitive data never touches a server. If
`FULL_VIEW_MODE=channel` is used instead and that channel resolves to the same
server as `LIGHT_GUILD_ID`, the post is refused and logged.

## Layout

```
bot.py                  Discord client, the two dashboards, alerting
collector.py            systemd + Discord API reads (no writes anywhere)
.env.example            config template
himyar-status.service   systemd unit
```

## Deploy (Hetzner)

```bash
cd ~/bots
git clone https://github.com/GXO-gg/himyar-status.git
cd himyar-status
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env && nano .env
venv/bin/python bot.py          # foreground test, Ctrl+C when the dashboards appear
sudo cp himyar-status.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now himyar-status
systemctl status himyar-status
```

## Permissions needed

In the main server: View Channel, Send Messages, Embed Links, Read Message
History, Manage Messages (to pin). No privileged intents.

`/health` additionally needs the bot invited with the `applications.commands`
scope. Without it the DM dashboard still works; only the command is missing.
