import joblib, json, sys
import pandas as pd
from pathlib import Path
import warnings
warnings.filterwarnings('ignore')

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
    # predict_proba returns probabilities for classes [0, 1]
    proba = stack.predict_proba(X)[:, 1]
    sub = sub.copy()
    sub['model_score'] = proba
    sub['error'] = abs(sub['human_label'] - proba)
    
    # Map label numbers to readable text
    sub['human_label_str'] = sub['human_label'].map({1: 'GOOD', 0: 'BAD'})
    
    results.append(sub)

if not results:
    print("No results.")
    sys.exit(0)

res_df = pd.concat(results, ignore_index=True)
res_df = res_df.sort_values(by='error', ascending=False)

print("===================================================================")
print("Top 15 Most 'Troublesome' Frags (Highest Model vs Human Disagreement)")
print("===================================================================")
cols = ['_source_file', 'human_time', 'mod_name', 'human_label_str', 'model_score', 'error']
# Formatting for better readability
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 1000)
pd.set_option('display.float_format', lambda x: '%.3f' % x)

print(res_df[cols].head(15).to_string(index=False))

print("\n===================================================================")
print("Average Error by Weapon (Which weapons confuse the model most?)")
print("===================================================================")
weapon_error = res_df.groupby('mod_name')['error'].agg(['mean', 'count']).sort_values(by='mean', ascending=False)
weapon_error.columns = ['Avg Error', 'Frag Count']
print(weapon_error.to_string())
