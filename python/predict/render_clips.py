#!/usr/bin/env python3
"""
render_clips.py — resilient runner for the jaMME clip batch.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── paths ─────────────────────────────────────────────────────────────────────
ROOT         = Path(os.environ.get("JACTF_ROOT") or Path(__file__).resolve().parents[2])
GAMEDATA     = Path(os.environ.get("JACTF_GAMEDATA")
                    or (ROOT / "a full jka install" / "game_directory" / "Jedi Academy" / "GameData"))
JAMME_EXE    = GAMEDATA / "jamme.exe"
MME_ROOT     = GAMEDATA / "mme"
MME_CAPTURES = MME_ROOT / "captures"
MME_LOGS     = MME_ROOT / "render_logs"
MANIFEST     = MME_ROOT / "clip_manifest.json"
STATE_FILE   = MME_ROOT / "render_state.json"
SINGLE_LIST  = MME_ROOT / "_single_clip.txt"

def capture_path_for(cid: str) -> Path:
    # Under default settings, jaMME writes here
    return MME_ROOT / "capture" / cid / "clip.mp4"

DEFAULT_TIMEOUT_S    = 180
DEFAULT_MAX_RETRIES  = 2
MIN_CLIP_BYTES       = 100_000
FFMPEG_MAX_FINALIZE_S = 90
FFMPEG_STABLE_S      = 2.0

JAMME_BASE_ARGS = [
    "+set", "fs_homepath",     ".",
    "+set", "cl_renderer",     "rd-jamme",
    "+set", "fs_game",         "mme",
    "+set", "r_fullscreen",    "0",
    "+set", "r_noborder",      "0",
    "+set", "r_centerWindow",  "0",
    "+set", "r_mode",          "-1",
    "+set", "r_customwidth",   "1280",
    "+set", "r_customheight",  "720",
    "+set", "r_picmip",        "0",
    "+set", "mme_demoListQuit","1",
    "+set", "mme_demoAutoQuit","1",
    "+set", "mme_demoEscapeQuit","1",
    "+set", "logfile",         "1",
    "+set", "com_developer",   "1",
    "+exec", "mmeconfig.cfg",
]

# ── state management ──
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def load_state() -> dict:
    if not STATE_FILE.is_file():
        return {"clips": {}, "runs": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"clips": {}, "runs": []}

def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)

# ── invocation ────────────────────────────────────────────────────────────────
def render_one(cid: str, timeout_s: int, log_path: Path) -> tuple[str, str, int]:
    cap = capture_path_for(cid)
    for stale in (cap, GAMEDATA / "clip.mp4"):
        if stale.exists():
            try: stale.unlink()
            except OSError: pass

    SINGLE_LIST.parent.mkdir(parents=True, exist_ok=True)
    SINGLE_LIST.write_text(f'"{cid}" "{cid}"\n', encoding="utf-8")

    cmd = [str(JAMME_EXE)] + JAMME_BASE_ARGS + [
        "+set", "mov_captureName", cid,
        "+demoList", SINGLE_LIST.name,
    ]

    log_path.parent.mkdir(parents=True, exist_ok=True)
    exit_code = -1
    timed_out = False

    qconsole = MME_ROOT / "qconsole.log"
    if qconsole.exists():
        try: qconsole.unlink()
        except OSError: pass

    with log_path.open("wb") as logf:
        logf.write(f"# {cid} started {now_iso()}\n".encode("utf-8"))
        logf.write(f"# cmd: {' '.join(cmd)}\n\n".encode("utf-8"))
        logf.flush()
        try:
            proc = subprocess.run(
                cmd, 
                cwd=str(GAMEDATA), 
                stdout=logf, 
                stderr=subprocess.STDOUT, 
                timeout=timeout_s
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
        except OSError as e:
            return ("crash", f"OSError: {e}", -1)

        if qconsole.exists():
            try:
                logf.write(b"\n# --- mme/qconsole.log ---\n")
                logf.write(qconsole.read_bytes())
            except OSError: pass

    if timed_out:
        return ("timeout", f"jaMME exceeded {timeout_s}s", -1)

    deadline = time.time() + FFMPEG_MAX_FINALIZE_S
    last_size = -1
    stable_since = 0.0
    while time.time() < deadline:
        if cap.is_file():
            sz = cap.stat().st_size
            if sz > 0:
                if sz == last_size:
                    if (time.time() - stable_since) >= FFMPEG_STABLE_S: break
                else:
                    last_size = sz
                    stable_since = time.time()
        time.sleep(0.5)

    if not cap.is_file():
        return ("no_output", f"jaMME exit={exit_code}, no mp4 produced", exit_code)
    
    size = cap.stat().st_size
    if size < MIN_CLIP_BYTES:
        try: cap.unlink()
        except OSError: pass
        return ("tiny_output", f"size {size}B too small", exit_code)

    MME_CAPTURES.mkdir(parents=True, exist_ok=True)
    dst = MME_CAPTURES / f"{cid}.mp4"
    if dst.exists(): dst.unlink()
    shutil.move(str(cap), str(dst))
    return ("ok", f"size={size:,}B exit={exit_code}", exit_code)

def fmt_eta(s: float) -> str:
    if s < 60: return f"{s:.0f}s"
    return f"{s/60:.1f}m"

def print_summary(state: dict, clip_ids: list) -> None:
    by_status = {}
    for cid in clip_ids:
        st = state["clips"].get(cid, {"status": "untouched"})["status"]
        by_status[st] = by_status.get(st, 0) + 1
    print(f"\nstate summary ({len(clip_ids)} total):")
    for k in ("done", "pending", "in_progress", "failed"):
        if by_status.get(k): print(f"    {k:<12} {by_status[k]}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if not JAMME_EXE.is_file() or not MANIFEST.is_file():
        sys.exit("Error: missing jaMME or manifest")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    clip_ids = [m["clip_id"] for m in manifest]
    state = {"clips": {}, "runs": []} if args.reset else load_state()
    state.setdefault("clips", {})
    state.setdefault("runs", [])

    healed = 0
    for cid, s in state["clips"].items():
        if s.get("status") == "in_progress":
            s["status"] = "pending"
            s["last_message"] = "interrupted before completion (healed on restart)"
            healed += 1
    if healed:
        print(f"  healed {healed} interrupted clip(s) -> pending")

    todo = []
    for cid in (args.only if args.only else clip_ids):
        if cid not in clip_ids: continue
        s = state["clips"].setdefault(cid, {"status": "pending", "attempts": 0})
        
        if s["status"] == "done":
            mp4 = MME_CAPTURES / f"{cid}.mp4"
            demo = MME_ROOT / "demos" / f"{cid}.dm_26"
            if mp4.is_file() and mp4.stat().st_size >= MIN_CLIP_BYTES:
                if demo.is_file() and mp4.stat().st_mtime < demo.stat().st_mtime:
                    s["status"] = "pending"
            else:
                s["status"] = "pending"
        
        if s["status"] == "pending" or (s["status"] == "failed" and s["attempts"] <= args.max_retries):
            todo.append(cid)

    if args.status or not todo:
        print_summary(state, clip_ids)
        return

    if args.limit > 0: todo = todo[:args.limit]
    print(f"Rendering {len(todo)} clips sequentially...")

    state["runs"].append({
        "started": now_iso(),
        "scheduled": len(todo),
    })
    save_state(state)

    n_ok = n_fail = 0
    t_start = time.time()

    try:
        for i, cid in enumerate(todo, 1):
            s = state["clips"][cid]
            s["status"] = "in_progress"
            s["last_attempt_at"] = now_iso()
            save_state(state)

            elapsed = time.time() - t_start
            avg = elapsed / max(i - 1, 1) if i > 1 else 0
            eta = avg * (len(todo) - i + 1) if i > 1 else 0
            attempts_so_far = s.get("attempts", 0)
            
            print(f"[{i}/{len(todo)}] {cid}  (attempt {attempts_so_far + 1}/{args.max_retries + 1})  "
                  f"ok={n_ok} fail={n_fail}  elapsed={fmt_eta(elapsed)}  eta={fmt_eta(eta)}")

            t0 = time.time()
            log_path = MME_LOGS / f"{cid}.log"
            status, msg, exit_code = render_one(cid, args.timeout, log_path)
            dur = time.time() - t0

            s["attempts"] = attempts_so_far + 1
            s["last_duration_s"] = round(dur, 1)
            s["last_message"] = msg
            s["last_exit_code"] = exit_code
            s["last_attempt_at"] = now_iso()

            if status == "ok":
                s["status"] = "done"
                s["mp4_size"] = (MME_CAPTURES / f"{cid}.mp4").stat().st_size
                s["rendered_at"] = now_iso()
                n_ok += 1
                print(f"      OK   {dur:.1f}s  {msg}")
            elif s["attempts"] >= args.max_retries + 1:
                s["status"] = "failed"
                n_fail += 1
                print(f"      FAIL ({status}: {msg}) — retries exhausted")
            else:
                s["status"] = "pending"
                print(f"      retry-queued ({status}: {msg})")

            save_state(state)

    except KeyboardInterrupt:
        for cid, s in state["clips"].items():
            if s.get("status") == "in_progress":
                s["status"] = "pending"
                s["last_message"] = "interrupted by user (Ctrl+C)"
        save_state(state)
        print("\n^C — saved state, exiting cleanly. Re-run to resume.")
        sys.exit(130)

    elapsed = time.time() - t_start
    print(f"\n=== run finished in {fmt_eta(elapsed)} ===")
    print(f"  ok this run:     {n_ok}")
    print(f"  failed this run: {n_fail}")
    print_summary(state, clip_ids)

if __name__ == "__main__":
    main()
