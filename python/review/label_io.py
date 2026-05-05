"""
label_io — read/write the *_client_frags_aggregated.json training corpus.

Shared between the Flask review UI (app.py) and any future CLI bulk-label
tools.  Mirrors update_good.py's dedup logic exactly so the four aggregated
JSONs stay schema-compatible with predict_frags_ensemble.py's training loader.

Key facts the dedup logic relies on:
  - Frag identity = (normalised _source_file, time_raw).
    See update_good.py:50-51 (dedup_key) and update_good.py:31-45
    (normalise_name).  We replicate it here verbatim.
  - When a label changes, the old aggregated entry must be REMOVED
    (predict_frags_ensemble.py concatenates good+bad and a frag in both
     would muddy the training signal).
  - Every write produces a timestamped backup under predict/backup/ so
    the user can roll back if a labelling session goes sideways.

Public API:
  AGGREGATED_FILE[label]              path to the corpus for that label
  load_corpus(label)                  -> list[dict]
  apply_label(clip, label)            -> dict status; mutates aggregated files + state
  remove_label(clip)                  -> dict status
  load_state() / save_state(state)    review_state.json sidecar I/O
  source_frags_for(predict_csv)       resolve _jactf_new_frags.ensemble_predictions.csv -> _jactf_new_frags.json
  find_frag(source_path, time_raw, source_demo)  raise/return the matching record
"""

from __future__ import annotations

import json
import re
import shutil
import time
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Optional

# ── paths ────────────────────────────────────────────────────────────────
# REVIEW_DIR is this file's directory; ROOT is up 2 (review/ → python/ → repo root).
# JACTF_ROOT overrides if you've moved the script elsewhere.
import os
REVIEW_DIR  = Path(__file__).resolve().parent
ROOT        = Path(os.environ.get("JACTF_ROOT") or REVIEW_DIR.parents[1])
PREDICT_DIR = ROOT / "python" / "predict"
BACKUP_DIR  = PREDICT_DIR / "backup"
STATE_FILE  = REVIEW_DIR / "review_state.json"
SESSIONS_DIR = REVIEW_DIR / "sessions"
CURRENT_SESSION_FILE = REVIEW_DIR / "current_session.json"

LABELS = ("good", "ok", "bad", "ignore")

AGGREGATED_FILE: dict[str, Path] = {
    "good":   PREDICT_DIR / "good_client_frags_aggregated.json",
    "ok":     PREDICT_DIR / "ok_client_frags_aggregated.json",
    "bad":    PREDICT_DIR / "bad_client_frags_aggregated.json",
    "ignore": PREDICT_DIR / "ignore_frags_aggregated.json",
}


# ── helpers ──────────────────────────────────────────────────────────────
def normalise_name(name: str) -> str:
    """Mirror of update_good.py:31-45 — must stay byte-identical so the
    Flask path and the legacy CLI path produce the same dedup_key for the
    same frag."""
    n = Path(name).name
    n = unicodedata.normalize("NFKC", n).strip()
    n = re.sub(r"\s+", " ", n)
    n = n.replace("\xa0", " ").lower()
    n = n.replace(" ", "_")
    n = re.sub(r"\.dm_26(\.dm_meta)?$", "", n)
    return n


def dedup_key(frag: dict) -> str:
    return f"{normalise_name(frag.get('_source_file', ''))}|{frag.get('time_raw', '')}"


def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── corpus I/O ───────────────────────────────────────────────────────────
def load_corpus(label: str) -> list[dict]:
    p = AGGREGATED_FILE[label]
    return json.loads(p.read_text("utf-8")) if p.exists() else []


