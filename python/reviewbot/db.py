"""
db.py — sqlite store for clips + community votes.

Two tables:

  clips
    one row per clip we've posted to Discord.  Pinned to a Discord message
    so we can edit it later (e.g. show updated tally).  source_csv +
    source_demo + time_raw is the link back to the canonical frag in the
    upstream `_frags.json` so the ingest path can re-derive the full
    feature vector when merging community votes into training.

  votes
    one row per (user, clip).  PRIMARY KEY enforces "one vote per user per
    clip"; updates are UPSERTs (re-voting overwrites prior label).

Design notes:
  - sqlite is fine for the MVP; ~1000 clips × 100 voters × 1 vote each ≈
    100k rows total, well within sqlite's comfort zone.
  - Connection is opened per-call (sqlite + asyncio mix is fragile if you
    hold a connection across awaits).  Each query is fast (<1ms).
  - Switching to Postgres is a swap of this file + driver; the schema
    translates verbatim.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional


# ── schema ───────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    clip_id      TEXT PRIMARY KEY,
    batch_id     TEXT NOT NULL,
    source_csv   TEXT NOT NULL,
    source_demo  TEXT NOT NULL,
    time_raw     INTEGER NOT NULL,
    weapon       TEXT,
    attacker     TEXT,
    target       TEXT,
    score        REAL,
    posted_at    TEXT NOT NULL,         -- ISO timestamp
    message_id   INTEGER,                -- Discord message_id (for editing)
    channel_id   INTEGER                 -- Discord channel_id
);

CREATE TABLE IF NOT EXISTS votes (
    clip_id  TEXT NOT NULL,
    user_id  INTEGER NOT NULL,           -- Discord user snowflake
    label    TEXT NOT NULL CHECK (label IN ('good','ok','bad','ignore')),
    ts       TEXT NOT NULL,              -- ISO timestamp
    PRIMARY KEY (clip_id, user_id),
    FOREIGN KEY (clip_id) REFERENCES clips(clip_id) ON DELETE CASCADE
);

-- Lookup by clip for the tally view; lookup by user for /myvotes etc.
CREATE INDEX IF NOT EXISTS idx_votes_clip ON votes(clip_id);
CREATE INDEX IF NOT EXISTS idx_votes_user ON votes(user_id);
"""


@contextmanager
def _conn(db_path: Path):
    """Yields a sqlite connection with WAL mode + foreign keys enforced."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.row_factory = sqlite3.Row
        yield c
    finally:
        c.close()


def init(db_path: Path) -> None:
    """Create tables if they don't exist.  Safe to call repeatedly."""
    with _conn(db_path) as c:
        c.executescript(SCHEMA)


# ── clips ────────────────────────────────────────────────────────────────
def upsert_clip(db_path: Path, *,
                clip_id: str, batch_id: str, source_csv: str,
                source_demo: str, time_raw: int,
                weapon: Optional[str], attacker: Optional[str],
                target: Optional[str], score: Optional[float],
                message_id: Optional[int], channel_id: Optional[int]) -> None:
    """Record a clip we just posted.  If the clip was already posted (e.g.
    a re-run of /post-batch), we update the message_id/channel_id so the
    new Discord message is the canonical one — old messages aren't deleted
    automatically (operator can clean up via Discord directly)."""
    with _conn(db_path) as c:
        c.execute("""
            INSERT INTO clips (clip_id, batch_id, source_csv, source_demo,
                               time_raw, weapon, attacker, target, score,
                               posted_at, message_id, channel_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(clip_id) DO UPDATE SET
                batch_id    = excluded.batch_id,
                source_csv  = excluded.source_csv,
                source_demo = excluded.source_demo,
                time_raw    = excluded.time_raw,
                weapon      = excluded.weapon,
                attacker    = excluded.attacker,
                target      = excluded.target,
                score       = excluded.score,
                posted_at   = excluded.posted_at,
                message_id  = excluded.message_id,
                channel_id  = excluded.channel_id
        """, (clip_id, batch_id, source_csv, source_demo, time_raw,
              weapon, attacker, target, score,
              _now_iso(), message_id, channel_id))


def get_clip(db_path: Path, clip_id: str) -> Optional[sqlite3.Row]:
    with _conn(db_path) as c:
        return c.execute("SELECT * FROM clips WHERE clip_id=?", (clip_id,)).fetchone()


def get_clip_by_message_id(db_path: Path, message_id: int) -> Optional[sqlite3.Row]:
    """Look up a clip by the Discord message it was posted to.  Used by the
    reaction handler to map a reaction event back to a clip."""
    with _conn(db_path) as c:
        return c.execute(
            "SELECT * FROM clips WHERE message_id=?",
            (message_id,)
        ).fetchone()


