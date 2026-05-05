#!/usr/bin/env python3
"""
bot.py — Discord bot for community-curated highlight labelling (reaction-based).

Workflow:

  operator (you, locally):
    /post-batch [batch_name]         upload all current rendered clips to channel
    /tally [batch_name]              show vote counts per clip
    /export-labels [batch_name]      dump votes JSON for ingest into training
    /sync-votes [batch_name]         force a re-read of Discord reaction state

  community (members of configured guild):
    react with 👎 / 😐 / 👍 / 🗑️ on each clip   one vote per user per clip

Why reactions instead of buttons:
  Reactions persist on Discord messages independent of bot state.  When the
  bot is offline, votes still accumulate (Discord stores reactions on the
  message).  When the bot comes back online, sync_all_reactions() reads each
  posted clip's current reaction state and reconciles it with the DB.
  Buttons would lose any votes cast while offline (Discord doesn't queue
  interactions for offline bots).  Run-locally + collect-on-startup is the
  intended pattern.

Run:
    cd python/reviewbot
    .\\run.bat
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import discord
from discord import app_commands

import config
import db
import subprocess


# ── logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("reviewbot")


def _ensure_discord_ready_size(mp4_path: Path, max_mb: float = 9.5) -> Path:
    """If file is too large, use ffmpeg to compress it. Recursively increases
    CRF (lower quality) until it fits under max_mb."""
    current_size = mp4_path.stat().st_size / (1024 * 1024)
    if current_size <= max_mb:
        return mp4_path

    log.info("Clip %s is too large (%.1fMB). Starting auto-compression...", mp4_path.name, current_size)
    
    # Try increasingly aggressive CRF values
    for crf in [28, 30, 32, 35, 40]:
        tmp_out = mp4_path.with_suffix(f".crf{crf}.mp4")
        cmd = [
            "ffmpeg", "-y", "-i", str(mp4_path),
            "-vcodec", "libx264", "-crf", str(crf), "-preset", "faster",
            "-acodec", "aac", "-b:a", "128k",
            str(tmp_out)
        ]
        
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            new_size = tmp_out.stat().st_size / (1024 * 1024)
            if new_size <= max_mb:
                log.info("  Success at CRF %d: %.1fMB", crf, new_size)
                # Overwrite original with the compressed version
                tmp_out.replace(mp4_path)
                return mp4_path
            else:
                log.info("  CRF %d still too large: %.1fMB", crf, new_size)
                tmp_out.unlink()
        except subprocess.CalledProcessError as e:
            log.warning("  ffmpeg failed at CRF %d: %s", crf, e)
            if tmp_out.exists(): tmp_out.unlink()

    log.warning("Could not compress %s under %.1fMB even at CRF 40.", mp4_path.name, max_mb)
    return mp4_path


# ── reaction <-> label mapping ───────────────────────────────────────────
# Order matters — bot adds reactions in this order so they appear left-to-right.
LABEL_ORDER = ["bad", "ok", "good", "ignore"]

WEAPON_MAP = {
    # Pistols
    "MOD_BRYAR_PISTOL":     "Pistol",
    "MOD_BRYAR_PISTOL_ALT": "Pistol Alt",
    
    # Rifles & Repeaters
    "MOD_BLASTER":          "Blaster",
    "MOD_REPEATER":         "Repeater",
    "MOD_REPEATER_ALT":     "Repeater Alt",
    "MOD_REPEATER_ALT_SPLASH": "Repeater Alt Splash",

    # Sniper
    "MOD_DISRUPTOR":        "Disruptor",
    "MOD_DISRUPTOR_SNIPER": "Sniper",

    # Heavy Weapons
    "MOD_BOWCASTER":        "Bowcaster",
    "MOD_ROCKET":           "Rocket",
    "MOD_ROCKET_SPLASH":    "Rocket Splash",
    "MOD_CONC":             "Conc",
    "MOD_CONCUSSION":       "Conc",
    "MOD_CONCUSSION_ALT":   "Conc Alt",
    "MOD_GOLAN_ALT":        "Golan Alt",
    "MOD_FLECHETTE":        "Golan",
    "MOD_FLECHETTE_ALT_SPLASH": "Golan Alt Splash",

    # DEMP
    "MOD_DEMP2":            "DEMP",
    "MOD_DEMP2_ALT":        "DEMP Alt",

    # Explosives
    "MOD_THERMAL":          "Thermal",
    "MOD_THERMAL_SPLASH":   "Thermal Splash",
    "MOD_DET_PACK":         "Det Pack",
    "MOD_DET_PACK_SPLASH":  "Det Pack Splash",
    "MOD_TIMED_MINE_SPLASH": "Mine Splash",
    "MOD_TRIP_MINE_SPLASH":  "Trip Mine Splash",

    # Melee & Sabers
    "MOD_SABER":            "Saber",
    "MOD_STUN_BATON":       "Baton",
    "MOD_MELEE":            "Melee",

    # Environmental / Special
    "MOD_TELEFRAG":         "Telefrag",
    "MOD_FALLING":          "Falling",
    "MOD_CRUSH":            "Crush",
    "MOD_SUICIDE":          "Suicide",
}

EMOJI_TO_LABEL: dict[str, str] = {
    "\U0001F44E": "bad",      # 👎
    "\U0001F610": "ok",       # 😐
    "\U0001F44D": "good",     # 👍
    "\U0001F5D1️": "ignore",  # 🗑️ (note variation selector)
}
# Some Discord clients send 🗑️ without the variation selector; accept both.
EMOJI_TO_LABEL["\U0001F5D1"] = "ignore"

LABEL_TO_EMOJI: dict[str, str] = {
    "bad":    "\U0001F44E",
    "ok":     "\U0001F610",
    "good":   "\U0001F44D",
    "ignore": "\U0001F5D1️",
}


def emoji_str(emoji) -> str:
    """Normalize a discord.PartialEmoji or discord.Emoji or str to a comparable
    string.  We only deal with Unicode emojis here; custom server emojis aren't
    supported (would need different equality)."""
    return str(emoji)


# ── manifest ─────────────────────────────────────────────────────────────
def load_manifest() -> list[dict]:
    """Same NUL-tolerant load as the Flask review UI's app.py."""
    if not config.MANIFEST_PATH.exists():
        return []
    raw = config.MANIFEST_PATH.read_bytes().rstrip(b"\x00").rstrip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        end = raw.rfind(b"]")
        if end > 0:
            try:
                return json.loads(raw[: end + 1])
            except json.JSONDecodeError:
                pass
        return []


