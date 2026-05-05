#!/usr/bin/env python3
"""
predict_frags_ensemble.py - v14 (2026-04-27) + Caching Optimization

Major changes from v11:
1) OPTION B labels: 'ok' frags are now DROPPED from training.
2) Per-weapon classifier councils.
3) Stacking ensemble replaces hard-vote consensus.
4) Per-weapon thresholds.
5) Caching: Models are saved to disk and reused unless training data changes.
"""

import warnings
warnings.filterwarnings('ignore')

import argparse, json, sys, hashlib, joblib
from pathlib import Path
import numpy as np
import pandas as pd

from sklearn.ensemble import (
    RandomForestClassifier, ExtraTreesClassifier, GradientBoostingClassifier,
    HistGradientBoostingClassifier, AdaBoostClassifier, StackingClassifier,
)
from sklearn.linear_model import LogisticRegression, RidgeClassifier
from sklearn.naive_bayes import GaussianNB
from sklearn.neighbors import KNeighborsClassifier
from sklearn.neural_network import MLPClassifier
from sklearn.svm import LinearSVC
from sklearn.calibration import CalibratedClassifierCV
from xgboost import XGBClassifier
import lightgbm as lgb

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

FEATURES = [
    "view_delta_deg", "view_speed_deg_per_ms",
    "view_best_delta_deg", "view_best_speed_deg_per_sec",
    "attacker_target_distance", "attacker_xy_speed", "attacker_z_speed",
    "attacker_distance_last_second",
    "target_xy_speed", "target_z_speed", "target_distance_last_second",
    "missile_lifetime", "missile_pitch",
    "target_corpse_travel_distance", "target_corpse_travel_z_distance",
]

PER_WEAPON_COUNCIL = {
    'MOD_ROCKET':               ['xgb', 'lgb', 'histgb', 'gb'],
    'MOD_BRYAR_PISTOL_ALT':     ['et',  'ada', 'rf',     'xgb'],
    'MOD_REPEATER_ALT':         ['ridge','et', 'lr',     'gb'],
    'MOD_BLASTER':              ['et',  'rf',  'xgb',    'csvc'],
    'MOD_BOWCASTER':            ['lr',  'et',  'gb',     'gnb'],
    'MOD_DISRUPTOR':            ['xgb', 'gb',  'rf',     'lr'],
    'MOD_DISRUPTOR_SNIPER':     ['xgb', 'gb',  'rf',     'gnb'],
    'MOD_FLECHETTE_ALT_SPLASH': ['xgb', 'gb',  'rf',     'gnb'],
    'MOD_THERMAL':              ['xgb', 'gb',  'rf',     'gnb'],
    'MOD_REPEATER':             ['lr',  'gb',  'rf',     'gnb'],
}
COUNCIL_DEFAULT = ['xgb', 'rf', 'gb', 'lr']

THRESHOLD_MODES = {
    'default': {
        'MOD_ROCKET':               0.50,
        'MOD_BRYAR_PISTOL_ALT':     0.50,
        'MOD_REPEATER_ALT':         0.50,
    },
    'high-precision': {
        'MOD_ROCKET':               0.75,
        'MOD_BRYAR_PISTOL_ALT':     0.75,
        'MOD_REPEATER_ALT':         0.55,
    },
}
DEFAULT_THRESHOLD = 0.50

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def load_df(path):
    """Load a JSON, tolerating trailing NUL padding from interrupted writes."""
    with open(path, 'rb') as f:
        text = f.read()
    nul = text.find(b'\x00')
    if nul >= 0:
        text = text[:nul]
    s = text.decode('utf-8', errors='replace')
    try:
        return pd.json_normalize(json.loads(s))
    except json.JSONDecodeError:
        depth = 0; in_string = False; escape = False
        last_complete = -1; array_started = False
        for i, c in enumerate(s):
            if escape: escape = False; continue
            if in_string:
                if c == '\\': escape = True
                elif c == '"': in_string = False
                continue
            if c == '"': in_string = True; continue
            if c == '[' and not array_started: array_started = True; continue
            if c == '{': depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0: last_complete = i
        if last_complete < 0: return pd.DataFrame()
        try:
            return pd.json_normalize(json.loads(s[:last_complete + 1] + ']'))
        except Exception:
            return pd.DataFrame()


