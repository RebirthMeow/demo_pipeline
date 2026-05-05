#!/usr/bin/env python3
"""Aggregate frags from .dm_meta files.  v13-aware, POV-filtered."""
import argparse, bisect, json, glob, os, pathlib, sys, time

# Default scans the xen demo set (legacy default for back-compat).  Override
# with --input on each run, or set JACTF_ROOT and pass --input ${JACTF_ROOT}/...
# This is invoked per-set from regen_pipeline.ps1, so the default is rarely used.
_REPO_ROOT = pathlib.Path(os.environ.get("JACTF_ROOT") or pathlib.Path(__file__).resolve().parent)
DEFAULT_INPUT = str(_REPO_ROOT / "python" / "trimming" / "demos source" / "xen" / "**" / "*.dm_meta")
DEFAULT_OUT   = "3xen_frags.json"
KILL_WINDOW_MS = 5000


def _load_meta(path):
    with open(path, "rb") as f:
        text = f.read()
    nul = text.find(b"\x00")
    if nul >= 0:
        text = text[:nul]
    return json.loads(text)


def aggregate(input_glob, out_path, frag_field="ownfrags"):
    """frag_field: 'ownfrags' (POV-filtered, default) or 'frags' (everything visible)."""
    print(f"  expanding glob: {input_glob}", flush=True)
    files = list(glob.glob(input_glob, recursive=True))
    print(f"  matched {len(files)} files (using maps[].{frag_field})", flush=True)

    aggregated = []
    n_demos = 0
    n_frags_so_far = 0
    last_print = time.time()

    for filepath in files:
        n_demos += 1
        if n_demos % 100 == 0 or (time.time() - last_print) > 5:
            print(f"  [{n_demos}/{len(files)}] frags={n_frags_so_far} current={os.path.basename(filepath)[:60]}", flush=True)
            last_print = time.time()
        try:
            data = _load_meta(filepath)
        except Exception as exc:
            print(f"  skip (parse fail): {filepath} ({exc})", file=sys.stderr, flush=True)
            continue
        if not data.get("maps"):
            continue
        for this_map in data["maps"]:
            # POV-filtered list: only kills made by the demo recorder.  Dedups
            # across the 8 POVs of a 4v4 match since each kill counts from the
            # one demo whose recorder == attacker.  (The .exe excludes suicides
            # and self-kills from ownfrags; we still drop teamkills below.)
            frags = this_map.get(frag_field, [])
            client_frags = [
                frag for frag in frags
                if frag.get("mod_name") != "MOD_SUICIDE"
                and frag.get("attacker_team") != frag.get("target_team")
            ]
            if not client_frags:
                continue
            map_start = this_map.get("map_start_time_raw")
            map_end   = this_map.get("map_end_time_raw")
            by_attacker = {}
            for f in client_frags:
                atk = f.get("attacker"); t = f.get("time_raw")
                if atk is None or t is None: continue
                by_attacker.setdefault(atk, []).append(t)
            for atk in by_attacker:
                by_attacker[atk].sort()
            for frag in client_frags:
                rec = frag.copy()
                rec["_source_file"]       = os.path.basename(filepath)
                rec["map_start_time_raw"] = map_start
                rec["map_end_time_raw"]   = map_end
                atk = frag.get("attacker"); t = frag.get("time_raw")
                if atk is not None and t is not None and atk in by_attacker:
                    times = by_attacker[atk]
                    lo = bisect.bisect_left(times, t - KILL_WINDOW_MS)
                    hi = bisect.bisect_right(times, t + KILL_WINDOW_MS)
                    window_count = hi - lo
                else:
                    window_count = 1
                rec["kills_in_window"] = window_count
                rec["multi_kills"]     = window_count > 1
                aggregated.append(rec)
                n_frags_so_far += 1

    if aggregated:
        pathlib.Path(out_path).write_text(json.dumps(aggregated, indent=2), encoding="utf-8")
        print(f"Wrote {len(aggregated)} frags from {n_demos} demos -> {out_path}", flush=True)
        return 0
    else:
        print(f"No qualifying frags found (scanned {n_demos} demos).", flush=True)
        return 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=DEFAULT_INPUT)
    ap.add_argument("--out",   default=DEFAULT_OUT)
    ap.add_argument("--include-witnesses", action="store_true",
                    help="use maps[].frags (everything visible from POV) instead of "
                         "maps[].ownfrags (only the recorder's kills).  Default off.")
    args = ap.parse_args(argv)
    field = "frags" if args.include_witnesses else "ownfrags"
    return aggregate(args.input, args.out, frag_field=field)


if __name__ == "__main__":
    sys.exit(main())
