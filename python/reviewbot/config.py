"""
config.py — env-var loading for the review Discord bot.

All knobs are environment variables.  Sensible defaults where reasonable;
required values fail fast with a clear error.

Required:
  DISCORD_TOKEN       Bot token from discord.com/developers/applications.
                      Keep this secret — never commit to git, never paste in chat.
  DISCORD_GUILD_ID    Numeric ID of the Discord server the bot operates in.
  DISCORD_CHANNEL_ID  Numeric ID of the channel where clips get posted.

Optional gates (anti-spam, all default to "off"):
  DISCORD_ROLE_ID         If set, only members with this role can vote.
  DISCORD_MIN_ACCOUNT_DAYS If set, reject votes from accounts younger than N days.

Path resolution (matches the rest of the pipeline):
  JACTF_ROOT          Project root override (default: auto-detected from this file).
  JACTF_GAMEDATA      JKA GameData directory (default: <root>/a full jka install/...).

Database:
  REVIEWBOT_DB        SQLite path (default: <reviewbot dir>/votes.db).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


# ── path resolution (mirror of label_io.py / app.py / others) ────────────
_REVIEWBOT_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("JACTF_ROOT") or _REVIEWBOT_DIR.parents[1])

GAMEDATA = Path(os.environ.get("JACTF_GAMEDATA")
                or (ROOT / "a full jka install" / "game_directory" / "Jedi Academy" / "GameData"))

MANIFEST_PATH = GAMEDATA / "mme" / "clip_manifest.json"
CAPTURES_DIR  = GAMEDATA / "mme" / "captures"

DB_PATH = Path(os.environ.get("REVIEWBOT_DB") or (_REVIEWBOT_DIR / "votes.db"))


# ── Discord config ───────────────────────────────────────────────────────
def _required(key: str) -> str:
    v = os.environ.get(key, "").strip()
    if not v:
        sys.exit(
            f"missing required env var: {key}\n"
            f"see python/reviewbot/.env.example for the full list, then either:\n"
            f"  $env:{key} = '...'    (this PowerShell session)\n"
            f"  [System.Environment]::SetEnvironmentVariable('{key}','...','User')   (persistent)"
        )
    return v


def _int_required(key: str) -> int:
    raw = _required(key)
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"env var {key} must be a numeric Discord snowflake, got: {raw!r}")


def _int_optional(key: str) -> int | None:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"env var {key} must be a numeric snowflake or empty, got: {raw!r}")


# Loaded lazily so importing this module for tests doesn't require env vars.
def load() -> dict:
    return {
        "token":            _required("DISCORD_TOKEN"),
        "guild_id":         _int_required("DISCORD_GUILD_ID"),
        "channel_id":       _int_required("DISCORD_CHANNEL_ID"),
        "role_id":          _int_optional("DISCORD_ROLE_ID"),         # optional
        "min_account_days": _int_optional("DISCORD_MIN_ACCOUNT_DAYS"),# optional
    }
