#!/usr/bin/env python3
"""
review/app.py — Flask review UI for rendered highlight clips.

Reads clip_manifest.json, plays each fNNNN.mp4 from mme/captures/, lets the
user thumbs-up / ok / down / ignore each one, and merges the verdict into the
training corpus (good/bad/ok/ignore_client_frags_aggregated.json) used by
predict_frags_ensemble.py.

Run it:
    cd <repo-root>\\python\\review
    .\\run.bat
    # then open http://127.0.0.1:5057/

The merge logic lives in label_io.py (deliberately separated from the web
layer so a CLI bulk tool could reuse it).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory, abort

import label_io

# ── paths ────────────────────────────────────────────────────────────────
# review/app.py → up 2 = repo root.  JACTF_ROOT and JACTF_GAMEDATA override.
ROOT          = Path(os.environ.get("JACTF_ROOT") or Path(__file__).resolve().parents[2])
GAMEDATA      = Path(os.environ.get("JACTF_GAMEDATA")
                     or (ROOT / "a full jka install" / "game_directory" / "Jedi Academy" / "GameData"))
MANIFEST_PATH = GAMEDATA / "mme" / "clip_manifest.json"
CAPTURES_DIR  = GAMEDATA / "mme" / "captures"

app = Flask(__name__, template_folder="templates", static_folder="static")
# stop browser caching so a relabel + refresh shows current state
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


# ── helpers ──────────────────────────────────────────────────────────────
def load_manifest() -> list[dict]:
    """clip_manifest.json may have trailing NULs from a sandbox-mount
    artifact (we observed this) — be tolerant.  Returns [] on any error."""
    if not MANIFEST_PATH.exists():
        return []
    raw = MANIFEST_PATH.read_bytes()
    # strip trailing NUL padding if any
    raw = raw.rstrip(b"\x00").rstrip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # last resort: try to find the closing bracket and parse to there
        end = raw.rfind(b"]")
        if end > 0:
            try:
                return json.loads(raw[: end + 1])
            except json.JSONDecodeError:
                pass
        return []


def has_capture(cid: str) -> bool:
    return (CAPTURES_DIR / f"{cid}.mp4").is_file()


def manifest_with_state() -> list[dict]:
    manifest = load_manifest()
    state = label_io.load_state()
    out = []
    for entry in manifest:
        cid = entry["clip_id"]
        s = state.get(cid, {})
        out.append({
            "clip_id":       cid,
            "weapon":        entry.get("mod_name", "?"),
            "attacker_name": entry.get("attacker_name", "?"),
            "target_name":   entry.get("target_name", "?"),
            "score":         entry.get("score", 0.0),
            "human_time":    entry.get("human_time", "?"),
            "source_demo":   entry.get("source_demo", "?"),
            "predict_csv":   entry.get("predict_csv", ""),
            "trimmed_clip":  entry.get("trimmed_clip", ""),
            "time_raw":      entry.get("time_raw", 0),
            "label":         s.get("label"),
            "label_ts":      s.get("ts"),
            "has_video":     has_capture(cid),
        })
    return out


# ── routes: pages ────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ── routes: video ────────────────────────────────────────────────────────
@app.route("/video/<cid>.mp4")
def serve_video(cid: str):
    # Path traversal guard — cid must look like a clip id (fNNNN).
    if not (cid.startswith("f") and len(cid) == 5 and cid[1:].isdigit()):
        abort(400, "bad clip id")
    if not has_capture(cid):
        abort(404, "capture not rendered")
    return send_from_directory(
        CAPTURES_DIR, f"{cid}.mp4",
        mimetype="video/mp4",
        conditional=True,  # honor Range so the player can seek
    )


# ── routes: api ──────────────────────────────────────────────────────────
@app.route("/api/clips")
def api_clips():
    clips = manifest_with_state()
    counts = {l: 0 for l in label_io.LABELS}
    counts["unset"] = 0
    for c in clips:
        counts[c["label"] or "unset"] = counts.get(c["label"] or "unset", 0) + 1
    return jsonify({
        "clips":  clips,
        "counts": counts,
        "total":  len(clips),
    })


@app.route("/api/label", methods=["POST"])
def api_label():
    body = request.get_json(silent=True) or {}
    cid   = body.get("clip_id", "")
    label = body.get("label", "")

    manifest = load_manifest()
    clip = next((m for m in manifest if m.get("clip_id") == cid), None)
    if clip is None:
        return jsonify({"ok": False, "error": f"unknown clip_id {cid!r}"}), 404

    if label == "unset":
        result = label_io.remove_label(clip)
    elif label in label_io.LABELS:
        result = label_io.apply_label(clip, label)
    else:
        return jsonify({"ok": False, "error": f"unknown label {label!r}"}), 400

    return jsonify(result), (200 if result.get("ok") else 500)


@app.route("/api/state")
def api_state():
    return jsonify(label_io.load_state())


@app.route("/api/finish", methods=["POST"])
def api_finish():
    """Snapshot the session, write a durable log, return the summary.
    The server stays up after this — shutdown is a separate explicit step
    so the user can review the summary before terminating."""
    manifest = load_manifest()
    summary = label_io.finalize_session(manifest)
    # Build the suggested next-step command using the actual ROOT so the post-
    # shutdown screen shows the right path on every machine.
    predict_dir = ROOT / "python" / "predict"
    next_cmd = (
        f"cd {predict_dir}\n"
        f".\\venv\\Scripts\\python.exe predict_frags_ensemble.py "
        f"{predict_dir / '_jactf_new_frags.json'}"
    )
    return jsonify({"ok": True, "next_cmd": next_cmd, **summary})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Terminate the server cleanly.  Sends the response first, then
    os._exit(0) on a 300ms-delayed daemon thread so the browser actually
    receives the 'closed' acknowledgement before the socket dies.

    werkzeug.server.shutdown is deprecated in current Flask; os._exit is
    the simplest reliable mechanism on Windows."""
    def _kill():
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=_kill, daemon=True).start()
    return jsonify({"ok": True, "message": "shutting down"})


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "not found"}), 404
    return ("not found", 404)


# ── main ─────────────────────────────────────────────────────────────────
def main():
    # Open a session on server start so duration tracking is meaningful even
    # if the user just looks at clips for a while before voting.
    label_io.get_or_create_session()
    # bind localhost only — this is a local review tool, not a service
    app.run(host="127.0.0.1", port=5057, debug=False)


if __name__ == "__main__":
    main()
