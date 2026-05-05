#!/usr/bin/env python3
"""
dedupe_good_against_bad.py

Removes any frag from good_client_frags_aggregated.json that is also in bad_client_frags_aggregated.json,
using (_source_file, time_raw, mod_name) as the unique key.
Backs up good_client_frags_aggregated.json into a backup subfolder with a timestamp.

Usage:
    python dedupe_good_against_bad.py
"""

import json
from pathlib import Path
import pandas as pd
import shutil
from datetime import datetime

def load_json_df(path):
    with open(path, encoding="utf-8") as f:
        return pd.json_normalize(json.load(f))

def main():
    root = Path(__file__).parent
    good_path = root / "good_client_frags_aggregated.json"
    bad_path  = root / "bad_client_frags_aggregated.json"

    # ---- Backup good file ----
    backup_dir = root / "backup"
    backup_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"good_client_frags_aggregated_{timestamp}.json"
    shutil.copy2(good_path, backup_path)
    print(f"Backup → {backup_path}")

    good_df = load_json_df(good_path)
    bad_df  = load_json_df(bad_path)

    # Unique key: (_source_file, time_raw, mod_name)
    def make_key(df):
        return (
            df["_source_file"].astype(str) + "|" +
            df["time_raw"].astype(str) + "|" +
            df["mod_name"].astype(str)
        )

    bad_keys  = set(make_key(bad_df))
    good_keys = make_key(good_df)

    # Keep only good frags not in bad
    keep_mask = ~good_keys.isin(bad_keys)
    deduped_good_df = good_df[keep_mask].copy()

    num_removed = len(good_df) - len(deduped_good_df)
    print(f"✓ Filtered good frags: {len(deduped_good_df)} remain after removing {num_removed} duplicates.")

    # Overwrite the original good file with deduped results
    deduped_json = deduped_good_df.to_dict(orient="records")
    with open(good_path, "w", encoding="utf-8") as f:
        json.dump(deduped_json, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