# ── gates ────────────────────────────────────────────────────────────────
async def check_gates(member: discord.Member, cfg: dict) -> tuple[bool, str]:
    """Returns (allowed, reason_if_not).  member.guild_permissions, .roles, and
    .created_at are all populated for fully-cached members; partial cases get
    a polite reject."""
    if not isinstance(member, discord.Member):
        return False, "couldn't verify your server membership."

    if member.guild.id != cfg["guild_id"]:
        return False, "votes only count from the JACTF Discord server."

    role_id = cfg.get("role_id")
    if role_id and not any(r.id == role_id for r in member.roles):
        return False, "you need the server vote-role to participate."

    min_days = cfg.get("min_account_days")
    if min_days:
        from datetime import datetime, timezone
        age_days = (datetime.now(timezone.utc) - member.created_at).total_seconds() / 86400
        if age_days < min_days:
            return False, (
                f"votes are restricted to accounts older than {min_days} days "
                f"(yours is {age_days:.0f} days)."
            )

    return True, ""


# ── bot setup ────────────────────────────────────────────────────────────
class ReviewBot(discord.Client):
    def __init__(self, cfg: dict):
        intents = discord.Intents.default()
        intents.members  = True   # for role + member.created_at access
        intents.reactions = True  # for raw_reaction events
        super().__init__(intents=intents)
        self._cfg = cfg
        self.tree = app_commands.CommandTree(self)
        self._initial_sync_done = False

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self._cfg["guild_id"])
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("slash commands synced to guild %s", self._cfg["guild_id"])


