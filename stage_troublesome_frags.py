import joblib, json, sys, os, subprocess
import pandas as pd
from pathlib import Path
import warnings
warnings.filterwarnings('ignore')

# 1. Load models and calculate errors
cache_path = Path("python/predict/models/ensemble_cache.joblib")
if not cache_path.exists():
    print("Cache not found.")
    sys.exit(1)

cache = joblib.load(cache_path)
models = cache['models']

def load_df(path):
    with open(path, 'r', encoding='utf-8') as f:
        return pd.json_normalize(json.load(f))

try:
    good = load_df("python/predict/good_client_frags_aggregated.json")
    good['human_label'] = 1
    bad = load_df("python/predict/bad_client_frags_aggregated.json")
    bad['human_label'] = 0
except Exception as e:
    print(f"Error loading data: {e}")
    sys.exit(1)

df = pd.concat([good, bad], ignore_index=True)

FEATURES = [
    "view_delta_deg", "view_speed_deg_per_ms",
    "view_best_delta_deg", "view_best_speed_deg_per_sec",
    "attacker_target_distance", "attacker_xy_speed", "attacker_z_speed",
    "attacker_distance_last_second",
    "target_xy_speed", "target_z_speed", "target_distance_last_second",
    "missile_lifetime", "missile_pitch",
    "target_corpse_travel_distance", "target_corpse_travel_z_distance",
]

results = []
for mod, stack in models.items():
    sub = df[df["mod_name"] == mod]
    if sub.empty: continue
    X = sub[FEATURES].fillna(0).values
    proba = stack.predict_proba(X)[:, 1]
    sub = sub.copy()
    sub['model_score'] = proba
    sub['error'] = abs(sub['human_label'] - proba)
    sub['human_label_str'] = sub['human_label'].map({1: 'GOOD', 0: 'BAD'})
    results.append(sub)

if not results:
    print("No results.")
    sys.exit(0)

res_df = pd.concat(results, ignore_index=True)
res_df = res_df.sort_values(by='error', ascending=False)

# Get top 20 most troublesome frags
top_troublesome = res_df.head(20).copy()

# 2. Export them as "good" so build_jamme_demolist.py stages them
# build_jamme_demolist expects 'consensus' == 'good'
top_troublesome['consensus'] = 'good'

out_csv = Path("python/predict/troublesome_frags.ensemble_predictions.csv")
out_cols = [
    '_source_file', 'time_raw', 'human_time',
    'map_start_time_raw', 'map_end_time_raw',
    'mod_name', 'consensus', 'score', 'threshold_used', 'extreme_99th',
    'attacker_name', 'target_name', 'human_label_str', 'error'
]
# Only keep columns that exist in the dataframe
out_cols = [c for c in out_cols if c in top_troublesome.columns]
top_troublesome[out_cols].to_csv(out_csv, index=False)

print(f"Exported top {len(top_troublesome)} troublesome frags to {out_csv.name}")

# 3. Stage the clips
print("\nStaging clips...")
stage_cmd = [sys.executable, "python/predict/build_jamme_demolist.py", "--csv", str(out_csv)]
subprocess.run(stage_cmd, check=True)

# 4. Render the clips
print("\nRendering clips (sequentially)...")
render_cmd = [sys.executable, "python/predict/render_clips.py", "--reset"]
subprocess.run(render_cmd, check=True)

print("\n===================================================================")
print("Done! The troublesome clips have been rendered and staged.")
print("To review them, open a new terminal and run:")
print("    cd C:\\jactf_pipeline\\python\\review")
print("    .\\run.bat")
print("Then open your browser to: http://127.0.0.1:5057/")
print("===================================================================")