def list_clips_in_batch(db_path: Path, batch_id: str) -> list[sqlite3.Row]:
    with _conn(db_path) as c:
        return c.execute(
            "SELECT * FROM clips WHERE batch_id=? ORDER BY clip_id",
            (batch_id,)
        ).fetchall()


def list_clips_with_messages(db_path: Path) -> list[sqlite3.Row]:
    """Return every clip that's been posted to Discord (has message_id +
    channel_id set).  Used by sync_reactions on bot startup."""
    with _conn(db_path) as c:
        return c.execute(
            "SELECT * FROM clips WHERE message_id IS NOT NULL "
            "AND channel_id IS NOT NULL ORDER BY posted_at DESC"
        ).fetchall()


# ── votes ────────────────────────────────────────────────────────────────
def upsert_vote(db_path: Path, *, clip_id: str, user_id: int, label: str) -> None:
    """One vote per (user, clip).  Re-voting overwrites prior label."""
    with _conn(db_path) as c:
        c.execute("""
            INSERT INTO votes (clip_id, user_id, label, ts)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(clip_id, user_id) DO UPDATE SET
                label = excluded.label,
                ts    = excluded.ts
        """, (clip_id, user_id, label, _now_iso()))


def remove_vote(db_path: Path, *, clip_id: str, user_id: int) -> bool:
    """For undo flow.  Returns True if a vote was removed."""
    with _conn(db_path) as c:
        cur = c.execute(
            "DELETE FROM votes WHERE clip_id=? AND user_id=?",
            (clip_id, user_id)
        )
        return cur.rowcount > 0


def replace_votes_for_clip(db_path: Path, *,
                           clip_id: str,
                           votes: list[tuple[int, str]]) -> None:
    """Atomically replace ALL votes for a clip with the supplied (user_id, label)
    list.  Used by the bot's reaction-sync to make Discord the source of truth
    after an offline period — whatever reactions are on the message NOW is what
    the DB reflects after sync."""
    with _conn(db_path) as c:
        c.execute("BEGIN")
        try:
            c.execute("DELETE FROM votes WHERE clip_id=?", (clip_id,))
            ts = _now_iso()
            for user_id, label in votes:
                c.execute(
                    "INSERT INTO votes (clip_id, user_id, label, ts) VALUES (?,?,?,?)",
                    (clip_id, user_id, label, ts)
                )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def get_user_vote(db_path: Path, *, clip_id: str, user_id: int) -> Optional[str]:
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT label FROM votes WHERE clip_id=? AND user_id=?",
            (clip_id, user_id)
        ).fetchone()
        return row["label"] if row else None


def tally(db_path: Path, clip_ids: Iterable[str]) -> dict[str, dict[str, int]]:
    """Returns {clip_id: {good: N, ok: N, bad: N, ignore: N}} for each id."""
    ids = list(clip_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    out: dict[str, dict[str, int]] = {
        cid: {"good": 0, "ok": 0, "bad": 0, "ignore": 0} for cid in ids
    }
    with _conn(db_path) as c:
        rows = c.execute(f"""
            SELECT clip_id, label, COUNT(*) AS n
            FROM votes
            WHERE clip_id IN ({placeholders})
            GROUP BY clip_id, label
        """, ids).fetchall()
        for r in rows:
            out[r["clip_id"]][r["label"]] = r["n"]
    return out


def export_votes(db_path: Path, batch_id: Optional[str] = None) -> list[dict]:
    """Return a flat list of {clip_id, user_id, label, ts, source_csv,
    source_demo, time_raw, weapon, score}.  The ingest path uses
    (source_csv, source_demo, time_raw) to look up the canonical frag in
    the upstream `_frags.json` and merge into training."""
    with _conn(db_path) as c:
        if batch_id:
            rows = c.execute("""
                SELECT v.clip_id, v.user_id, v.label, v.ts,
                       c.source_csv, c.source_demo, c.time_raw, c.weapon, c.score
                FROM votes v
                JOIN clips c ON c.clip_id = v.clip_id
                WHERE c.batch_id = ?
                ORDER BY v.clip_id, v.ts
            """, (batch_id,)).fetchall()
        else:
            rows = c.execute("""
                SELECT v.clip_id, v.user_id, v.label, v.ts,
                       c.source_csv, c.source_demo, c.time_raw, c.weapon, c.score
                FROM votes v
                JOIN clips c ON c.clip_id = v.clip_id
                ORDER BY v.clip_id, v.ts
            """).fetchall()
    return [dict(r) for r in rows]


# ── helpers ──────────────────────────────────────────────────────────────
def _now_iso() -> str:
    # ISO 8601 with seconds.  Avoids microsecond noise in the DB.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