def make_clf(name, seed=42):
    if name == 'rf':     return RandomForestClassifier(n_estimators=200, random_state=seed, n_jobs=-1)
    if name == 'et':     return ExtraTreesClassifier(n_estimators=200, random_state=seed, n_jobs=-1)
    if name == 'gb':     return GradientBoostingClassifier(random_state=seed)
    if name == 'xgb':    return XGBClassifier(n_estimators=200, max_depth=4, eval_metric='logloss', verbosity=0, random_state=seed, n_jobs=-1)
    if name == 'lgb':    return lgb.LGBMClassifier(n_estimators=200, num_leaves=31, random_state=seed, n_jobs=-1, verbose=-1)
    if name == 'histgb': return HistGradientBoostingClassifier(max_iter=200, max_depth=4, random_state=seed)
    if name == 'ada':    return AdaBoostClassifier(n_estimators=200, random_state=seed)
    if name == 'lr':     return LogisticRegression(max_iter=5000, random_state=seed)
    if name == 'ridge':  return RidgeClassifier(random_state=seed)
    if name == 'lsvc':   return LinearSVC(max_iter=10000, dual=False, random_state=seed)
    if name == 'csvc':   return CalibratedClassifierCV(LinearSVC(max_iter=10000, dual=False, random_state=seed), cv=3)
    if name == 'knn':    return KNeighborsClassifier(n_neighbors=5)
    if name == 'mlp':    return MLPClassifier(hidden_layer_sizes=(50,), max_iter=1000, random_state=seed)
    if name == 'gnb':    return GaussianNB()
    raise ValueError(f"unknown classifier name: {name}")


def build_stack(council_names, seed=42):
    base = [(name, make_clf(name, seed=seed)) for name in council_names]
    return StackingClassifier(
        estimators=base,
        final_estimator=LogisticRegression(max_iter=5000, random_state=seed),
        cv=3,
        n_jobs=1,
        passthrough=False,
    )

def get_files_hash(paths):
    h = hashlib.sha256()
    for p in sorted(paths):
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()

# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

ap = argparse.ArgumentParser()
ap.add_argument("input_json", help=".dm_meta-aggregated frag JSON to score")
ap.add_argument("--threshold-mode", choices=list(THRESHOLD_MODES.keys()), default='default',
                help="default = balanced recall/precision; high-precision = stricter for 'clearly good only'")
ap.add_argument("--force-retrain", action="store_true", help="bypass cache and retrain models")
args = ap.parse_args()

# ---------------------------------------------------------------------
# Load labeled training sets
# ---------------------------------------------------------------------
root = Path(__file__).parent
train_files = [
    root / "good_client_frags_aggregated.json",
    root / "bad_client_frags_aggregated.json"
]
cache_dir = root / "models"
cache_dir.mkdir(exist_ok=True)
cache_file = cache_dir / "ensemble_cache.joblib"
current_hash = get_files_hash(train_files)

weapon_models = {}
weapon_features_99 = {}
thr_99_global = None

# Attempt to load from cache
if not args.force_retrain and cache_file.exists():
    cache_data = joblib.load(cache_file)
    if cache_data.get("hash") == current_hash:
        print(f">> loading models from cache ({current_hash[:8]})", file=sys.stderr)
        weapon_models = cache_data["models"]
        weapon_features_99 = cache_data["features_99"]
        thr_99_global = cache_data["thr_99_global"]
    else:
        print(f">> training data changed, retraining...", file=sys.stderr)
else:
    print(f">> cache miss or --force-retrain, retraining...", file=sys.stderr)

if not weapon_models:
    print(f">> loading training labels", file=sys.stderr)
    good = load_df(root / "good_client_frags_aggregated.json")
    ok   = load_df(root / "ok_client_frags_aggregated.json")
    bad  = load_df(root / "bad_client_frags_aggregated.json")
    
    good["label"] = 1
    bad["label"]  = 0
    train_df = pd.concat([good, bad], ignore_index=True)
    print(f">> training on {len(train_df)} frags  good={int(train_df.label.sum())}  bad={int((train_df.label==0).sum())}", file=sys.stderr)

    print(f">> fitting per-weapon stacks", file=sys.stderr)
    for mod, council in PER_WEAPON_COUNCIL.items():
        sub = train_df[train_df["mod_name"] == mod]
        pos = int(sub.label.sum()); neg = len(sub) - pos
        if len(sub) < 20 or pos < 5 or neg < 5:
            continue
        X = sub[FEATURES].fillna(0).values
        y = sub["label"].values
        try:
            stack = build_stack(council)
            stack.fit(X, y)
            weapon_models[mod] = stack
            gp = sub[sub.label == 1][FEATURES].fillna(0)
            if not gp.empty:
                weapon_features_99[mod] = gp.quantile(0.99)
            print(f"    fit {mod:28s}  n={len(sub)}  good={pos}", file=sys.stderr)
        except Exception as exc:
            print(f"    FAILED {mod}: {exc}", file=sys.stderr)

    thr_99_global = good[FEATURES].quantile(0.99) if not good.empty else None
    
    # Save to cache
    joblib.dump({
        "hash": current_hash,
        "models": weapon_models,
        "features_99": weapon_features_99,
        "thr_99_global": thr_99_global
    }, cache_file)
    print(f">> models saved to cache", file=sys.stderr)

