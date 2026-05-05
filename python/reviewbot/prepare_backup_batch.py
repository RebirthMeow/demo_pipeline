import json
import random
import shutil
import os
from pathlib import Path

# Paths
ROOT = Path(__file__).resolve().parents[2]
OK_POOL = ROOT / "python" / "predict" / "ok_client_frags_aggregated.json"
GOOD_POOL = ROOT / "python" / "predict" / "good_client_frags_aggregated.json"
ARCHIVE_OK = ROOT / "archive" / "demo files" / "ok"
ARCHIVE_GOOD = ROOT / "archive" / "demo files" / "good"
BACKUP_LANDING = ROOT / "python" / "trimming" / "demos source" / "_backup_batch"

def get_year(meta_name):
    import re
    m = re.search(r'(\d{4})-\d{2}-\d{2}', meta_name)
    return int(m.group(1)) if m else 9999

def prepare_backup(count=20):
    if not OK_POOL.exists() or not GOOD_POOL.exists():
        print("Error: Pools not found.")
        return

    print("Loading pools...")
    with open(OK_POOL, 'r', encoding='utf-8') as f: ok_data = json.load(f)
    with open(GOOD_POOL, 'r', encoding='utf-8') as f: good_data = json.load(f)

    # Filter for Rocket shots
    ok_rockets = [c for c in ok_data if c.get("mod_name") in ["MOD_ROCKET", "MOD_ROCKET_SPLASH"]]
    good_rockets = [c for c in good_data if c.get("mod_name") in ["MOD_ROCKET", "MOD_ROCKET_SPLASH"]]
    
    # Sort good rockets by age (oldest first)
    good_rockets.sort(key=lambda x: get_year(x.get("_source_file", "")))

    print(f"Found {len(ok_rockets)} OK and {len(good_rockets)} GOOD rocket shots.")
    
    # Selection: 5 random OK, 5 Oldest GOOD
    ok_sample = random.sample(ok_rockets, min(5, len(ok_rockets)))
    good_sample = good_rockets[:5]
    
    batch = ok_sample + good_sample
    random.shuffle(batch)
    
    # Prepare landing zone
    if BACKUP_LANDING.exists():
        shutil.rmtree(BACKUP_LANDING)
    BACKUP_LANDING.mkdir(parents=True, exist_ok=True)
    
    copied = 0
    for clip in batch:
        meta_name = clip.get("_source_file")
        if not meta_name: continue
            
        demo_name = meta_name.replace(".dm_meta", "")
        found = False
        
        # Check primary archive locations
        for base in [ARCHIVE_OK, ARCHIVE_GOOD]:
            if (base / meta_name).exists() and (base / demo_name).exists():
                shutil.copy2(base / meta_name, BACKUP_LANDING / meta_name)
                shutil.copy2(base / demo_name, BACKUP_LANDING / demo_name)
                copied += 1
                found = True
                break
        
        if not found:
            # Fallback recursive search
            for root, dirs, files in os.walk(ROOT / "archive" / "demo files"):
                if meta_name in files and demo_name in files:
                    shutil.copy2(Path(root) / meta_name, BACKUP_LANDING / meta_name)
                    shutil.copy2(Path(root) / demo_name, BACKUP_LANDING / demo_name)
                    copied += 1
                    found = True
                    break
            if not found:
                print(f"  [warn] Could not find: {meta_name}")

    print(f"\nSuccessfully staged {copied} clips (5 Grey Zone, 5 Old Gold) to {BACKUP_LANDING}")
    print("\nTo process this backup batch, run:")
    print("  .\\run_pipeline.ps1 -SkipFetch")

if __name__ == "__main__":
    prepare_backup(10)