def _backup(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"{path.stem}_{ts}.json"
    # avoid backup-spam on rapid-fire labels — if a backup from this same
    # second already exists, don't make another (saves disk if the user
    # mashes 'good' on 50 clips in a minute).
    if dst.exists():
        return dst
    shutil.copy2(path, dst)
    return dst


def _atomic_write(path: Path, data: list[dict]) -> None:
    """tmp + rename so a crash mid-save doesn't truncate the corpus."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── source frag lookup ───────────────────────────────────────────────────
@lru_cache(maxsize=8)
def _load_source(json_path: str) -> tuple[dict, ...]:
    """Cached load of a *_frags.json file (read-only).  Returns tuple so
    lru_cache can hash it; values inside are still the original dicts."""
    raw = json.loads(Path(json_path).read_text("utf-8"))
    return tuple(raw)


def source_frags_for(predict_csv: str) -> Path:
    """Map '_jactf_new_frags.ensemble_predictions.csv' -> _jactf_new_frags.json.
    Lives in PREDICT_DIR alongside the CSV."""
    base = Path(predict_csv).name
    if base.endswith(".ensemble_predictions.csv"):
        stem = base[: -len(".ensemble_predictions.csv")]
    else:
        stem = Path(base).stem
    return PREDICT_DIR / f"{stem}.json"


def find_frag(source_path: Path, time_raw: int, source_demo: str) -> Optional[dict]:
    """Locate the canonical frag record (with the full feature vector) by
    matching time_raw and normalised _source_file.  Returns None if not
    found — the caller decides whether that's a hard error."""
    if not source_path.exists():
        return None
    norm_expected = normalise_name(source_demo)
    for frag in _load_source(str(source_path)):
        if int(frag.get("time_raw", -1)) != int(time_raw):
            continue
        if normalise_name(frag.get("_source_file", "")) == norm_expected:
            return frag
    return None


# ── label apply / remove ─────────────────────────────────────────────────
def _remove_from_corpus(corpus: list[dict], key: str) -> list[dict]:
    return [f for f in corpus if dedup_key(f) != key]


def apply_label(clip: dict, label: str) -> dict:
    """Move (or place) a clip's frag into the aggregated file for `label`.
    If the clip is currently labeled differently, remove from that corpus
    too.  Returns a status dict for logging / UI feedback."""
    if label not in LABELS:
        return {"ok": False, "error": f"unknown label {label!r}"}

    cid          = clip["clip_id"]
    src_path     = source_frags_for(clip.get("predict_csv", ""))
    frag = find_frag(src_path, int(clip["time_raw"]), clip["source_demo"])
    if frag is None:
        return {
            "ok": False,
            "error": (
                f"could not locate frag for {cid} in {src_path.name} "
                f"(time_raw={clip['time_raw']}, source_demo={clip['source_demo']!r})"
            ),
        }

    tagged = dict(frag)
    tagged.update({
        "consensus":    label,
        "label_source": "review_ui",
        "label_ts":     now_ts(),
    })
    key = dedup_key(tagged)

    state = load_state()
    prior = state.get(cid, {}).get("label")

    # remove from prior corpus if this is a re-label
    if prior and prior != label and prior in LABELS:
        prior_path = AGGREGATED_FILE[prior]
        prior_corpus = load_corpus(prior)
        new_prior = _remove_from_corpus(prior_corpus, key)
        if len(new_prior) != len(prior_corpus):
            _backup(prior_path)
            _atomic_write(prior_path, new_prior)

    # add to new corpus (idempotent)
    target_path = AGGREGATED_FILE[label]
    corpus = load_corpus(label)
    if any(dedup_key(f) == key for f in corpus):
        added = False
    else:
        _backup(target_path)
        corpus.append(tagged)
        _atomic_write(target_path, corpus)
        added = True

    # update sidecar state
    state[cid] = {
        "label":    label,
        "ts":       now_ts(),
        "frag_key": key,
    }
    save_state(state)

    return {
        "ok":       True,
        "clip_id":  cid,
        "label":    label,
        "added":    added,
        "prior":    prior,
        "frag_key": key,
        "corpus_size": len(corpus),
    }


def remove_label(clip: dict) -> dict:
    """Undo: clear a clip's label and remove from whichever aggregated file
    it currently lives in."""
    cid   = clip["clip_id"]
    state = load_state()
    entry = state.get(cid)
    if not entry:
        return {"ok": True, "clip_id": cid, "removed_from": None, "noop": True}

    label = entry.get("label")
    key   = entry.get("frag_key")
    if not (label in LABELS and key):
        # malformed state — clear it
        state.pop(cid, None)
        save_state(state)
        return {"ok": True, "clip_id": cid, "removed_from": None, "noop": True}

    target_path = AGGREGATED_FILE[label]
    corpus = load_corpus(label)
    new_corpus = _remove_from_corpus(corpus, key)
    if len(new_corpus) != len(corpus):
        _backup(target_path)
        _atomic_write(target_path, new_corpus)

    state.pop(cid, None)
    save_state(state)
    return {
        "ok":           True,
        "clip_id":      cid,
        "removed_from": label,
        "corpus_size":  len(new_corpus),
    }


# ── review_state.json ────────────────────────────────────────────────────
def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except json.JSONDecodeError:
        # don't lose data on a bad parse — back it up and start fresh
        backup = STATE_FILE.with_name(f"review_state.corrupt-{int(time.time())}.json")
        STATE_FILE.rename(backup)
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ── session lifecycle ────────────────────────────────────────────────────
def get_or_create_session() -> dict:
    """Return the current session marker, creating one with a fresh
    started_at + initial corpus snapshot if none exists.

    Called from app.main() at server startup so 'session duration' covers
    the full time the server's been running, not just from the first vote.
    """
    if CURRENT_SESSION_FILE.exists():
        try:
            return json.loads(CURRENT_SESSION_FILE.read_text("utf-8"))
        except json.JSONDecodeError:
            pass  # corrupt — fall through to create a fresh one
    sess = {
        "started_at":            now_ts(),
        "initial_corpus_sizes":  {l: len(load_corpus(l)) for l in LABELS},
    }
    CURRENT_SESSION_FILE.write_text(json.dumps(sess, indent=2), encoding="utf-8")
    return sess


def finalize_session(manifest: list[dict]) -> dict:
    """Snapshot the session: write a durable session log to sessions/,
    clear current_session.json so the next vote / next-server-start opens
    a fresh session.  Returns the same data that was written, with the
    log_path added, so the API can hand it straight to the UI."""
    sess  = get_or_create_session()
    state = load_state()

    clips = []
    counts = {l: 0 for l in LABELS}
    counts["unset"] = 0
    for entry in manifest:
        cid    = entry["clip_id"]
        s      = state.get(cid, {})
        label  = s.get("label")
        counts[label or "unset"] = counts.get(label or "unset", 0) + 1
        clips.append({
            "clip_id":     cid,
            "label":       label,
            "weapon":      entry.get("mod_name", "?"),
            "score":       entry.get("score", 0.0),
            "human_time":  entry.get("human_time", "?"),
            "source_demo": entry.get("source_demo", "?"),
            "time_raw":    entry.get("time_raw", 0),
            "labeled_at":  s.get("ts"),
            "frag_key":    s.get("frag_key"),
        })

    final_corpus = {l: len(load_corpus(l)) for l in LABELS}
    delta = {l: final_corpus[l] - sess["initial_corpus_sizes"].get(l, 0)
             for l in LABELS}

    log = {
        "started_at":            sess["started_at"],
        "finished_at":           now_ts(),
        "manifest_size":         len(manifest),
        "label_counts":          counts,
        "initial_corpus_sizes":  sess["initial_corpus_sizes"],
        "final_corpus_sizes":    final_corpus,
        "corpus_delta":          delta,
        "clips":                 clips,
    }

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = SESSIONS_DIR / f"session_{time.strftime('%Y%m%d_%H%M%S')}.json"
    log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")

    # Clear the marker so the next vote (or next server start) begins a
    # fresh session.  We don't touch review_state.json — the labels are
    # legitimately still in place and re-opening the UI shows them as such.
    if CURRENT_SESSION_FILE.exists():
        try: CURRENT_SESSION_FILE.unlink()
        except OSError: pass

    return {**log, "log_path": str(log_path)}
