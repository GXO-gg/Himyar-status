"""Data collection for the Himyar Status Bot.

Two sources, neither of which requires touching the other bots:

1. systemd  -> is the unit running, when did it last start, how many restarts
2. Discord  -> each bot's own token is read from its .env, and we ask Discord
               "which guilds is this bot in?" as that bot

Nothing here writes anything. It only reads.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import aiohttp

API = "https://discord.com/api/v10"

# .env key names used across the Himyar bots, in order of preference.
TOKEN_KEYS = (
    "DISCORD_TOKEN",
    "BOT_TOKEN",
    "TOKEN",
    "DISCORD_BOT_TOKEN",
    "CLIENT_TOKEN",
)

SYSTEMD_PROPS = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "ActiveEnterTimestamp",
    "ActiveExitTimestamp",
    "NRestarts",
)


@dataclass
class GuildInfo:
    id: str
    name: str
    members: int | None = None


@dataclass
class BotTarget:
    """A bot we intend to monitor."""

    name: str
    unit: str
    directory: Path
    token: str | None = None


@dataclass
class BotStatus:
    name: str
    unit: str
    loaded: bool = False
    active: bool = False
    sub_state: str = "unknown"
    started_at: float | None = None
    stopped_at: float | None = None
    restarts: int = 0
    token_found: bool = False
    guilds: list[GuildInfo] = field(default_factory=list)
    guild_error: str | None = None
    guilds_checked_at: float | None = None

    @property
    def uptime(self) -> float | None:
        if self.active and self.started_at:
            return max(0.0, time.time() - self.started_at)
        return None

    @property
    def member_reach(self) -> int:
        return sum(g.members or 0 for g in self.guilds)


# --------------------------------------------------------------------------
# .env reading / bot discovery
# --------------------------------------------------------------------------


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser. Never raises."""
    data: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return data
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        data[key.strip()] = value
    return data


def discover_bots(
    bots_dir: str | Path,
    include: Sequence[str] | None = None,
    exclude: Iterable[str] = (),
) -> list[BotTarget]:
    """Find bots to monitor.

    If `include` is given, that list wins (and order is preserved).
    Otherwise every direct subdirectory of `bots_dir` is treated as a bot.

    An entry may be plain (`himyar-tickets`, unit assumed to be
    `himyar-tickets.service`) or an explicit mapping for the cases where the
    folder and the systemd unit are named differently:

        himyar-warden=himyar-welcome-bot
    """
    base = Path(bots_dir)
    excluded = {e.strip() for e in exclude if e.strip()}

    if include:
        entries = [n.strip() for n in include if n.strip()]
    else:
        try:
            entries = sorted(p.name for p in base.iterdir() if p.is_dir())
        except OSError:
            entries = []

    targets: list[BotTarget] = []
    for entry in entries:
        folder, _, override = entry.partition("=")
        folder = folder.strip()
        if not folder or folder in excluded:
            continue
        unit = (override.strip() or folder)
        if not unit.endswith(".service"):
            unit += ".service"
        directory = base / folder
        env = read_env_file(directory / ".env")
        token = next((env[k] for k in TOKEN_KEYS if env.get(k)), None)
        targets.append(
            BotTarget(
                name=folder,
                unit=unit,
                directory=directory,
                token=token,
            )
        )
    return targets


# --------------------------------------------------------------------------
# systemd
# --------------------------------------------------------------------------


def _parse_timestamp(value: str | None) -> float | None:
    value = (value or "").strip()
    if not value or value in {"n/a", "0", "@0"}:
        return None
    if value.startswith("@"):  # --timestamp=unix form
        try:
            return float(value[1:].split(".")[0])
        except ValueError:
            return None
    # Fallback for older systemd: "Fri 2026-09-12 19:00:01 UTC"
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %Z"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


async def query_systemd(units: Sequence[str]) -> dict[str, dict[str, str]]:
    """Return {unit_id: {property: value}} for the given units."""
    if not units:
        return {}

    cmd = ["systemctl", "show", "--timestamp=unix"]
    for prop in SYSTEMD_PROPS:
        cmd += ["-p", prop]
    cmd += list(units)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    text = stdout.decode("utf-8", errors="ignore")

    result: dict[str, dict[str, str]] = {}
    for block in text.split("\n\n"):
        props: dict[str, str] = {}
        for line in block.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                props[key] = value
        unit_id = props.get("Id")
        if unit_id:
            result[unit_id] = props
    return result


# --------------------------------------------------------------------------
# Discord guild lookup
# --------------------------------------------------------------------------


async def fetch_guilds(
    session: aiohttp.ClientSession, token: str
) -> tuple[list[GuildInfo], str | None]:
    """Ask Discord which guilds this token's bot is in."""
    url = f"{API}/users/@me/guilds?with_counts=true&limit=200"
    headers = {"Authorization": f"Bot {token}"}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 401:
                return [], "token rejected (401)"
            if resp.status == 429:
                return [], "rate limited (429)"
            if resp.status != 200:
                return [], f"HTTP {resp.status}"
            payload = await resp.json()
    except asyncio.TimeoutError:
        return [], "timed out"
    except aiohttp.ClientError as exc:
        return [], f"network error: {type(exc).__name__}"

    guilds = [
        GuildInfo(
            id=str(g.get("id")),
            name=g.get("name") or "(unnamed)",
            members=g.get("approximate_member_count"),
        )
        for g in payload
    ]
    guilds.sort(key=lambda g: (-(g.members or 0), g.name.lower()))
    return guilds, None


# --------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------


async def collect(
    targets: Sequence[BotTarget],
    session: aiohttp.ClientSession | None = None,
    include_guilds: bool = True,
) -> list[BotStatus]:
    """Build a BotStatus for every target."""
    systemd = await query_systemd([t.unit for t in targets])

    statuses: list[BotStatus] = []
    for target in targets:
        props = systemd.get(target.unit, {})
        status = BotStatus(
            name=target.name,
            unit=target.unit,
            loaded=props.get("LoadState") == "loaded",
            active=props.get("ActiveState") == "active",
            sub_state=props.get("SubState") or "unknown",
            started_at=_parse_timestamp(props.get("ActiveEnterTimestamp")),
            stopped_at=_parse_timestamp(props.get("ActiveExitTimestamp")),
            restarts=int(props.get("NRestarts") or 0),
            token_found=bool(target.token),
        )
        statuses.append(status)

    if include_guilds and session is not None:
        async def _one(status: BotStatus, target: BotTarget) -> None:
            if not target.token:
                status.guild_error = "no token found in .env"
                return
            guilds, error = await fetch_guilds(session, target.token)
            status.guilds = guilds
            status.guild_error = error
            status.guilds_checked_at = time.time()

        await asyncio.gather(
            *(_one(s, t) for s, t in zip(statuses, targets)),
            return_exceptions=True,
        )

    return statuses
