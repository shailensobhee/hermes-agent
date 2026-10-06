"""config.yaml readers for the Discord adapter: DM role-auth guild opt-in and button-view timeout.

Re-exported from ``plugins.platforms.discord.adapter`` (callers and tests import them there).
"""
from typing import Any, Optional


def _read_dm_role_auth_guild() -> Optional[int]:
    """Return the guild ID opted-in for DM role-based auth, or None (secure default). Read from
    config.yaml ``discord.dm_role_auth_guild`` only (behavioral, not a secret); int or numeric string."""
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config() or {}
        discord_cfg = cfg.get("discord", {}) or {}
        raw = discord_cfg.get("dm_role_auth_guild")
    except Exception:
        return None
    if raw is None or raw == "":
        return None
    try:
        guild_id = int(raw)
    except (TypeError, ValueError):
        return None
    return guild_id if guild_id > 0 else None


# Default timeout for Discord button views when ``approvals.discord_prompt_timeout`` is unset;
# Discord interaction tokens expire at ~15 minutes, so 900s is the practical ceiling.
_DISCORD_PROMPT_TIMEOUT_DEFAULT = 300
_DISCORD_PROMPT_TIMEOUT_MIN = 30
_DISCORD_PROMPT_TIMEOUT_MAX = 900


def _read_discord_prompt_timeout():  # maia: never-expire -> Optional[int] (None = no timeout)
    """Timeout (seconds) for Discord button views from ``approvals.discord_prompt_timeout``
    (default 300), clamped to [MIN, MAX] so a typo can't make prompts vanish or outlive tokens."""
    raw: Any = None
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config() or {}
        approvals_cfg = cfg.get("approvals", {}) or {}
        raw = approvals_cfg.get("discord_prompt_timeout")
    except Exception:
        return _DISCORD_PROMPT_TIMEOUT_DEFAULT
    if raw is None or raw == "":
        return _DISCORD_PROMPT_TIMEOUT_DEFAULT
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return _DISCORD_PROMPT_TIMEOUT_DEFAULT
    # maia: never-expire sentinel. A configured value <= 0 means "wait indefinitely"
    # (standing user gate: clarify/approval prompts must never auto-expire). Returning None
    # makes discord.ui.View(timeout=None) listen forever; each click is a fresh interaction,
    # so this does not hit the ~15min Discord token limit. Bypasses the [MIN, MAX] clamp.
    if seconds <= 0:
        return None
    if seconds < _DISCORD_PROMPT_TIMEOUT_MIN:
        return _DISCORD_PROMPT_TIMEOUT_MIN
    if seconds > _DISCORD_PROMPT_TIMEOUT_MAX:
        return _DISCORD_PROMPT_TIMEOUT_MAX
    return seconds
