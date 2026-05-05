import os
import subprocess
from pathlib import Path

captures_dir = Path(r"C:\jactf_pipeline\a full jka install\game_directory\Jedi Academy\GameData\mme\captures")

def compress_clips():
    count = 0
    for mp4 in captures_dir.glob("*.mp4"):
        if mp4.stat().st_size > 9.5 * 1024 * 1024:
            size_mb = mp4.stat().st_size / (1024 * 1024)
            print(f"Compressing {mp4.name} ({size_mb:.1f}MB) -> target < 10MB...")
            
            out_mp4 = mp4.with_suffix(".tmp.mp4")
            
            # Use ffmpeg to re-encode at a lower CRF (higher compression)
            # CRF 32 is usually a sweet spot for fast, highly compressed game footage
            cmd = [
                "ffmpeg", "-y", "-i", str(mp4),
                "-vcodec", "libx264", "-crf", "32", "-preset", "faster",
                "-acodec", "aac", "-b:a", "128k",
                str(out_mp4)
            ]
            
            # Run without noisy output unless it fails
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0 and out_mp4.exists():
                new_size_mb = out_mp4.stat().st_size / (1024 * 1024)
                print(f"  Success: {new_size_mb:.1f}MB. Replacing original.")
                out_mp4.replace(mp4)
                count += 1
            else:
                print(f"  Failed to compress {mp4.name}:")
                print(res.stderr)
                if out_mp4.exists():
                    out_mp4.unlink()
    
    if count == 0:
        print("All clips are already under 9.5MB or no clips were found.")
    else:
        print(f"Successfully compressed {count} clip(s).")

if __name__ == "__main__":
    compress_clips()