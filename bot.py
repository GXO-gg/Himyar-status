"""Himyar Status Bot — private monitoring for the Himyar bot suite.

Two views:
  LIGHT — online/offline, uptime, last restart.        -> main Himyar server
  FULL  — the above plus per-bot server lists/reach.   -> private server only

Both are single messages that get edited in place, so the channels stay clean.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import aiohttp
import discord
from discord.ext import tasks
from dotenv import load_dotenv

from collector import BotStatus, collect, discover_bots

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("himyar-status")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def _int(name: str, default: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    return [p.strip() for p in (os.getenv(name) or "").split(",") if p.strip()]


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
BOTS_DIR = os.getenv("BOTS_DIR", "/home/himyar/bots")
MONITOR_BOTS = _list("MONITOR_BOTS")
EXCLUDE_BOTS = _list("EXCLUDE_BOTS") or ["himyar-status"]

LIGHT_GUILD_ID = _int("LIGHT_GUILD_ID")
LIGHT_CHANNEL_ID = _int("LIGHT_CHANNEL_ID")
LIGHT_CHANNEL_NAME = os.getenv("LIGHT_CHANNEL_NAME", "").strip().lstrip("#")

FULL_GUILD_ID = _int("FULL_GUILD_ID")
FULL_CHANNEL_ID = _int("FULL_CHANNEL_ID")
FULL_CHANNEL_NAME = os.getenv("FULL_CHANNEL_NAME", "").strip().lstrip("#")

REFRESH_SECONDS = max(30, _int("REFRESH_SECONDS", 60))
GUILD_REFRESH_SECONDS = max(60, _int("GUILD_REFRESH_SECONDS", 300))
ALERT_USER_ID = _int("ALERT_USER_ID")
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

COLOR_OK = 0x2ECC71
COLOR_WARN = 0xE67E22
COLOR_DOWN = 0xE74C3C

# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def human_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def ts(epoch: float | None, style: str = "R") -> str:
    if not epoch:
        return "unknown"
    return f"<t:{int(epoch)}:{style}>"


def dot(status: BotStatus) -> str:
    if not status.loaded:
        return "⚪"
    if status.active:
        return "🟢"
    return "🔴"


def overall_color(statuses: list[BotStatus]) -> int:
    if not statuses:
        return COLOR_WARN
    down = [s for s in statuses if not s.active]
    if not down:
        return COLOR_OK
    if len(down) == len(statuses):
        return COLOR_DOWN
    return COLOR_WARN


def light_embed(statuses: list[BotStatus]) -> discord.Embed:
    lines = []
    for s in statuses:
        if s.active:
            lines.append(
                f"{dot(s)} **{s.name}** — up {human_uptime(s.uptime)} · "
                f"restarted {ts(s.started_at)}"
            )
        elif not s.loaded:
            lines.append(f"{dot(s)} **{s.name}** — no systemd unit found")
        else:
            since = ts(s.stopped_at) if s.stopped_at else "unknown"
            lines.append(f"{dot(s)} **{s.name}** — OFFLINE ({s.sub_state}) · since {since}")

    up = sum(1 for s in statuses if s.active)
    embed = discord.Embed(
        title="Himyar Bot Status",
        description="\n".join(lines) or "No bots configured.",
        color=overall_color(statuses),
    )
    embed.add_field(name="Summary", value=f"{up}/{len(statuses)} bots online", inline=False)
    embed.set_footer(text="Updated every minute")
    embed.timestamp = discord.utils.utcnow()
    return embed


def _guild_line(status: BotStatus, max_named: int = 6) -> str:
    if status.guild_error:
        return f"Servers: ⚠️ {status.guild_error}"
    if not status.guilds:
        return "Servers: 0"
    named = []
    for g in status.guilds[:max_named]:
        count = f" ({g.members:,})" if g.members else ""
        named.append(f"{g.name}{count}")
    extra = len(status.guilds) - len(named)
    tail = f" · +{extra} more" if extra > 0 else ""
    return (
        f"Servers: **{len(status.guilds)}** · reach {status.member_reach:,} members\n"
        f"{' · '.join(named)}{tail}"
    )


def full_embed(statuses: list[BotStatus]) -> discord.Embed:
    embed = discord.Embed(
        title="Himyar Bot Health — Full View",
        description="Private. Includes per-bot server lists and reach.",
        color=overall_color(statuses),
    )

    for s in statuses:
        if s.active:
            head = f"Uptime {human_uptime(s.uptime)} · {s.restarts} restarts · restarted {ts(s.started_at)}"
        elif not s.loaded:
            head = "No systemd unit found"
        else:
            head = f"**OFFLINE** ({s.sub_state}) · since {ts(s.stopped_at)}"
        value = f"{head}\n{_guild_line(s)}"
        embed.add_field(name=f"{dot(s)} {s.name}", value=value[:1024], inline=False)

    unique: dict[str, int] = {}
    for s in statuses:
        for g in s.guilds:
            unique[g.id] = g.members or 0
    installs = sum(len(s.guilds) for s in statuses)
    up = sum(1 for s in statuses if s.active)

    embed.add_field(
        name="Totals",
        value=(
            f"{up}/{len(statuses)} bots online\n"
            f"{installs} total installs across {len(unique)} unique servers\n"
            f"Combined unique reach: {sum(unique.values()):,} members"
        ),
        inline=False,
    )
    embed.set_footer(text="Updated every minute · server data every few minutes")
    embed.timestamp = discord.utils.utcnow()
    return embed


# --------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------


class StatusBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.session: aiohttp.ClientSession | None = None
        self.state: dict = self._load_state()
        self.guild_cache: dict[str, tuple] = {}
        self.last_guild_refresh = 0.0
        self.previous_active: dict[str, bool] = {}
        self.started = False

    # -- state -------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            STATE_FILE.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not write state file: %s", exc)

    # -- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        for guild in self.guilds:
            log.info("In server: %s (%s)", guild.name, guild.id)
        if not self.started:
            self.started = True
            self.refresh.start()

    # -- channel resolution ------------------------------------------------

    def resolve_channel(
        self, guild_id: int, channel_id: int, channel_name: str
    ) -> discord.TextChannel | None:
        if channel_id:
            channel = self.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel
            log.warning("Channel id %s not visible to the bot", channel_id)
            return None
        guild = self.get_guild(guild_id) if guild_id else None
        if guild and channel_name:
            match = discord.utils.get(guild.text_channels, name=channel_name)
            if match:
                return match
            log.warning("No channel named '%s' in %s", channel_name, guild.name)
        return None

    async def ensure_message(
        self, key: str, channel: discord.TextChannel
    ) -> discord.Message | None:
        entry = self.state.get(key) or {}
        if entry.get("channel_id") == channel.id and entry.get("message_id"):
            try:
                return await channel.fetch_message(int(entry["message_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.info("Stored %s message is gone, posting a new one", key)

        try:
            message = await channel.send(embed=discord.Embed(description="Starting up…"))
        except discord.Forbidden:
            log.error("Missing permission to post in #%s", channel.name)
            return None
        try:
            await message.pin()
        except discord.HTTPException:
            log.info("Could not pin the %s message (not fatal)", key)

        self.state[key] = {"channel_id": channel.id, "message_id": message.id}
        self._save_state()
        return message

    # -- alerts ------------------------------------------------------------

    async def maybe_alert(self, statuses: list[BotStatus]) -> None:
        if not ALERT_USER_ID:
            return
        changed = [
            s
            for s in statuses
            if s.name in self.previous_active and self.previous_active[s.name] != s.active
        ]
        if changed:
            lines = [
                f"{'🟢 back online' if s.active else '🔴 WENT DOWN'}: **{s.name}**"
                for s in changed
            ]
            try:
                user = await self.fetch_user(ALERT_USER_ID)
                await user.send("\n".join(lines))
            except discord.HTTPException as exc:
                log.warning("Alert DM failed: %s", exc)
        self.previous_active = {s.name: s.active for s in statuses}

    # -- main loop ---------------------------------------------------------

    @tasks.loop(seconds=REFRESH_SECONDS)
    async def refresh(self) -> None:
        targets = discover_bots(BOTS_DIR, include=MONITOR_BOTS or None, exclude=EXCLUDE_BOTS)
        if not targets:
            log.warning("No bots discovered in %s", BOTS_DIR)

        due = (time.time() - self.last_guild_refresh) >= GUILD_REFRESH_SECONDS
        statuses = await collect(targets, session=self.session, include_guilds=due)

        if due:
            self.guild_cache = {
                s.name: (s.guilds, s.guild_error, s.guilds_checked_at) for s in statuses
            }
            self.last_guild_refresh = time.time()
        else:
            for s in statuses:
                cached = self.guild_cache.get(s.name)
                if cached:
                    s.guilds, s.guild_error, s.guilds_checked_at = cached

        await self.publish("light", light_embed(statuses))
        await self.publish("full", full_embed(statuses))
        await self.maybe_alert(statuses)

    async def publish(self, key: str, embed: discord.Embed) -> None:
        if key == "light":
            channel = self.resolve_channel(LIGHT_GUILD_ID, LIGHT_CHANNEL_ID, LIGHT_CHANNEL_NAME)
        else:
            channel = self.resolve_channel(FULL_GUILD_ID, FULL_CHANNEL_ID, FULL_CHANNEL_NAME)
            # Safety rail: the full view must never land on the public Himyar server.
            if channel and LIGHT_GUILD_ID and channel.guild.id == LIGHT_GUILD_ID:
                log.error(
                    "REFUSING to post the full health view in %s — that is the light-view server.",
                    channel.guild.name,
                )
                return
        if channel is None:
            return
        message = await self.ensure_message(key, channel)
        if message is None:
            return
        try:
            await message.edit(content=None, embed=embed)
        except discord.HTTPException as exc:
            log.warning("Could not edit the %s message: %s", key, exc)

    @refresh.before_loop
    async def before_refresh(self) -> None:
        await self.wait_until_ready()


def main() -> None:
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing from .env")
    StatusBot().run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