def make_bot(cfg: dict) -> ReviewBot:
    bot = ReviewBot(cfg)

    # ── lifecycle ─────────────────────────────────────────────────────
    @bot.event
    async def on_ready():
        log.info("logged in as %s (id=%s)", bot.user, bot.user.id)
        if not bot._initial_sync_done:
            log.info("running initial reaction sync — this catches up votes "
                     "received while bot was offline")
            try:
                summary = await sync_all_reactions(bot, cfg)
                log.info("initial sync: %d clips processed, %d votes recorded, "
                         "%d reactions pruned (gates/duplicates)",
                         summary["clips"], summary["votes"], summary["pruned"])
            except Exception as e:
                log.exception("initial sync failed: %s", e)
            bot._initial_sync_done = True
        log.info("ready — watching guild %s, posting to channel %s",
                 cfg["guild_id"], cfg["channel_id"])

    # ── reaction handlers (the new vote mechanism) ────────────────────
    @bot.event
    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
        if payload.user_id == bot.user.id:
            return  # ignore the bot's own seed reactions
        await _handle_reaction_add(bot, cfg, payload)

    @bot.event
    async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
        if payload.user_id == bot.user.id:
            return
        await _handle_reaction_remove(bot, cfg, payload)

    # ── slash commands ────────────────────────────────────────────────
    @bot.tree.command(
        name="post-batch",
        description="Upload all current rendered clips to the review channel.",
    )
    @app_commands.describe(batch_name="optional label for this batch (defaults to a timestamp)")
    async def post_batch(interaction: discord.Interaction,
                         batch_name: str | None = None):
        member = interaction.user
        if member.id != 205505475972694016:
            await interaction.response.send_message(
                "only the operator can run this command.",
                ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        manifest = load_manifest()
        if not manifest:
            await interaction.followup.send(
                f"no clips found in `{config.MANIFEST_PATH}` — render some first.",
                ephemeral=True)
            return

        # Load match metadata for date lookups
        seen_data = {}
        seen_path = config.ROOT / "python" / "fetch" / "seen_matches.json"
        if seen_path.is_file():
            try:
                seen_data = json.loads(seen_path.read_text(encoding="utf-8")).get("seen", {})
            except Exception:
                pass

        channel = bot.get_channel(cfg["channel_id"])
        if channel is None:
            await interaction.followup.send(
                f"can't find channel {cfg['channel_id']} — check DISCORD_CHANNEL_ID.",
                ephemeral=True)
            return

        batch = batch_name or time.strftime("batch_%Y%m%d_%H%M%S")
        try:
            thread = await channel.create_thread(name=f"Review Batch - {batch}", type=discord.ChannelType.public_thread)
        except discord.HTTPException as e:
            await interaction.followup.send(f"failed to create thread: {e}", ephemeral=True)
            return

        posted = skipped = 0
        for clip in manifest:
            cid = clip["clip_id"]
            mp4 = config.CAPTURES_DIR / f"{cid}.mp4"
            if not mp4.is_file():
                log.warning("skipping %s — mp4 missing at %s", cid, mp4)
                skipped += 1
                continue
            
            # Auto-compress if needed to fit Discord's 10MB limit
            mp4 = _ensure_discord_ready_size(mp4)
            if mp4.stat().st_size > 10 * 1024 * 1024:
                log.warning("skipping %s — still too large (%.1fMB) after compression",
                            cid, mp4.stat().st_size / (1024 * 1024))
                skipped += 1
                continue

            embed = _clip_embed(clip, seen_data)
            file = discord.File(str(mp4), filename=f"{cid}.mp4")
            try:
                msg = await thread.send(embed=embed, file=file)
            except discord.HTTPException as e:
                log.warning("failed to post %s: %s", cid, e)
                skipped += 1
                continue

            # Seed the 4 vote reactions.  Order matters for visual layout.
            for label in LABEL_ORDER:
                try:
                    await msg.add_reaction(LABEL_TO_EMOJI[label])
                except discord.HTTPException as e:
                    log.warning("failed to add %s reaction on %s: %s", label, cid, e)

            db.upsert_clip(
                config.DB_PATH,
                clip_id=cid,
                batch_id=batch,
                source_csv=clip.get("predict_csv", ""),
                source_demo=clip.get("source_demo", ""),
                time_raw=int(clip.get("time_raw", 0)),
                weapon=clip.get("mod_name"),
                attacker=clip.get("attacker_name"),
                target=clip.get("target_name"),
                score=float(clip.get("score") or 0.0),
                message_id=msg.id,
                channel_id=thread.id,
            )
            posted += 1

        await interaction.followup.send(
            f"posted **{posted}** clip(s) to <#{thread.id}> as batch `{batch}`."
            + (f"  ({skipped} skipped — see logs)" if skipped else ""),
            ephemeral=True)

    @bot.tree.command(
        name="tally",
        description="Show current vote counts per clip in a batch.",
    )
    @app_commands.describe(batch_name="batch label (default: most recent posted)")
    async def tally(interaction: discord.Interaction,
                    batch_name: str | None = None):
        if interaction.user.id != 205505475972694016:
            await interaction.response.send_message("only the operator can run this command.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=False)

        if batch_name is None:
            import sqlite3
            with sqlite3.connect(str(config.DB_PATH)) as c:
                row = c.execute(
                    "SELECT batch_id FROM clips ORDER BY posted_at DESC LIMIT 1"
                ).fetchone()
            if row is None:
                await interaction.followup.send("no clips have been posted yet.", ephemeral=False)
                return
            batch_name = row[0]

        clips = db.list_clips_in_batch(config.DB_PATH, batch_name)
        if not clips:
            await interaction.followup.send(f"no clips in batch `{batch_name}`.", ephemeral=False)
            return

        cids = [c["clip_id"] for c in clips]
        t = db.tally(config.DB_PATH, cids)

        lines = [f"**Tally for batch `{batch_name}`** ({len(clips)} clips)\n", "```"]
        lines.append(f"{'clip':6}  {'good':>4} {'ok':>4} {'bad':>4} {'ign':>4}  weapon                  attacker")
        lines.append("─" * 80)
        for c in clips:
            counts = t.get(c["clip_id"], {})
            raw_weapon = c['weapon'] or '?'
            weapon = WEAPON_MAP.get(raw_weapon)
            if not weapon:
                weapon = raw_weapon.replace('MOD_','').replace('_',' ').title()
                
            atk = _strip_color_codes(c['attacker'] or '?')
            line = (
                f"{c['clip_id']:6}  {counts.get('good',0):>4} {counts.get('ok',0):>4} "
                f"{counts.get('bad',0):>4} {counts.get('ignore',0):>4}  "
                f"{weapon:<22}  "
                f"{atk}"
            )
            lines.append(line[:76])
        lines.append("```")

        body = "\n".join(lines)
        for chunk in _chunk_message(body, 1900):
            await interaction.followup.send(chunk, ephemeral=False)

    @bot.tree.command(
        name="sync-votes",
        description="Re-read Discord reactions and reconcile with the DB.",
    )
    @app_commands.describe(batch_name="batch to sync (default: all clips with messages)")
    async def sync_votes(interaction: discord.Interaction,
                         batch_name: str | None = None):
        if interaction.user.id != 205505475972694016:
            await interaction.response.send_message("only the operator can run this command.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        summary = await sync_all_reactions(bot, cfg, batch_filter=batch_name)
        await interaction.followup.send(
            f"sync complete — {summary['clips']} clip(s) processed, "
            f"{summary['votes']} vote(s) in DB, "
            f"{summary['pruned']} reaction(s) pruned (gates / duplicates).",
            ephemeral=True)

    @bot.tree.command(
        name="export-labels",
        description="Dump current vote tallies as JSON for the ingest pipeline.",
    )
    @app_commands.describe(batch_name="batch label (default: all batches)")
    async def export_labels(interaction: discord.Interaction,
                            batch_name: str | None = None):
        if interaction.user.id != 205505475972694016:
            await interaction.response.send_message("only the operator can run this command.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        rows = db.export_votes(config.DB_PATH, batch_id=batch_name)
        if not rows:
            await interaction.followup.send(
                f"no votes recorded{' for batch ' + batch_name if batch_name else ''}.",
                ephemeral=True)
            return

        export_dir = config.DB_PATH.parent / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        tag = batch_name or "all"
        out_path = export_dir / f"votes_{tag}_{ts}.json"
        out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

        await interaction.followup.send(
            f"exported **{len(rows)}** vote(s) to `{out_path}`.",
            file=discord.File(str(out_path)),
            ephemeral=True)

    return bot


# ── reaction handling ────────────────────────────────────────────────────
async def _handle_reaction_add(bot: discord.Client, cfg: dict,
                                payload: discord.RawReactionActionEvent) -> None:
    """A user added a reaction.  Validate, enforce one-vote, write to DB."""
    label = EMOJI_TO_LABEL.get(emoji_str(payload.emoji))
    if label is None:
        return  # not one of our vote emojis — ignore

    clip = db.get_clip_by_message_id(config.DB_PATH, payload.message_id)
    if clip is None:
        return  # not a clip message we know about

    # Resolve guild + member for gates
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if guild is None:
        return
    member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
    if member is None or member.bot:
        return

    # Need the message to mutate reactions
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except discord.NotFound:
            return
    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return

    # Apply gates — if rejected, remove the reaction silently and DM the user
    ok, reason = await check_gates(member, cfg)
    if not ok:
        try:
            await message.remove_reaction(payload.emoji, member)
        except discord.HTTPException:
            pass
        try:
            await member.send(
                f"your reaction on the clip wasn't recorded: {reason}"
            )
        except discord.HTTPException:
            pass  # user has DMs disabled — fall back to silent removal
        return

    # Enforce one vote per user per clip — remove any other vote reactions
    # this user has on this message.
    for reaction in message.reactions:
        if emoji_str(reaction.emoji) == emoji_str(payload.emoji):
            continue
        if EMOJI_TO_LABEL.get(emoji_str(reaction.emoji)) is None:
            continue  # non-vote reaction, leave alone (e.g. someone added 🔥)
        # is this user in this reaction?
        try:
            async for u in reaction.users():
                if u.id == member.id:
                    await message.remove_reaction(reaction.emoji, member)
                    break
        except discord.HTTPException:
            pass

    db.upsert_vote(config.DB_PATH, clip_id=clip["clip_id"],
                   user_id=member.id, label=label)
    log.info("vote: user=%s clip=%s label=%s", member.id, clip["clip_id"], label)


async def _handle_reaction_remove(bot: discord.Client, cfg: dict,
                                   payload: discord.RawReactionActionEvent) -> None:
    """A user removed a reaction.  If it was their current vote, remove from DB."""
    label = EMOJI_TO_LABEL.get(emoji_str(payload.emoji))
    if label is None:
        return

    clip = db.get_clip_by_message_id(config.DB_PATH, payload.message_id)
    if clip is None:
        return

    current = db.get_user_vote(config.DB_PATH,
                                clip_id=clip["clip_id"],
                                user_id=payload.user_id)
    if current == label:
        db.remove_vote(config.DB_PATH,
                        clip_id=clip["clip_id"],
                        user_id=payload.user_id)
        log.info("unvote: user=%s clip=%s (was %s)",
                 payload.user_id, clip["clip_id"], label)


# ── reaction sync (catches up votes from offline period) ─────────────────
async def sync_all_reactions(bot: discord.Client, cfg: dict,
                              batch_filter: str | None = None) -> dict:
    """Walk every posted clip, read its current reactions on Discord, and
    rewrite the DB to match.  Applies gates retroactively (removes reactions
    from non-eligible users) and prunes multi-votes (one vote per user, kept
    in first-seen order).  Returns a summary dict for logging.

    Discord is the source of truth here — the DB is rebuilt from reaction
    state, not the other way round.  This makes the sync idempotent and lets
    operators 'reset' votes by removing reactions on Discord directly."""
    summary = {"clips": 0, "votes": 0, "pruned": 0}

    if batch_filter:
        clips = db.list_clips_in_batch(config.DB_PATH, batch_filter)
    else:
        clips = db.list_clips_with_messages(config.DB_PATH)

    guild = bot.get_guild(cfg["guild_id"])
    if guild is None:
        log.warning("sync: guild %s not found", cfg["guild_id"])
        return summary

    for clip in clips:
        if not (clip["message_id"] and clip["channel_id"]):
            continue
        channel = bot.get_channel(clip["channel_id"])
        if channel is None:
            try:
                channel = await bot.fetch_channel(clip["channel_id"])
            except discord.NotFound:
                continue

        try:
            message = await channel.fetch_message(clip["message_id"])
        except discord.NotFound:
            log.warning("sync: message %s for clip %s no longer accessible",
                        clip["message_id"], clip["clip_id"])
            continue

        # Walk reactions in their stored order; first vote-emoji per user wins.
        user_to_label: dict[int, str] = {}

        for reaction in message.reactions:
            label = EMOJI_TO_LABEL.get(emoji_str(reaction.emoji))
            if label is None:
                continue
            try:
                async for user in reaction.users():
                    if user.bot:
                        continue
                    member = guild.get_member(user.id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(user.id)
                        except discord.NotFound:
                            continue

                    ok, _reason = await check_gates(member, cfg)
                    if not ok:
                        try:
                            await message.remove_reaction(reaction.emoji, member)
                            summary["pruned"] += 1
                        except discord.HTTPException:
                            pass
                        continue

                    if user.id in user_to_label:
                        # already have a vote from this user — strip the dupe
                        try:
                            await message.remove_reaction(reaction.emoji, member)
                            summary["pruned"] += 1
                        except discord.HTTPException:
                            pass
                        continue

                    user_to_label[user.id] = label
            except discord.HTTPException:
                continue

        db.replace_votes_for_clip(
            config.DB_PATH,
            clip_id=clip["clip_id"],
            votes=list(user_to_label.items()),
        )
        summary["clips"] += 1
        summary["votes"] += len(user_to_label)

    return summary


# ── formatting helpers ───────────────────────────────────────────────────
def _get_timecode(clip: dict) -> str:
    import re
    # Try human_time first
    htime = clip.get("human_time")
    if htime and htime != "?":
        # Extract mm:ss, ignoring hours or ms
        m = re.search(r'(?:\d{2}:)?(\d{2}:\d{2})(?:\.\d+)?', htime)
        if m: return m.group(1)
        m2 = re.search(r'(\d+:\d{2})(?:\.\d+)?', htime)
        if m2: return m2.group(1)
        return htime

    # Fallback to parsing from trimmed_clip name (e.g., trm_hash_12-58.342_...)
    tclip = clip.get("trimmed_clip", "")
    m = re.search(r'_(\d+)-(\d+)(?:\.\d+)?', tclip)
    if m:
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"

    return "?"

def _clean_demo_name(demo: str) -> str:
    if not demo or demo == "?":
        return "?"
    import re
    # Strip hash prefix (e.g., aadc7d94de320a5b__)
    m = re.sub(r'^[a-f0-9]+__', '', demo)
    # Strip extension
    m = re.sub(r'\.dm_\d+', '', m)
    m = re.sub(r'\.dm_meta', '', m)
    return m

def _clip_embed(clip: dict, seen_data: dict | None = None) -> discord.Embed:
    """The metadata card that accompanies each posted clip."""
    raw_weapon = clip.get("mod_name") or "?"
    weapon = WEAPON_MAP.get(raw_weapon)
    if not weapon:
        weapon = raw_weapon.replace("MOD_", "").replace("_", " ").title()
        
    atk_raw = clip.get("attacker_name") or "?"
    score   = clip.get("score") or 0.0
    htime   = _get_timecode(clip)
    demo    = clip.get("source_demo", "?")
    
    clean_demo = _clean_demo_name(demo)
    atk_ansi = _ansi_color_codes(atk_raw)

    # Extract match ID and date
    match_id = "unknown"
    match_date = "unknown"
    if demo and demo != "?":
        # Extract 16-char hex match ID from start of filename
        import re
        m = re.match(r'^([a-f0-9]{16})__', demo)
        if m:
            match_id = m.group(1)
            # Look up date in seen_matches
            if seen_data and match_id in seen_data:
                mdata = seen_data[match_id]
                ts = mdata.get("time_created")
                if ts:
                    from datetime import datetime
                    match_date = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")

    e = discord.Embed(
        title=f"[{clip['clip_id']}] {clean_demo}",
        description=f"```ansi\n\u001b[0;37m{atk_ansi}\u001b[0m\n```",
        color=0x4d8cff,
    )
    e.add_field(name="Weapon",      value=weapon,            inline=True)
    e.add_field(name="Match Time",  value=htime,             inline=True)
    e.add_field(name="Model Score", value=f"{score:.3f}",    inline=True)
    e.add_field(
        name="How to vote",
        value="React with 👎 bad   ·   😐 ok   ·   👍 good   ·   🗑️ ignore  (one per clip; toggle to change)",
        inline=False,
    )
    e.set_footer(text=f"Match: {match_id}  ·  Date: {match_date}")
    return e


def _ansi_color_codes(s: str) -> str:
    """Convert JKA / JA+ alphanumeric colour codes (^X) to Discord ANSI codes."""
    import re
    # Discord ansi colors: 30: black, 31: red, 32: green, 33: yellow, 34: blue, 35: magenta, 36: cyan, 37: white
    
    # Mapping for all standard (0-9) and extended (A-Z, a-z) JKA codes
    color_map = {
        # Numbers
        '1': '31', '9': '31',                               # Reds
        '2': '32',                                          # Green
        '3': '33', '8': '33',                               # Yellow / Orange
        '4': '34',                                          # Blue
        '5': '36',                                          # Cyan
        '6': '35',                                          # Magenta
        '7': '37',                                          # White
        '0': '30',                                          # Black
        
        # Extended Uppercase (JA+)
        'A': '31', 'I': '31', 'Q': '31',                    # Reds
        'B': '32', 'J': '32', 'R': '32',                    # Greens
        'C': '33', 'K': '33', 'S': '33', 'Y': '33',          # Yellows / Orange
        'D': '34', 'L': '34', 'T': '34',                    # Blues
        'E': '36', 'M': '36', 'U': '36',                    # Cyans
        'F': '35', 'N': '35', 'V': '35', 'Z': '35',          # Magentas / Purples
        'G': '37', 'W': '37',                               # Whites
        'H': '30', 'O': '37', 'P': '30', 'X': '30',          # Black / Gray
        
        # Extended Lowercase (JA+)
        'a': '31', 'i': '31', 'q': '31',                    # Reds
        'b': '32', 'j': '32', 'r': '32',                    # Greens
        'c': '33', 'k': '33', 's': '33', 'y': '33',          # Yellows / Orange
        'd': '34', 'l': '34', 't': '34',                    # Blues
        'e': '36', 'm': '36', 'u': '36',                    # Cyans
        'f': '35', 'n': '35', 'v': '35', 'z': '35',          # Magentas / Purples
        'g': '37', 'w': '37',                               # Whites
        'h': '30', 'o': '37', 'p': '30', 'x': '30',          # Black / Gray
    }
    
    def repl(m):
        code = m.group(1)
        c = color_map.get(code, '37')
        return f"\u001b[0;{c}m"
        
    res = re.sub(r"\^([0-9a-zA-Z])", repl, s)
    if res != s:
        res += "\u001b[0m"
    return res


def _strip_color_codes(s: str) -> str:
    """Q3 colour codes are ^N where N is 0-9.  Strip for readability."""
    import re
    return re.sub(r"\^.", "", s)


def _chunk_message(text: str, limit: int = 1900) -> list[str]:
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


# ── main ─────────────────────────────────────────────────────────────────
def main():
    cfg = config.load()
    db.init(config.DB_PATH)
    log.info("db initialized at %s", config.DB_PATH)

    bot = make_bot(cfg)
    try:
        bot.run(cfg["token"], log_handler=None)
    except discord.LoginFailure:
        sys.exit("DISCORD_TOKEN was rejected — double-check it's the right bot token.")


if __name__ == "__main__":
    main()
