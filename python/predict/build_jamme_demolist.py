#!/usr/bin/env python3
"""
build_jamme_demolist.py — bridge predict_frags_ensemble output into jaMME render queue.

Reads all `*.ensemble_predictions.csv` produced by predict_frags_ensemble.py,
filters consensus=='good', ensures each good frag has a 12s trimmed clip
(auto-trims via DemoTrimmer.exe if missing), then stages clips into the jaMME
demo folder as f0001..fNNNN, with a manifest mapping every f-number back to
the source frag (for review/feedback later).

Also generates `render_clips_batch.bat` — a Windows batch that invokes jaMME
once per clip with `+demo fNNNN`, then renames the output `clip.mp4` to
`captures\fNNNN.mp4`. (jaMME's pipe command always writes to a fixed name,
so we rename between runs.)

Usage:
  python build_jamme_demolist.py
  python build_jamme_demolist.py --limit 20
  python build_jamme_demolist.py --predict-glob "<repo-root>/python/predict/xen*.csv"
  python build_jamme_demolist.py --dry-run
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

# ── paths ─────────────────────────────────────────────────────────────────────
# ROOT auto-detects: predict/<file>.py → up 2 = repo root.  JACTF_ROOT overrides.
ROOT          = Path(os.environ.get("JACTF_ROOT") or Path(__file__).resolve().parents[2])
PREDICT_DIR   = ROOT / "python" / "predict"
TRIM_DIR      = ROOT / "python" / "trimming"
DEMO_SOURCE   = TRIM_DIR / "demos source"
DEMO_OUTPUT   = TRIM_DIR / "demos output"
TRIMMER_EXE   = TRIM_DIR / "DemoTrimmer.exe"
# GAMEDATA defaults to in-repo; override with JACTF_GAMEDATA on machines where
# jaMME lives elsewhere (most users).
GAMEDATA      = Path(os.environ.get("JACTF_GAMEDATA")
                     or (ROOT / "a full jka install" / "game_directory" / "Jedi Academy" / "GameData"))
MME_ROOT      = GAMEDATA / "mme"
MME_DEMOS     = MME_ROOT / "demos"
MME_CAPTURES  = MME_ROOT / "captures"
MME_PROJECTS  = MME_ROOT / "project"

# jaMME project template — gets written as mme/project/<cid>/<cid>.cfg for
# every staged clip.  The <capture> block makes jaMME auto-record on demo
# load; output lands at mme/capture/<cid>/clip.mp4.  Without a per-clip
# project file, jaMME tries to use one whose .mme is paired to a different
# demo and bails with "Couldn't load project ...".
PROJECT_CFG_TEMPLATE = """<capture>
\t<start>0</start>
\t<end>12000</end>
\t<speed>1.00000000</speed>
\t<view>chase</view>
</capture>
<line>
\t<start>0</start>
\t<end>12000</end>
</line>
"""

PADDING_MS = 6_000  # ±6s window around the kill, matches trim_good_frags.py
DEFAULT_PREDICT_GLOB = str(PREDICT_DIR / "*.ensemble_predictions.csv")


# ── helpers ───────────────────────────────────────────────────────────────────
def ms_to_hhmmss(ms: int) -> str:
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def extract_demo_name(src_field: str) -> Optional[str]:
    """`xxx.dm_26.dm_meta` -> `xxx.dm_26`"""
    suf = ".dm_26.dm_meta"
    i = src_field.find(suf)
    if i != -1:
        return src_field[: i + len(".dm_26")]
    base = os.path.basename(src_field)
    return base[: -len(".dm_meta")] if base.endswith(suf) else None


def find_demo_file(name: str) -> Optional[Path]:
    """walk demos source/ for a .dm_26 by basename."""
    for d, _, files in os.walk(DEMO_SOURCE):
        if name in files:
            return Path(d) / name
    return None


def find_trimmed_path(trimmed_name: str) -> Optional[Path]:
    """walk demos output/ for a trimmed .dm_26 by basename."""
    for d, _, files in os.walk(DEMO_OUTPUT):
        if trimmed_name in files:
            return Path(d) / trimmed_name
    return None


def human_to_ms(ts: str) -> Optional[int]:
    """HH:MM:SS(.mmm) or MM:SS(.mmm) -> milliseconds.  Returns match-time ms.
    DemoTrimmer.exe interprets the timestamps it receives as match-time, NOT
    demo-time (which is what `time_raw` contains).  JACTF demos record warmup
    before the match starts so demo-time != match-time — passing time_raw
    directly to DemoTrimmer cuts at the wrong moment in the match."""
    s = (ts or "").strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) == 3:
        hh, mm, rest = parts
    elif len(parts) == 2:
        hh = "0"; mm, rest = parts
    else:
        return None
    if "." in rest:
        ss, ms = rest.split(".", 1)
        ms = ms.ljust(3, "0")[:3]
    else:
        ss, ms = rest, "000"
    try:
        return (int(hh) * 3600 + int(mm) * 60 + int(ss)) * 1000 + int(ms)
    except ValueError:
        return None


def safe_mmss_ms(human: str) -> str:
    s = (human or "").strip()
    if not s or ":" not in s:
        return "00-00.000"
    if s.count(":") == 2:
        s = s.split(":", 1)[1]
    if "." not in s:
        s += ".000"
    mm, rest = s.split(":", 1)
    ss, ms = rest.split(".")
    return f"{mm.zfill(2)}-{ss}.{ms.ljust(3, '0')[:3]}"


def short_stem(demo_name: str, limit: int = 15) -> str:
    return Path(demo_name).stem.replace(".dm_26", "")[:limit]


# ── trimmed_map.json ──────────────────────────────────────────────────────────
def load_trimmed_map() -> dict:
    p = DEMO_OUTPUT / "trimmed_map.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def save_trimmed_map(m: dict) -> None:
    p = DEMO_OUTPUT / "trimmed_map.json"
    p.write_text(json.dumps(m, indent=2), encoding="utf-8")


def index_by_demo_time(trimmed_map: dict) -> dict:
    """build {(full_demo, time_raw): trimmed_filename}."""
    idx = {}
    for trimmed_name, info in trimmed_map.items():
        try:
            key = (info.get("full_demo", ""), int(info.get("time_raw", -1)))
        except (TypeError, ValueError):
            continue
        idx[key] = trimmed_name
    return idx


# ── trimming ──────────────────────────────────────────────────────────────────
def trim_one(src: Path, dst: Path, center_ms: int) -> bool:
    start_ms = max(0, center_ms - PADDING_MS)
    end_ms = center_ms + PADDING_MS
    if end_ms - start_ms < 1_000:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.check_call(
            [str(TRIMMER_EXE), str(src), str(dst),
             ms_to_hhmmss(start_ms), ms_to_hhmmss(end_ms)],
            cwd=str(TRIM_DIR),
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"  [trim fail] {src.name}: {e}", file=sys.stderr)
        return False


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--csv", nargs="+", default=None, metavar="PATH",
                    help="explicit CSV path(s) to consume. overrides --predict-glob. "
                         "Used by run_pipeline.ps1 to pass only the just-produced CSVs.")
    ap.add_argument("--predict-glob", default=DEFAULT_PREDICT_GLOB,
                    help=f"glob for predict CSV(s) (default: {DEFAULT_PREDICT_GLOB}). "
                         f"Only used when --csv is not specified.")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap N clips (0 = all). useful for testing.")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be staged; don't trim, copy, or write.")
    ap.add_argument("--keep-existing", action="store_true",
                    help="don't wipe existing f*.dm_26 from mme/demos/.")
    args = ap.parse_args()

    if not TRIMMER_EXE.is_file():
        sys.exit(f"missing trimmer: {TRIMMER_EXE}")

    # --- 1. load predict CSVs -------------------------------------------------
    if args.csv:
        csv_paths = sorted(args.csv)
    else:
        csv_paths = sorted(glob.glob(args.predict_glob))
    if not csv_paths:
        sys.exit(f"no predict CSVs to consume "
                 f"({'--csv: ' + ','.join(args.csv) if args.csv else '--predict-glob: ' + args.predict_glob})")
    print(f"loading {len(csv_paths)} predict CSV(s)...")
    dfs = []
    for p in csv_paths:
        d = pd.read_csv(p)
        d["_predict_csv"] = Path(p).name
        dfs.append(d)
        print(f"    {Path(p).name}: {len(d)} rows")
    df = pd.concat(dfs, ignore_index=True)
    if "consensus" not in df.columns:
        sys.exit("predict CSV missing 'consensus' column")
    good = df[df["consensus"].astype(str).str.lower() == "good"].copy()
    print(f"    total: {len(df)}  good: {len(good)}")
    if good.empty:
        sys.exit("no good frags to render.")

    # sort by score desc so f0001 = highest-confidence
    if "score" in good.columns:
        good = good.sort_values("score", ascending=False, na_position="last")
    good = good.reset_index(drop=True)
    if args.limit and args.limit > 0:
        good = good.head(args.limit).copy()
        print(f"    --limit {args.limit} -> {len(good)} clips")

    # --- 2. resolve / trim each row -------------------------------------------
    trimmed_map = load_trimmed_map()
    by_key = index_by_demo_time(trimmed_map)
    staged = []  # list of (row, clip_path)
    new_trims = 0
    skipped = 0

    print(f"\nresolving {len(good)} clip(s)...")
    for idx, row in good.iterrows():
        demo_name = extract_demo_name(str(row.get("_source_file", "")))
        try:
            time_raw = int(row.get("time_raw"))
        except (TypeError, ValueError):
            print(f"  [skip] row {idx}: bad time_raw")
            skipped += 1; continue
        if not demo_name:
            print(f"  [skip] row {idx}: bad _source_file")
            skipped += 1; continue

        # already trimmed?
        existing_name = by_key.get((demo_name, time_raw))
        clip_path = find_trimmed_path(existing_name) if existing_name else None

        if clip_path is None:
            src = find_demo_file(demo_name)
            if src is None:
                print(f"  [skip] row {idx}: demo not found: {demo_name}")
                skipped += 1; continue
            human = str(row.get("human_time", ""))
            # DemoTrimmer.exe expects MATCH-time (post-warmup, derived from
            # human_time).  Passing time_raw directly cuts at the wrong moment
            # because JACTF demos record warmup before the match.
            center_ms = human_to_ms(human)
            if center_ms is None:
                print(f"  [skip] row {idx}: bad human_time: {human!r}")
                skipped += 1; continue
            mmssms = safe_mmss_ms(human)
            stem = short_stem(demo_name)
            out_dir = DEMO_OUTPUT / os.path.relpath(src.parent, DEMO_SOURCE)
            trimmed_name = f"trm_{stem}_{mmssms}_{idx}.dm_26"
            clip_path = out_dir / trimmed_name
            if args.dry_run:
                print(f"  [dry] would trim: {demo_name} @ match-time {center_ms}ms -> {trimmed_name}")
            else:
                if not trim_one(src, clip_path, center_ms):
                    skipped += 1; continue
                # NB: by_key index uses time_raw (stable across runs) but the
                # actual cut was at center_ms (match-time).  The stored entry
                # records both for traceability.
                trimmed_map[trimmed_name] = {
                    "full_demo": demo_name, "time_raw": time_raw, "match_ms": center_ms,
                }
                by_key[(demo_name, time_raw)] = trimmed_name
                new_trims += 1
                print(f"  [trim] {trimmed_name}")
        staged.append((row, clip_path))

    print(f"\n  resolved: {len(staged)}, new trims: {new_trims}, skipped: {skipped}")
    if not staged:
        sys.exit("no clips to stage.")

    if not args.dry_run and new_trims > 0:
        save_trimmed_map(trimmed_map)
        print(f"  updated trimmed_map.json (+{new_trims} entries)")

    # --- 3. stage f-numbered clips into mme/demos/ ----------------------------
    if not args.dry_run:
        MME_DEMOS.mkdir(parents=True, exist_ok=True)
        MME_CAPTURES.mkdir(parents=True, exist_ok=True)
        MME_PROJECTS.mkdir(parents=True, exist_ok=True)
        if not args.keep_existing:
            removed = 0
            for p in MME_DEMOS.glob("f????.*"):
                try:
                    p.unlink(); removed += 1
                except OSError:
                    pass
            if removed:
                print(f"  wiped {removed} stale f*.* file(s) from mme/demos/")
            # Wipe stale per-clip project folders too
            removed_proj = 0
            for d in MME_PROJECTS.glob("f????"):
                if d.is_dir():
                    try:
                        shutil.rmtree(d); removed_proj += 1
                    except OSError:
                        pass
            if removed_proj:
                print(f"  wiped {removed_proj} stale f???? project folder(s)")
            # Wipe stale jaMME-format .mme cache files in mme/mmedemos/.
            # jaMME records a per-demo .mme alongside the .dm_26 on first
            # playback and re-uses it on subsequent loads — if we replace
            # f0001.dm_26 with a new clip but leave the old f0001.mme,
            # jaMME plays the cached content and produces no fresh capture.
            mmedemos_dir = MME_ROOT / "mmedemos"
            removed_mme = 0
            for p in mmedemos_dir.glob("f????.mme"):
                try:
                    p.unlink(); removed_mme += 1
                except OSError:
                    pass
            if removed_mme:
                print(f"  wiped {removed_mme} stale f????.mme cache file(s)")
            # Wipe stale capture/f????/ output folders so we never get
            # confused by leftover mp4s from prior runs of this clip-id.
            cap_dir = MME_ROOT / "capture"
            removed_cap = 0
            for d in cap_dir.glob("f????"):
                if d.is_dir():
                    try:
                        shutil.rmtree(d); removed_cap += 1
                    except OSError:
                        pass
            if removed_cap:
                print(f"  wiped {removed_cap} stale capture/f???? folder(s)")

    width = max(4, len(str(len(staged))))
    manifest = []
    demolist_lines = []
    print(f"\nstaging {len(staged)} clip(s) into mme/demos/...")
    for i, (row, clip_path) in enumerate(staged, 1):
        clip_id = f"f{i:0{width}d}"
        dst = MME_DEMOS / f"{clip_id}.dm_26"
        if not args.dry_run:
            shutil.copy2(clip_path, dst)
            meta_src = Path(str(clip_path) + ".dm_meta")
            if meta_src.is_file():
                shutil.copy2(meta_src, MME_DEMOS / f"{clip_id}.dm_26.dm_meta")
            # Write per-clip jaMME project so its <capture> block fires when
            # this demo loads.  Each clip gets its own folder so captures
            # don't collide (output: mme/capture/<clip_id>/clip.mp4).
            proj_dir = MME_PROJECTS / clip_id
            proj_dir.mkdir(parents=True, exist_ok=True)
            (proj_dir / f"{clip_id}.cfg").write_text(PROJECT_CFG_TEMPLATE, encoding="utf-8")
        demolist_lines.append(f'"{clip_id}" "{clip_id}"')

        def _v(k, default=None):
            v = row.get(k, default)
            try:
                return None if pd.isna(v) else v
            except (TypeError, ValueError):
                return v

        manifest.append({
            "clip_id":       clip_id,
            "trimmed_clip":  clip_path.name,
            "source_demo":   _v("_source_file"),
            "time_raw":      int(row.get("time_raw")),
            "human_time":    _v("human_time"),
            "mod_name":      _v("mod_name"),
            "score":         _v("score"),
            "consensus":     _v("consensus"),
            "attacker":      _v("attacker"),
            "attacker_name": _v("attacker_name"),
            "target":        _v("target"),
            "target_name":   _v("target_name"),
            "predict_csv":   _v("_predict_csv"),
        })

    # --- 4. write demolist + manifest -----------------------------------------
    if not args.dry_run:
        (MME_ROOT / "frags.txt").write_text(
            "\n".join(demolist_lines) + "\n", encoding="utf-8"
        )
        (MME_ROOT / "clip_manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str), encoding="utf-8"
        )
        print(f"  wrote {MME_ROOT/'frags.txt'} ({len(demolist_lines)} entries)")
        print(f"  wrote {MME_ROOT/'clip_manifest.json'}")

    # --- 5. generate render_clips_batch.bat ----------------------------------
    bat_path = GAMEDATA / "render_clips_batch.bat"
    if not args.dry_run:
        lines = [
            "@echo off",
            "REM auto-generated by build_jamme_demolist.py — do not edit by hand",
            "setlocal enabledelayedexpansion",
            'cd /d "%~dp0"',
            "",
            "if not exist jamme.exe (echo ERROR: jamme.exe not found in %CD% & pause & exit /b 1)",
            "if not exist mme\\captures mkdir mme\\captures",
            "",
            f"set TOTAL={len(manifest)}",
            "set DONE=0",
            "set START_TIME=%TIME%",
            "",
        ]
        for m in manifest:
            cid = m["clip_id"]
            lines += [
                f"REM ---- {cid} ({m.get('mod_name','?')}, score={m.get('score','?')}) ----",
                "set /a DONE+=1",
                f'echo [!DONE!/%TOTAL%] {cid}  ({m.get("mod_name","?")})',
                "jamme.exe ^",
                "  +set fs_homepath . ^",
                "  +set cl_renderer rd-jamme ^",
                "  +set fs_game mme ^",
                "  +set r_fullscreen 0 ^",
                "  +set r_noborder 0 ^",
                "  +set r_centerWindow 0 ^",
                "  +set r_mode -1 ^",
                "  +set r_customwidth 1280 ^",
                "  +set r_customheight 720 ^",
                "  +set mme_demoListQuit 1 ^",
                "  +set mme_demoAutoQuit 1 ^",
                "  +set mme_demoEscapeQuit 1 ^",
                f"  +set mov_captureName {cid} ^",
                f"  +demo {cid}",
                f'if exist clip.mp4 (move /Y clip.mp4 "mme\\captures\\{cid}.mp4" >nul) else echo   [warn] {cid}: clip.mp4 not produced',
                "",
            ]
        lines += [
            "echo.",
            "echo done. captures in mme\\captures\\",
            "echo started %START_TIME% finished %TIME%",
            "endlocal",
        ]
        bat_path.write_text("\r\n".join(lines), encoding="utf-8")
        print(f"  wrote {bat_path}")

    # --- 6. friendly summary --------------------------------------------------
    weapons = pd.DataFrame(manifest)["mod_name"].value_counts() \
        if manifest else pd.Series(dtype=int)
    print("\nweapon breakdown:")
    for w, n in weapons.items():
        print(f"    {w:<28}  {n}")

    print(f"\nstaged {len(manifest)} clip(s).")
    if args.dry_run:
        print("(dry run — nothing was written.)")
    else:
        print("\nnext step:")
        print(f"  cd \"{GAMEDATA}\"")
        print(f"  .\\render_clips_batch.bat")


if __name__ == "__main__":
    main()
