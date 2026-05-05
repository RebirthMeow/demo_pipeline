
#!/usr/bin/env python3
"""
trim_good_frags.py  –  **human‑time trimming** (robust parser)

Patch v3 (2025‑06‑28)
─────────────────────
Fixes: rows like `0:07:06` (no milliseconds) or `1:00:08` were rejected.

* `human_to_ms()` now accepts any of these forms:
      • HH:MM:SS.mmm   (full precision)
      • HH:MM:SS       (no milliseconds)
      •  MM:SS.mmm     (no hours)
      •  MM:SS         (no hours, no milliseconds)
* `safe_mmss_ms()` likewise tolerates missing `.mmm`.
Everything else unchanged.
"""

import os, sys, subprocess, argparse, re, json
from pathlib import Path
import pandas as pd

# ── Configuration ───────────────────────────────────────────────────────
DEMO_ROOT    = Path.cwd() / "demos source"
OUT_ROOT     = Path.cwd() / "demos output"
TRIMMER_EXE  = Path.cwd() / "DemoTrimmer.exe"
PADDING_MS   = 6_000
CORE_WEAPONS = {"MOD_ROCKET", "MOD_REPEATER_ALT", "MOD_BRYAR_PISTOL_ALT"}
SET_WEAP = {"MOD_ROCKET", "MOD_REPEATER_ALT", "MOD_BRYAR_PISTOL_ALT", "MOD_DISRUPTOR_SNIPER", "MOD_FLECHETTE_ALT_SPLASH", "MOD_THERMAL", "MOD_DISRUPTOR", "MOD_BLASTER", "MOD_REPEATER",} 

# ── Helpers ────────────────────────────────────────────────────────────

def find_demo_file(root: Path, name: str) -> Path | None:
    for d, _, files in os.walk(root):
        if name in files:
            return Path(d) / name
    return None


def extract_demo_name(src_field: str) -> str | None:
    suf = ".dm_26.dm_meta"
    i   = src_field.find(suf)
    if i != -1:
        return src_field[: i + len(".dm_26")]
    base = os.path.basename(src_field)
    return base[:-len(".dm_meta")] if base.endswith(suf) else None


def ms_to_hhmmss(ms: int) -> str:
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms,   60_000)
    s, ms = divmod(ms,    1_000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

# --- robust human‑time → ms ------------------------------------------------

def human_to_ms(ts: str) -> int | None:
    """Convert HH:MM:SS(.mmm) or MM:SS(.mmm) to milliseconds."""
    ts = ts.strip()
    if not ts:
        return None
    parts = ts.split(":")
    if len(parts) == 3:       # HH:MM:SS[.mmm]
        hh, mm, rest = parts
    elif len(parts) == 2:     # MM:SS[.mmm]
        hh = "0"
        mm, rest = parts
    else:
        return None
    if "." in rest:
        ss, ms = rest.split(".", 1)
        ms = ms.ljust(3, "0")[:3]
    else:
        ss, ms = rest, "000"
    try:
        total_ms = (int(hh)*3600 + int(mm)*60 + int(ss)) * 1000 + int(ms)
        return total_ms
    except ValueError:
        return None


def safe_mmss_ms(ts: str) -> str:
    """Return MM-SS.mmm string for filename (hours dropped)."""
    ts = ts.strip()
    if not ts:
        return "00-00.000"
    # ensure we have minutes and seconds
    if ":" not in ts:
        return "00-00.000"
    if ts.count(":") == 2:   # HH:MM:SS(.mmm) → drop hours
        ts = ts.split(":", 1)[1]
    if "." not in ts:
        ts += ".000"
    mm, rest = ts.split(":", 1)
    ss, ms   = rest.split(".")
    return f"{mm.zfill(2)}-{ss}.{ms.ljust(3,'0')[:3]}"


def short_stem(src_demo: str, limit: int = 15) -> str:
    return Path(src_demo).stem.replace(".dm_26", "")[:limit]


def trim_demo(src: Path, dst: Path, start_ms: int, end_ms: int) -> None:
    subprocess.check_call([
        str(TRIMMER_EXE), str(src), str(dst),
        ms_to_hhmmss(start_ms), ms_to_hhmmss(end_ms)
    ])
    print(f"Trimmed {src.name} → {dst.name}")

# ── Main ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Trim frags into 12‑second mini‑demos.")
    ap.add_argument("csv", help="frags CSV to process")
    ap.add_argument("--core-only", action="store_true",
                    help="trim only ROCKET / REPEATER_ALT / BRYAR_PISTOL_ALT")
    ap.add_argument("-a", "--all-frags", action="store_true",
                    help="trim every row (ignore consensus filter)")
    ap.add_argument("--set", action="store_true",
                    help="trim only rkt/rep/pistol/sniper/golan/e11")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.is_file():
        sys.exit(f"CSV not found: {csv_path}")

    df = pd.read_csv(csv_path)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    trimmed_map = {}

    for idx, row in df.iterrows():
        if not args.all_frags and str(row.get("consensus", "")).lower() != "good":
            continue
        if args.core_only and row.get("mod_name") not in CORE_WEAPONS:
            continue

        demo_name = extract_demo_name(str(row.get("_source_file", "")))
        if not demo_name:
            print(f"[Warn] Row {idx}: can't parse demo name"); continue

        demo_in = find_demo_file(DEMO_ROOT, demo_name)
        if not demo_in:
            print(f"[Warn] Row {idx}: demo not found"); continue

        center_ms = human_to_ms(str(row.get("human_time")))
        if center_ms is None:
            print(f"[Warn] Row {idx}: bad human_time '{row.get('human_time')}'"); continue

        start_ms = max(0, center_ms - PADDING_MS)
        end_ms   = center_ms + PADDING_MS
        if end_ms - start_ms < 1_000:
            print(f"[Warn] Row {idx}: window <1 s after clamp – skipped"); continue

        mmssms = safe_mmss_ms(str(row["human_time"]))
        stem   = short_stem(demo_name)
        out_dir = OUT_ROOT / os.path.relpath(demo_in.parent, DEMO_ROOT)
        out_dir.mkdir(parents=True, exist_ok=True)
        demo_out = out_dir / f"trm_{stem}_{mmssms}_{idx}.dm_26"

        try:
            trim_demo(demo_in, demo_out, start_ms, end_ms)
            trimmed_map[demo_out.name] = {
                "full_demo": demo_in.name,
                "time_raw":  int(row.get("time_raw", center_ms))
            }
        except subprocess.CalledProcessError as e:
            print(f"[Error] Row {idx}: trimming failed – {e}")

    if trimmed_map:
        map_out = OUT_ROOT / "trimmed_map.json"
        map_out.write_text(json.dumps(trimmed_map, indent=2), encoding="utf-8")
        print(f"\nWrote mapping → {map_out}")
    else:
        print("No clips trimmed – nothing to write.")

if __name__ == "__main__":
    main()
