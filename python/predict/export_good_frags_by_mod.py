#!/usr/bin/env python3
"""
export_good_frags_by_mod.py

Exports good frags from good_client_frags_aggregated.json for specified MOD_* types,
outputting in the same CSV format as predict_frags_ensemble.py, but no prediction.
Usage:
    python export_good_frags_by_mod.py --mod MOD_ROCKET --mod MOD_BRYAR_PISTOL_ALT
"""

import json
import argparse
import pandas as pd
from pathlib import Path

def load_df(path: Path) -> pd.DataFrame:
    with path.open(encoding="utf-8") as f:
        return pd.json_normalize(json.load(f))

def main():
    parser = argparse.ArgumentParser(description="Export good frags by MOD_* type")
    parser.add_argument('--mod', action='append', required=True,
                        help="MOD_* weapon types to export, e.g. --mod MOD_ROCKET")
    parser.add_argument('--out', default=None, help="Output CSV path (defaults to good_frags_selected.csv)")
    args = parser.parse_args()

    # Hardcoded path (same folder as script)
    good_json_path = Path(__file__).parent / "good_client_frags_aggregated.json"
    good = load_df(good_json_path)
    filtered = good[good["mod_name"].isin(args.mod)].copy()
    if filtered.empty:
        print("No frags found for selected MOD_* types.")
        return

    # Always mark as 'good' and set extreme_99th to False
    filtered["consensus"] = "good"
    filtered["extreme_99th"] = False

    # Prob columns (as NaN, no model inference here)
    prob_cols = [f'prob_{t}' for t in ('rf','et','lr','gb','xgb','lsvc','csvc','knn','mlp','gnb')]
    for col in prob_cols:
        filtered[col] = float('nan')

    # List features for output (mirror original)
    FEATURES = [
        "view_delta_deg", "view_speed_deg_per_ms",
        "view_best_delta_deg", "view_best_speed_deg_per_sec",
        "attacker_target_distance", "attacker_xy_speed", "attacker_z_speed",
        "attacker_distance_last_second",
        "target_xy_speed", "target_z_speed", "target_distance_last_second",
        "missile_lifetime", "missile_pitch",
        "target_corpse_travel_distance", "target_corpse_travel_z_distance",
    ]
    out_cols = [
        '_source_file','time_raw','human_time','map_start_time_raw','map_end_time_raw',
        'mod_name','consensus','extreme_99th', 'label_ts',
    ] + prob_cols + FEATURES + ['attacker_name','target_name']

    # Only include columns that exist in the data
    final_cols = [c for c in out_cols if c in filtered.columns]

    out_csv = args.out or "good_frags_selected.csv"
    filtered[final_cols].to_csv(out_csv, index=False)
    print(f"✓ Exported {len(filtered)} frags to {out_csv}")

if __name__ == "__main__":
    main()
