import subprocess
import os
from pathlib import Path

source = Path(r"C:\jactf_pipeline\a full jka install\game_directory\Jedi Academy\GameData\mme\captures\f0005.mp4")
target_limit = 9.8 * 1024 * 1024 # 9.8 MB

def test_crf(crf):
    out = source.with_name(f"test_crf_{crf}.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", str(source),
        "-vcodec", "libx264", "-crf", str(crf), "-preset", "faster",
        "-acodec", "aac", "-b:a", "128k",
        str(out)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    size = out.stat().st_size
    # clean up test file
    out.unlink()
    return size / (1024 * 1024)

print(f"{'CRF':>4} | {'Size (MB)':>10}")
print("-" * 18)
for crf in range(20, 36, 2):
    size_mb = test_crf(crf)
    marker = " <--" if size_mb < 9.5 else ""
    print(f"{crf:>4} | {size_mb:>10.2f}{marker}")
