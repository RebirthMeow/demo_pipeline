#!/usr/bin/env python3
"""
check_duplicate_frags.py – v3 · 2025‑07‑02

Compare a *new* frag JSON (default **xen_frags.json**) against every other
*.json* in the same folder and report duplicates. A duplicate is defined by an
*identity signature* composed of core fields plus the optional metrics you
listed.

**What’s new in v3**
-------------------
* **Per‑frag error handling** – malformed frags in an *other* file are now
  skipped individually instead of causing the whole file to be ignored.
* Added concise counters for skipped/malformed frags per file so you can gauge
  data quality.
* Minor code tidy‑ups and clearer console output.

Usage
-----
```
python check_duplicate_frags.py xen_frags.json [--strict] [--write-unique]
```

* `--strict`      – require every optional metric (legacy behaviour)
* `--write-unique` – emit `<new>_unique.json` with non‑duplicate frags

Dependencies: only `argparse`, `json`, `pathlib`, `collections`, `textwrap`,
`itertools` from the standard library.
"""
from __future__ import annotations
import json, argparse, sys, textwrap, itertools
from pathlib import Path
from collections import Counter, defaultdict

# ── Signature fields ──────────────────────────────────────────────────────────
REQUIRED_FIELDS = [
    "time_raw", "attacker_name", "attacker_team",
    "target_name", "target_team", "mod_name",
]
OPTIONAL_FIELDS = [
    "view_delta_deg", "view_speed_deg_per_ms", "view_best_delta_deg",
    "view_best_speed_deg_per_sec", "attacker_target_distance",
    "attacker_xy_speed", "attacker_z_speed", "attacker_distance_last_second",
    "target_xy_speed", "target_z_speed", "target_distance_last_second",
    "target_corpse_travel_distance", "target_corpse_travel_z_distance",
]
ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

# ── Helpers ───────────────────────────────────────────────────────────────────

def frag_signature(frag: dict, *, strict: bool = False) -> tuple:
    """Return an immutable signature tuple for a frag.

    If *strict* is False optional fields that are missing are substituted with
    ``None``. If *strict* is True a KeyError is raised for any missing field.
    """
    sig = []
    for field in REQUIRED_FIELDS:
        if field not in frag:
            raise KeyError(f"missing required field '{field}'")
        sig.append(frag[field])
    for field in OPTIONAL_FIELDS:
        if field in frag:
            sig.append(frag[field])
        elif strict:
            raise KeyError(f"missing optional field '{field}' (strict)")
        else:
            sig.append(None)
    return tuple(sig)


def load_frags(path: Path) -> list[dict]:
    """Return a list of frag dicts irrespective of file structure."""
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "frags" in data:
            return data["frags"]
        if "maps" in data and isinstance(data["maps"], list) and data["maps"]:
            return data["maps"][0].get("frags", [])
    raise ValueError(f"Unrecognised structure in {path.name}")


# ── Main logic ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""Compare one frag JSON against all others in the folder and report duplicates.""",
    )
    ap.add_argument("new_file", help="The JSON file to de‑duplicate (e.g. xen_frags.json)")
    ap.add_argument("--strict", action="store_true", help="Require all optional fields to exist (legacy behaviour)")
    ap.add_argument("--write-unique", action="store_true", help="Write <new>_unique.json with duplicates removed")
    args = ap.parse_args()

    new_path = Path(args.new_file).resolve()
    if not new_path.exists():
        sys.exit(f"File not found: {new_path}")

    folder = new_path.parent
    other_files = [p for p in folder.glob("*.json") if p != new_path]

    print(f"→ Comparing {new_path.name} against {len(other_files)} other JSON files…")

    other_signatures: set[tuple] = set()
    file_origin: defaultdict[tuple, list[str]] = defaultdict(list)
    malformed_per_file: Counter[str] = Counter()

    # Build signature sets for other files
    for path in other_files:
        try:
            for frag in load_frags(path):
                try:
                    sig = frag_signature(frag, strict=args.strict)
                except KeyError as ke:
                    malformed_per_file[path.name] += 1
                    continue  # skip malformed frag only
                other_signatures.add(sig)
                file_origin[sig].append(path.name)
        except Exception as e:
            # gross structural issue – skip whole file
            print(f"  ! Skipped entire {path.name}: {e}")

    # Now process the new file
    new_frags = load_frags(new_path)
    duplicate_count = 0
    per_file_counter: Counter[str] = Counter()
    unique_frags: list[dict] = []

    for frag in new_frags:
        try:
            sig = frag_signature(frag, strict=args.strict)
        except KeyError as e:
            print(f"  ! Skipped malformed frag in {new_path.name}: {e}")
            continue
        if sig in other_signatures:
            duplicate_count += 1
            for src in file_origin[sig]:
                per_file_counter[src] += 1
        else:
            unique_frags.append(frag)

    # ── Results ──────────────────────────────────────────────────────────────
    total = len(new_frags)
    print("\nSummary")
    print("=" * 60)
    print(f"Total frags in {new_path.name}: {total}")
    print(f"Duplicates found        : {duplicate_count}")
    print(f"Unique frags remaining  : {total - duplicate_count}")

    if malformed_per_file:
        print("\nMalformed frags skipped from other files:")
        for fname in sorted(malformed_per_file):
            print(f"  {fname:<40} {malformed_per_file[fname]:>5}")

    if duplicate_count:
        print("\nBreakdown by original file:")
        for fname, cnt in per_file_counter.most_common():
            print(f"  {fname:<40} {cnt:>5}")

    # ── Optional writing ------------------------------------------------------
    if args.write_unique and unique_frags:
        out_path = new_path.with_stem(new_path.stem + "_unique")
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(unique_frags, f, indent=2)
        print(f"\nWrote {out_path.name} containing {len(unique_frags)} non‑duplicate frags.")


if __name__ == "__main__":
    main()