# ---------------------------------------------------------------------
# Load inference data (always needed to score)
# ---------------------------------------------------------------------
good = load_df(root / "good_client_frags_aggregated.json")
ok   = load_df(root / "ok_client_frags_aggregated.json")
bad  = load_df(root / "bad_client_frags_aggregated.json")
ignore = load_df(root / "ignore_frags_aggregated.json") if (root / "ignore_frags_aggregated.json").exists() else pd.DataFrame()

seen = set()
for src_df in (good, ok, bad, ignore):
    if not src_df.empty and "_source_file" in src_df.columns and "human_time" in src_df.columns:
        seen |= {f"{f}|{t}" for f, t in zip(src_df["_source_file"], src_df["human_time"])}

drop_demos = []
if not ignore.empty and "_source_file" in ignore.columns:
    drop_demos = ignore["_source_file"].value_counts()[lambda s: s >= 2].index

# ---------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------
print(f">> scoring {args.input_json}", file=sys.stderr)
un_df = load_df(Path(args.input_json))
if un_df.empty:
    print("ERROR: input file empty or unparseable", file=sys.stderr); sys.exit(1)

if len(drop_demos) > 0:
    un_df = un_df[~un_df["_source_file"].isin(drop_demos)].reset_index(drop=True)

un_df = un_df[(un_df.get("attacker_team", "") != un_df.get("target_team", "")) &
              (un_df.get("mod_name", "") != "MOD_SUICIDE")].reset_index(drop=True)

un_df["__key"] = un_df["_source_file"].astype(str) + "|" + un_df["human_time"].astype(str)
un_df = un_df[~un_df["__key"].isin(seen)].drop_duplicates(subset="__key").reset_index(drop=True)

if un_df.empty:
    print("OK: all frags already labeled or filtered -- nothing to score.", file=sys.stderr)
    sys.exit(0)

thresholds = THRESHOLD_MODES[args.threshold_mode]
un_df["score"] = np.nan
un_df["consensus"] = "bad"
un_df["threshold_used"] = DEFAULT_THRESHOLD
for mod, stack in weapon_models.items():
    mask = un_df["mod_name"] == mod
    if not mask.any():
        continue
    Xm = un_df.loc[mask, FEATURES].fillna(0).values
    proba = stack.predict_proba(Xm)[:, 1]
    thr = thresholds.get(mod, DEFAULT_THRESHOLD)
    un_df.loc[mask, "score"] = proba
    un_df.loc[mask, "threshold_used"] = thr
    un_df.loc[mask, "consensus"] = np.where(proba >= thr, "good", "bad")

mask_nb = (un_df.get("attacker_is_bot", 0) == 0) & (un_df.get("target_is_bot", 0) == 0)
un_df["extreme_99th"] = False
if thr_99_global is not None:
    un_df.loc[mask_nb, "extreme_99th"] = (un_df.loc[mask_nb, FEATURES] > thr_99_global).any(axis=1)

# ---------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------
out_csv = Path(args.input_json).with_suffix('.ensemble_predictions.csv')
out_cols = [
    '_source_file', 'time_raw', 'human_time',
    'map_start_time_raw', 'map_end_time_raw',
    'mod_name', 'consensus', 'score', 'threshold_used', 'extreme_99th',
] + FEATURES + ['attacker_name', 'target_name']
out_cols = [c for c in dict.fromkeys(out_cols) if c in un_df.columns]
un_df[out_cols].to_csv(out_csv, index=False)

n_good = int((un_df['consensus'] == 'good').sum())
n_bad  = int((un_df['consensus'] == 'bad').sum())
print(f">> scored {len(un_df):,} frags: {n_good} good, {n_bad} bad")
print(f">> threshold mode: {args.threshold_mode}")
print(f">> output: {out_csv}")
print()
print(">> per-weapon breakdown:")
for mod in weapon_models:
    cnt_good = int(((un_df['mod_name'] == mod) & (un_df['consensus'] == 'good')).sum())
    cnt_total = int((un_df['mod_name'] == mod).sum())
    if cnt_total > 0:
        print(f"   {mod:28s}  {cnt_good:5d} / {cnt_total:5d} good  ({100.0*cnt_good/cnt_total:.0f}%)")
