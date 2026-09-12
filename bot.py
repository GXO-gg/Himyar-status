"""Himyar Status Bot — private monitoring for the Himyar bot suite.

Two views, deliberately split:

  LIGHT  every 60s   -> a channel on the main Himyar server
                        online/offline, uptime, last restart
  FULL   every hour  -> a DM to the owner (or a channel, if configured)
                        the above plus each bot's server list and reach

  /health            -> the full view on demand, private to the owner

Both dashboards are a single message that gets edited in place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
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
    try:
        return int((os.getenv(name) or "").strip())
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"} if raw else default


def _list(name: str) -> list[str]:
    return [p.strip() for p in (os.getenv(name) or "").split(",") if p.strip()]


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
BOTS_DIR = os.getenv("BOTS_DIR", "/home/himyar/bots")
MONITOR_BOTS = _list("MONITOR_BOTS")
EXCLUDE_BOTS = _list("EXCLUDE_BOTS") or ["himyar-status"]

OWNER_USER_ID = _int("OWNER_USER_ID")

LIGHT_GUILD_ID = _int("LIGHT_GUILD_ID")
LIGHT_CHANNEL_ID = _int("LIGHT_CHANNEL_ID")
LIGHT_CHANNEL_NAME = os.getenv("LIGHT_CHANNEL_NAME", "").strip().lstrip("#")

FULL_VIEW_MODE = (os.getenv("FULL_VIEW_MODE", "dm").strip().lower() or "dm")
FULL_GUILD_ID = _int("FULL_GUILD_ID")
FULL_CHANNEL_ID = _int("FULL_CHANNEL_ID")
FULL_CHANNEL_NAME = os.getenv("FULL_CHANNEL_NAME", "").strip().lstrip("#")

LIGHT_REFRESH_SECONDS = max(30, _int("LIGHT_REFRESH_SECONDS", 60))
FULL_REFRESH_SECONDS = max(300, _int("FULL_REFRESH_SECONDS", 3600))
ALERT_DM = _bool("ALERT_DM", False)
ENABLE_HEALTH_COMMAND = _bool("ENABLE_HEALTH_COMMAND", True)
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

COLOR_OK = 0x2ECC71
COLOR_WARN = 0xE67E22
COLOR_DOWN = 0xE74C3C

# --------------------------------------------------------------------------
# Formatting
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
    return f"<t:{int(epoch)}:{style}>" if epoch else "unknown"


def dot(status: BotStatus) -> str:
    if not status.loaded:
        return "⚪"
    return "🟢" if status.active else "🔴"


def overall_color(statuses: list[BotStatus]) -> int:
    down = [s for s in statuses if not s.active]
    if not statuses:
        return COLOR_WARN
    if not down:
        return COLOR_OK
    return COLOR_DOWN if len(down) == len(statuses) else COLOR_WARN


def light_embed(statuses: list[BotStatus]) -> discord.Embed:
    lines = []
    for s in statuses:
        if s.active:
            lines.append(
                f"{dot(s)} **{s.name}** — up {human_uptime(s.uptime)} · restarted {ts(s.started_at)}"
            )
        elif not s.loaded:
            lines.append(f"{dot(s)} **{s.name}** — no systemd unit found")
        else:
            lines.append(
                f"{dot(s)} **{s.name}** — OFFLINE ({s.sub_state}) · since {ts(s.stopped_at)}"
            )

    up = sum(1 for s in statuses if s.active)
    embed = discord.Embed(
        title="Himyar Bot Status",
        description="\n".join(lines) or "No bots configured.",
        color=overall_color(statuses),
    )
    embed.add_field(name="Summary", value=f"{up}/{len(statuses)} bots online", inline=False)
    embed.set_footer(text="Refreshes every minute")
    embed.timestamp = discord.utils.utcnow()
    return embed


def _guild_line(status: BotStatus, max_named: int = 6) -> str:
    if status.guild_error:
        return f"Servers: ⚠️ {status.guild_error}"
    if not status.guilds:
        return "Servers: 0"
    named = [
        f"{g.name}{f' ({g.members:,})' if g.members else ''}"
        for g in status.guilds[:max_named]
    ]
    extra = len(status.guilds) - len(named)
    tail = f" · +{extra} more" if extra > 0 else ""
    return (
        f"Servers: **{len(status.guilds)}** · reach {status.member_reach:,} members\n"
        f"{' · '.join(named)}{tail}"
    )


def full_embed(statuses: list[BotStatus], on_demand: bool = False) -> discord.Embed:
    embed = discord.Embed(
        title="Himyar Bot Health — Full View",
        description="Private. Per-bot server lists and total reach.",
        color=overall_color(statuses),
    )

    for s in statuses:
        if s.active:
            head = f"Uptime {human_uptime(s.uptime)} · {s.restarts} restarts · restarted {ts(s.started_at)}"
        elif not s.loaded:
            head = "No systemd unit found"
        else:
            head = f"**OFFLINE** ({s.sub_state}) · since {ts(s.stopped_at)}"
        embed.add_field(name=f"{dot(s)} {s.name}", value=f"{head}\n{_guild_line(s)}"[:1024], inline=False)

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
    embed.set_footer(
        text="On demand via /health" if on_demand else f"Refreshes every {FULL_REFRESH_SECONDS // 60} min"
    )
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
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None
        self.state: dict = self._load_state()
        self.previous_active: dict[str, bool] = {}
        self.loops_started = False

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
        if ENABLE_HEALTH_COMMAND:
            await self._register_commands()

    async def _register_commands(self) -> None:
        @self.tree.command(name="health", description="Full Himyar bot health report (private)")
        async def health(interaction: discord.Interaction) -> None:
            if OWNER_USER_ID and interaction.user.id != OWNER_USER_ID:
                await interaction.response.send_message(
                    "This command is owner-only.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            statuses = await self.gather(include_guilds=True)
            await interaction.followup.send(
                embed=full_embed(statuses, on_demand=True), ephemeral=True
            )

        if not LIGHT_GUILD_ID:
            return
        guild = discord.Object(id=LIGHT_GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
            log.info("/health registered in guild %s", LIGHT_GUILD_ID)
        except discord.HTTPException as exc:
            log.warning(
                "Could not register /health (re-invite the bot with the "
                "applications.commands scope): %s",
                exc,
            )

    async def close(self) -> None:
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        for guild in self.guilds:
            log.info("In server: %s (%s)", guild.name, guild.id)
        if not self.loops_started:
            self.loops_started = True
            self.refresh_light.start()
            self.refresh_full.start()

    # -- collection --------------------------------------------------------

    async def gather(self, include_guilds: bool) -> list[BotStatus]:
        targets = discover_bots(BOTS_DIR, include=MONITOR_BOTS or None, exclude=EXCLUDE_BOTS)
        if not targets:
            log.warning("No bots discovered in %s", BOTS_DIR)
        return await collect(targets, session=self.session, include_guilds=include_guilds)

    # -- destinations ------------------------------------------------------

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

    async def full_destination(self) -> discord.abc.Messageable | None:
        if FULL_VIEW_MODE == "dm":
            if not OWNER_USER_ID:
                log.error("FULL_VIEW_MODE=dm but OWNER_USER_ID is not set")
                return None
            try:
                user = await self.fetch_user(OWNER_USER_ID)
                return user.dm_channel or await user.create_dm()
            except discord.HTTPException as exc:
                log.warning("Could not open a DM: %s", exc)
                return None

        channel = self.resolve_channel(FULL_GUILD_ID, FULL_CHANNEL_ID, FULL_CHANNEL_NAME)
        # Safety rail: the full view must never land on the public Himyar server.
        if channel and LIGHT_GUILD_ID and channel.guild.id == LIGHT_GUILD_ID:
            log.error(
                "REFUSING to post the full health view in %s — that is the light-view server.",
                channel.guild.name,
            )
            return None
        return channel

    async def ensure_message(
        self, key: str, channel: discord.abc.Messageable
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
            log.error("Missing permission to post the %s view", key)
            return None
        try:
            await message.pin()
        except discord.HTTPException:
            log.info("Could not pin the %s message (not fatal)", key)

        self.state[key] = {"channel_id": channel.id, "message_id": message.id}
        self._save_state()
        return message

    async def publish(self, key: str, channel: discord.abc.Messageable, embed: discord.Embed) -> None:
        message = await self.ensure_message(key, channel)
        if message is None:
            return
        try:
            await message.edit(content=None, embed=embed)
        except discord.HTTPException as exc:
            log.warning("Could not edit the %s message: %s", key, exc)

    # -- alerts ------------------------------------------------------------

    async def maybe_alert(self, statuses: list[BotStatus]) -> None:
        if not (ALERT_DM and OWNER_USER_ID):
            self.previous_active = {s.name: s.active for s in statuses}
            return
        changed = [
            s for s in statuses
            if s.name in self.previous_active and self.previous_active[s.name] != s.active
        ]
        if changed:
            lines = [
                f"{'🟢 back online' if s.active else '🔴 WENT DOWN'}: **{s.name}**"
                for s in changed
            ]
            try:
                user = await self.fetch_user(OWNER_USER_ID)
                await user.send("\n".join(lines))
            except discord.HTTPException as exc:
                log.warning("Alert DM failed: %s", exc)
        self.previous_active = {s.name: s.active for s in statuses}

    # -- loops -------------------------------------------------------------

    @tasks.loop(seconds=LIGHT_REFRESH_SECONDS)
    async def refresh_light(self) -> None:
        statuses = await self.gather(include_guilds=False)
        channel = self.resolve_channel(LIGHT_GUILD_ID, LIGHT_CHANNEL_ID, LIGHT_CHANNEL_NAME)
        if channel:
            await self.publish("light", channel, light_embed(statuses))
        await self.maybe_alert(statuses)

    @tasks.loop(seconds=FULL_REFRESH_SECONDS)
    async def refresh_full(self) -> None:
        destination = await self.full_destination()
        if destination is None:
            return
        statuses = await self.gather(include_guilds=True)
        await self.publish("full", destination, full_embed(statuses))

    @refresh_light.before_loop
    @refresh_full.before_loop
    async def before_loops(self) -> None:
        await self.wait_until_ready()


def main() -> None:
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing from .env")
    StatusBot().run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
