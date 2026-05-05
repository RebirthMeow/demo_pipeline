# Jedi Academy CTF Highlight Pipeline

End-to-end ML-driven highlight-reel pipeline for Jedi Academy multiplayer
demos. Pulls demos from the JACTF tournament archive, scores every kill
("frag") with a per-weapon stacking ensemble, trims the good ones to 12-second
clips, renders them to mp4 via jaMME (a Q3-derivative movie engine), and
hands the rendered clips to a local Flask review UI for human labelling.
The labels feed back into the training corpus for the next iteration.

This is a personal/community tool for a specific multiplayer FPS community —
not a generic pipeline. Several pieces are hardcoded to my install path,
my Discord identity, and my JACTF account.

## Pipeline at a glance

| stage | tool | input | output |
|---|---|---|---|
| 0 | `python/fetch/fetch_jactf_demos.py` | JACTF API | `demos source/_jactf_new/*.dm_26` |
| 1 | `jkdemometadata.exe` | `*.dm_26` | `*.dm_26.dm_meta` (per-frame parsed JSON) |
| 2 | `scanner.py` | `.dm_meta` | `predict/<set>_frags.json` (per-set frag features) |
| 3 | `predict/predict_frags_ensemble.py` | `<set>_frags.json` + training corpus | `<set>_frags.ensemble_predictions.csv` |
| 4 | `predict/build_jamme_demolist.py` | predict CSV + `trimmed_map.json` | trimmed `.dm_26` clips + `mme/clip_manifest.json` + per-clip jaMME projects |
| 5 | `predict/render_clips.py` (via `render_resilient.bat`) | per-clip jaMME projects | `mme/captures/fNNNN.mp4` |
| 6 | `python/review/` Flask UI | `mme/clip_manifest.json` + clips | merged labels in `*_client_frags_aggregated.json` |
| 7 | (planned) Discord bot | rendered clips | community-aggregated labels |

Stages 1 + 2 are bundled in `regen_pipeline.ps1`. Stages 0–5 are chained by
`run_pipeline.ps1`. Stage 6 is run separately when you're ready to label.

Every stage is **idempotent**. Re-running with the same inputs is safe. Each
stage tracks "what's already been done" in its own state file (`seen_matches.json`,
`render_state.json`, `review_state.json`, etc.) and only does fresh work.

## Quick start

```powershell
cd C:\jactf_pipeline
.\run_pipeline.ps1                          # full pipeline, last 24h of new demos
.\run_pipeline.ps1 -Since 7d -LimitClips 5  # last week, render only first 5
.\run_pipeline.ps1 -SkipFetch -SkipRegen    # already have demos and .dm_meta?
```

Then open the review UI:

```powershell
cd C:\jactf_pipeline\python\review
.\run.bat
# open http://127.0.0.1:5057/
```

### Prerequisites

- **Windows** with Python 3.11+ on PATH. (3.13 used in the canonical venv.)
- **ffmpeg.exe** on PATH. **This is the gotcha.** If ffmpeg goes missing,
  jaMME's capture pipe writes 0-byte frames silently and you get
  "no capture produced after 90s wait" with empty `mme/capture/fNNNN/` dirs.
  Verify with `ffmpeg -version` from a cmd in `GameData/`.
- **A full JKA install** at `C:\jactf_pipeline\a full jka install\game_directory\Jedi Academy\GameData\`,
  including `jamme.exe`, `rd-jamme_x86.dll`, base assets, and `mme/` subtree.
- **DemoTrimmer.exe** at `C:\jactf_pipeline\python\trimming\demotrimmer.exe`.
- **JACTF account credentials** (the fetcher reuses cookies from a prior browser session).

### Verify the install

```powershell
# from PowerShell, anywhere
python --version              # >= 3.11
ffmpeg -version               # any recent build
Test-Path "C:\jactf_pipeline\jkdemometadata.exe"
Test-Path "C:\jactf_pipeline\python\trimming\demotrimmer.exe"
Test-Path "C:\jactf_pipeline\a full jka install\game_directory\Jedi Academy\GameData\jamme.exe"
```

All four should be green.

## File layout

```
C:\jactf_pipeline\
├── README.md                      ← you are here
├── file_inventory_audit.txt       ← user-curated audit driving the archive layout
├── run_pipeline.ps1               ← top-level orchestrator (stages 0-5)
├── regen_pipeline.ps1             ← stages 1+2 (jkdemometadata + scanner)
├── jkdemometadata.exe             ← custom C++ demo parser
├── scanner.py                     ← stage 2 aggregator
├── regen_perf.log                 ← per-demo timing for jkdemometadata
├── archive\                       ← stale-but-preserved files (see archive\README.md)
│   ├── legacy_predict\            ← update_good/bad/ignore + xen/al frags + new_*.txt
│   ├── old_per_set_corpora\       ← 3xen/oh/xen2/xen3/fiends/down/down2/download frags
│   ├── old_aggregated\            ← root-level *_aggregated.json no code reads
│   ├── chat_tools\                ← insults/jokes/build_chatdb/extract-chats + venv_nlp + python_chats
│   ├── training_demos\            ← demo_files (good/bad/ok 1516 demos), emails (28 demos)
│   ├── dm_meta_backups\           ← dm_meta_backup_2026-04-27_*.tar.gz
│   ├── scattered_demos\           ← root-level one-off .dm_26 + test_folder duplicate
│   ├── old_fetchers\              ← chromedriver + predecessor JACTF download scripts
│   └── utility_scripts\           ← convert-demos.ps1 (manual ad-hoc tool)
│
├── a full jka install\
│   └── game_directory\Jedi Academy\GameData\
│       ├── jamme.exe              ← Q3-derivative movie engine
│       ├── rd-jamme_x86.dll       ← jaMME's renderer
│       ├── render_resilient.bat   ← stage 5 entry point
│       └── mme\
│           ├── mmeconfig.cfg      ← jaMME pipe command + settings
│           ├── mmedemos.cfg       ← capture trigger (autoexec on demoList load)
│           ├── autoexec.cfg       ← override layer (cg_drawTimer 1, etc.)
│           ├── frags.txt          ← demolist (full batch)
│           ├── _single_clip.txt   ← demolist (per-clip render)
│           ├── clip_manifest.json ← stage 4 output → stage 5/6 input
│           ├── render_state.json  ← stage 5 per-clip status
│           ├── qconsole.log       ← jaMME console output (overwritten per run)
│           ├── demos\             ← staged fNNNN.dm_26 (clip inputs)
│           ├── project\fNNNN\     ← per-clip jaMME projects with <capture> XML
│           ├── mmedemos\          ← jaMME's per-demo .mme caches (binary)
│           ├── capture\fNNNN\     ← per-render scratch (clip.mkv → clip.mp4)
│           ├── captures\          ← FINAL flat folder of fNNNN.mp4
│           └── render_logs\fNNNN.log  ← per-clip stdout+qconsole capture
│
├── python\
│   ├── fetch\
│   │   ├── fetch_jactf_demos.py   ← stage 0
│   │   └── seen_matches.json      ← stage 0 dedup state
│   │
│   ├── trimming\
│   │   ├── demotrimmer.exe        ← extracts a window from a .dm_26
│   │   ├── trim_good_frags.py
│   │   └── demos source\<set>\    ← downloaded full-match demos
│   │       demos output\<set>\    ← trimmed 12s clips (named trm_<demoid>_<HH-MM.SSS>_<idx>.dm_26)
│   │       demos output\trimmed_map.json  ← trimmed-name → (full_demo, time_raw)
│   │
│   ├── predict\
│   │   ├── predict_frags_ensemble.py     ← stage 3 (ML training + inference)
│   │   ├── build_jamme_demolist.py       ← stage 4 (stage clips into mme/)
│   │   ├── render_clips.py               ← stage 5 (resilient runner)
│   │   ├── dedupe_good_against_bad.py    ← cross-corpus dedup utility
│   │   ├── check_duplicate_frags.py      ← duplicate detection utility
│   │   ├── export_good_frags_by_mod.py   ← per-weapon export utility
│   │   ├── good_client_frags_aggregated.json  ← TRAINING: confirmed good
│   │   ├── ok_client_frags_aggregated.json    ← TRAINING: dropped from train, kept for analysis
│   │   ├── bad_client_frags_aggregated.json   ← TRAINING: confirmed bad
│   │   ├── ignore_frags_aggregated.json       ← INFERENCE: filter at scoring time
│   │   ├── _jactf_new_frags.json              ← current per-set frag corpus
│   │   ├── _jactf_new_frags.ensemble_predictions.csv  ← current predict output
│   │   ├── backup\                            ← auto-backups of aggregated files
│   │   └── (legacy update_good/bad/ignore + older corpora moved to archive\legacy_predict\
│   │        and archive\old_per_set_corpora\ — see archive\README.md)
│   │
│   ├── review\                            ← stage 6 — Flask review UI
│   │   ├── README.md                      ← detailed per-tool docs
│   │   ├── app.py
│   │   ├── label_io.py                    ← reusable label-merge logic
│   │   ├── run.bat                        ← venv bootstrap + launch
│   │   ├── review_state.json              ← per-clip {label, ts, frag_key}
│   │   ├── current_session.json           ← (transient) session-start marker
│   │   ├── sessions\session_*.json        ← durable session logs
│   │   ├── templates\index.html
│   │   └── static\{style.css, app.js}
│   │
│   └── chats\                             ← unrelated — chat extraction / NLP
└── (top-level *.dm_26, *.dm_meta scattered, *_aggregated.json from older eras)
```

## Stage 0 — fetch new demos from JACTF

`python/fetch/fetch_jactf_demos.py` calls JACTF's `searchmatches` RPC,
downloads `.dm_26` demos via `getdemo.php`, names them
`<date>__<map>__<id8>__cNN_<player>.dm_26`, and writes each to
`python/trimming/demos source/_jactf_new/`.

State: `python/fetch/seen_matches.json` — a list of match IDs already pulled.
Re-running skips matches you already have. The fetcher early-stops when it
hits a streak of seen matches.

Hard limits: refuses any window > 30 days per invocation. Skips pure spectator
POVs (clients that never actually played). A "match" yields one demo per
client perspective — typical 4v4 CTF gives 8 demos per match.

### CLI

```powershell
python python\fetch\fetch_jactf_demos.py --since 1d --until 0d
python python\fetch\fetch_jactf_demos.py --since 30d --max-matches 5 --dry-run
```

### Common errors

- **401 from JACTF**: cookies are stale. Open `demos.jactf.com` in a browser,
  log in, the script picks up the refreshed cookie from your default profile.
- **"window > 30 days"**: split the backfill into multiple windows
  (`-Since 30d`, then `-Since 60d -Until 30d`, etc.).

## Stages 1+2 — extract metadata + aggregate

`regen_pipeline.ps1` orchestrates these together because they're tightly coupled.

### Stage 1: `jkdemometadata.exe`

A custom C++ demo parser. For each `.dm_26`, walks the netcode frame-by-frame
and emits a `<demo>.dm_26.dm_meta` JSON file with per-tick game state: positions,
weapons, view angles, kill events, flag captures, etc.

Cost: ~172 CPU-seconds per full match demo on a current laptop. The pipeline
parallelizes via PowerShell jobs. Per-demo timing logged to `regen_perf.log`.

This is the slow part. If you're iterating on prediction logic, use
`-SkipMetaRegen` so it just re-runs the scanner.

### Stage 2: `scanner.py`

Walks each player-set folder under `demos source/`, pairs `.dm_meta` with
`.dm_26`, extracts every kill event with its full feature vector
(view angles, victim airtime, attacker speed, missile lifetime, etc.),
and writes a per-set aggregated file at `python/predict/<set>_frags.json`.

`run_pipeline.ps1` snapshots `$preRegenTime` before regen and only treats
`*_frags.json` files newer than that as "fresh", so stages 3 and 4 ignore
stale files from prior experiments. **Important consequence**: re-running
the orchestrator with `-SkipRegen` skips predict + stage too because nothing
is "fresh." Run those manually if the inputs are unchanged but a code fix
needs to re-process them.

### CLI

```powershell
.\regen_pipeline.ps1                                    # full regen + scan
.\regen_pipeline.ps1 -SkipMetaRegen                     # scan only
.\regen_pipeline.ps1 -SkipBackupCheck -SkipWipe         # resume mode
```

Auto-discovers any folder under `demos source/` with `.dm_26` files. Only
re-runs scanner on "dirty" sets (ones with new demos this run), tracked in
`$script:DirtySets`.

## Stage 3 — predict

`python/predict/predict_frags_ensemble.py` is a per-weapon stacking ensemble.
Each weapon (`MOD_*`) has its own council of base learners:

```
rocket   xgb / lgb / histgb / gb
bryar    et / ada / rf / xgb
det_pack et / ada / rf / xgb
disruptor lgb / rf / et / xgb
bowcaster gb / lgb / xgb / histgb
... etc per mod_name
```

Training data: concatenation of `good_client_frags_aggregated.json` +
`bad_client_frags_aggregated.json`. **`ok` is dropped from training** (per
the v14 change) — empirical F1 was ~0.10–0.28 lower per weapon when ok rows
were kept. The `ignore` set filters out frags at inference time (broken demos,
feature-extraction failures, etc.).

Each base learner is calibrated, then the council averages probabilities
into a single per-frag score. Threshold per weapon is tuned on a held-out
fold to hit a target precision. Output: `<set>_frags.ensemble_predictions.csv`
with a `consensus` column (`good` | `ok` | `bad`) and a `score` column.

### CLI

```powershell
cd C:\jactf_pipeline\python\predict
.\venv\Scripts\python.exe predict_frags_ensemble.py _jactf_new_frags.json
.\venv\Scripts\python.exe predict_frags_ensemble.py _jactf_new_frags.json --threshold-mode high-precision
```

`--threshold-mode high-precision` biases each weapon's threshold toward
fewer-but-more-confident predictions — useful when you don't want to wade
through a long false-positive review queue.

## Stage 4 — build jaMME demolist + manifest

`python/predict/build_jamme_demolist.py` consumes the predict CSV and stages
clips for rendering.

For every row where `consensus == 'good'`:

1. Look up the trimmed clip in `trimmed_map.json` by `(full_demo, time_raw)`.
   If a trim doesn't exist, run `demotrimmer.exe` to produce a 12-second
   window centered ±6s on the kill (`human_to_ms(human_time)`, NOT `time_raw` —
   see "Known gotchas" below).
2. Copy the trimmed clip into `mme/demos/fNNNN.dm_26`.
3. Write `mme/project/fNNNN/fNNNN.cfg` — a tiny XML with `<capture><start>0</start><end>12000</end>...</capture>`.
4. Append `"fNNNN" "fNNNN"` to `mme/frags.txt` (the full demolist).
5. Add a row to `mme/clip_manifest.json` with the source frag's metadata
   (weapon, attacker, target, score, source_demo, time_raw, predict_csv).

On every run it wipes stale `fNNNN.*` files in `mme/{demos, project, mmedemos, capture}`
and rebuilds from scratch — except when Windows file locks hold open files
from a recently-finished render (in which case the wipe silently fails;
re-run after the render fully exits).

### CLI

```powershell
cd C:\jactf_pipeline
python python\predict\build_jamme_demolist.py --csv python\predict\_jactf_new_frags.ensemble_predictions.csv
python python\predict\build_jamme_demolist.py --csv ... --limit 5    # smoke test
```

Multiple CSVs can be passed; clips get sequentially-numbered IDs across all
of them.

## Stage 5 — render

`python/predict/render_clips.py`, wrapped by `render_resilient.bat`. Runs
jaMME once per clip with a single-line demolist:

```
"fNNNN" "fNNNN"
```

Why per-clip instead of one batch run: jaMME's `mmedemos.cfg` autoexec
(which fires `capture pipe 60 clip`) only triggers in `+demoList` mode,
not `+demo` mode. With a 1-line demolist we get the autoexec but only
one clip per process invocation. This isolates each clip's output and
makes retry-on-crash possible.

The runner is **resilient**:
- per-clip timeout (default 180s, `--timeout` to override)
- per-clip retries on crash / no-output / tiny-output (default 2 retries, `--max-retries`)
- timestamps each attempt in `mme/render_state.json`
- captures jaMME's stdout + the `mme/qconsole.log` content into `mme/render_logs/fNNNN.log`
- skips clips already done (mp4 in `mme/captures/` newer than the source `.dm_26`)
- safe to Ctrl+C and re-run; resumes from prior state
- `--reset` discards prior state and re-renders everything

After jaMME exits cleanly, the runner polls `mme/capture/<cid>/clip.mp4` for
size stability (file exists AND size unchanged for 2s, up to 90s total). This
is because ffmpeg's `-preset slow` queue can take 30-90s to drain after jaMME
closes the pipe. One-shot checking right after jaMME exit gives false-negatives.

On success, moves `mme/capture/<cid>/clip.mp4` → `mme/captures/<cid>.mp4`.

### CLI

```powershell
cd C:\jactf_pipeline\a full jka install\game_directory\Jedi Academy\GameData
.\render_resilient.bat                          # render everything pending
.\render_resilient.bat --status                 # just print state, don't render
.\render_resilient.bat --limit 3                # smoke test
.\render_resilient.bat --only f0042 f0099       # render specific clips
.\render_resilient.bat --reset                  # forget state, render all
.\render_resilient.bat --timeout 240 --max-retries 5
```

## Stage 6 — review UI

A local Flask app at `python/review/`. Plays each `fNNNN.mp4` in a looping
HTML5 player with native controls, vote buttons (bad / ok / good / ignore),
keyboard shortcuts (`1`/`2`/`3`/`4` vote, `←`/`→` nav, `space` pause,
`j`/`l` ±1s, `,`/`.` frame step, `m` mute, `r` replay, `u` undo).

Each vote merges into the matching aggregated training file (with backup
to `predict/backup/` and dedup by `(_source_file, time_raw)`). Re-labelling
removes from the prior corpus before adding to the new one.

Session lifecycle: a "Finish session" button writes a durable log to
`python/review/sessions/session_YYYYMMDD_HHMMSS.json` and offers a clean
server shutdown. Multiple finishes per server start are allowed; each
writes its own log.

See `python/review/README.md` for the full per-tool reference.

### CLI

```powershell
cd C:\jactf_pipeline\python\review
.\run.bat
# http://127.0.0.1:5057/
```

First run creates a per-tool venv at `python/review/venv/` and installs Flask.
Re-runs reuse it.

## Stage 7 — Discord bot for community labelling (reaction-based)

Posts rendered clips to a designated Discord channel by creating a public thread; community members react with 👎 / 😐 / 👍 / 🗑️ to vote; votes accumulate in a local sqlite DB. Designed for "run locally, occasionally" — bot only needs to be online to post a batch and sync votes; community reactions accumulate on Discord messages independently of bot uptime, and the bot reads them on next startup.

Architecture lives in `python/reviewbot/`:
- `bot.py` — discord client, slash commands (`/post-batch`, `/tally`, `/sync-votes`, `/export-labels`), reaction handlers, JA+ color mapping, auto-compression via ffmpeg for oversized files.
- `db.py` — sqlite schema (clips + votes, idempotent UPSERT, atomic `replace_votes_for_clip` for sync)
- `config.py` — env-var loading

Commands are hardcoded to the Operator's Discord ID for security. 

Setup walkthrough lives in [`python/reviewbot/README.md`](python/reviewbot/README.md).
Short version: register a Discord application, set your token and IDs in `python/reviewbot/.env`, run `.\run.bat`.

Same `JACTF_ROOT` / `JACTF_GAMEDATA` env vars used everywhere else apply,
so the bot finds `clip_manifest.json` and `mme/captures/*.mp4` automatically.

## Top-level orchestrator (`run_pipeline.ps1`)

```powershell
.\run_pipeline.ps1                                      # last 24h
.\run_pipeline.ps1 -Since 7d                            # last week
.\run_pipeline.ps1 -Since 60d -Until 30d                # backfill: days 30-60
.\run_pipeline.ps1 -SkipFetch                           # skip stage 0
.\run_pipeline.ps1 -SkipRegen -SkipPredict -SkipStage   # only render
.\run_pipeline.ps1 -MaxMatches 5 -LimitClips 5          # tiny end-to-end test
.\run_pipeline.ps1 -ThresholdMode high-precision        # narrower predict gate
```

Skip flags are independent. Stages 3 and 4 are gated on "freshness" —
they only consume files newer than the orchestrator's `$preRegenTime`
snapshot. So `-SkipRegen` will silently skip stages 3 and 4 too unless
you've manually produced fresh inputs.

The orchestrator's stage-3-and-4 freshness gate exists specifically to
avoid re-predicting / re-staging stale `*_frags.json` from prior
experiments (e.g. `3xen_frags.json`, `download_frags.json`) that no
longer have matching `demos source/<set>/` folders.

## State files reference

The pipeline is a chain of idempotent transforms. Each stage's state lives
in a single canonical location:

| file | owned by | purpose |
|---|---|---|
| `python/fetch/seen_matches.json` | stage 0 | match IDs already pulled |
| `regen_perf.log` | stage 1 | per-demo timing log (append-only) |
| `python/predict/<set>_frags.json` | stage 2 | per-set frag features |
| `python/predict/<set>_frags.ensemble_predictions.csv` | stage 3 | scores + consensus |
| `python/trimming/demos output/trimmed_map.json` | stage 4 | trimmed-name → (full_demo, time_raw) |
| `mme/clip_manifest.json` | stage 4 | clip_id → metadata |
| `mme/frags.txt` | stage 4 | full demolist (`"fNNNN" "fNNNN"` per line) |
| `mme/render_state.json` | stage 5 | per-clip render status + last-attempt log |
| `mme/render_logs/fNNNN.log` | stage 5 | per-clip stdout + qconsole capture |
| `python/predict/{good,ok,bad}_client_frags_aggregated.json` | stage 6 | training corpus |
| `python/predict/ignore_frags_aggregated.json` | stage 6 | inference-time filter |
| `python/predict/backup/` | stage 6 | timestamped corpus backups before each write |
| `python/review/review_state.json` | stage 6 | per-clip UI label state |
| `python/review/current_session.json` | stage 6 | transient session-start marker |
| `python/review/sessions/session_*.json` | stage 6 | durable session logs |

Wiping any of these resets the corresponding stage. Most are safe to delete
selectively if a stage misbehaves and you want a clean re-run.

## Known gotchas

These are everything that has surprised someone (mostly me) at least once.
Each was a real debug session.

### ffmpeg disappearing from PATH

jaMME's `mme_pipeCommand` is `ffmpeg -f avi -i - ...`. If `ffmpeg` isn't on
PATH, Windows runs `cmd /c ffmpeg ...` which prints "not recognized" and
exits, leaving jaMME's pipe handle pointing at a closed stdin.

**Symptom**: every `FS_Write` call writes 0 bytes. Hundreds of
`FS_Write: 0 bytes written` lines in `mme/render_logs/fNNNN.log`.
`mme/capture/fNNNN/` is empty (no `clip.mkv`, no PNGs). The 90s
ffmpeg-finalize poll times out cleanly because there was nothing to finalize.

**Fix**: install ffmpeg, ensure `ffmpeg -version` works in the cwd you run
the pipeline from. Or drop a single `ffmpeg.exe` into `GameData/` (cwd
resolution wins over PATH for bare names).

**Prevention**: consider editing `mme/mmeconfig.cfg` to call `ffmpeg.exe`
explicitly. The bare-name resolution is too quiet about its failure mode.

### DemoTrimmer treats `time_raw` as match-time

`demotrimmer.exe` interprets timestamps as MATCH-time (post-warmup). For
older datasets (xen / al / oh) `time_raw == match_time` so the bug was
invisible. JACTF demos record warmup, so `time_raw` is 110-280s ahead of
`match_time`. Pre-fix trims were centered on the wrong moment.

**Fix in code**: `build_jamme_demolist.py` now passes `human_to_ms(human_time)`
not `time_raw` to `demotrimmer.exe`. **Bug class**: any time you add a new
upstream demo source with different timing semantics, audit the trimmer.

### jaMME `+demoList` vs `+demo`

The capture autoexec in `mmedemos.cfg` only fires under `+demoList` (batch)
mode. Direct `+demo` invocation skips the autoexec, so no capture pipe is
opened, so no clip is produced.

**Workaround**: always use `+demoList` even for a single clip. `render_clips.py`
writes a 1-line `_single_clip.txt` per render.

### jaMME caches per-demo state in `mme/mmedemos/<name>.mme`

These are NOT config files — they're pre-parsed binary demo caches. If you
replace `mme/demos/fNNNN.dm_26` with new content but leave `mme/mmedemos/fNNNN.mme`
in place, jaMME plays the cached old content silently.

**Fix**: stage 4's wipe step removes `mme/mmedemos/fNNNN.mme` along with the
`.dm_26`. If the wipe is silently defeated by Windows file locks (from a
recently-finished render), you'll get the wrong clip rendered.

### `mme/capture/<cid>/clip.mp4` is the per-render scratch path

NOT `GameData/clip.mp4` and NOT `mme/captures/<cid>.mp4` directly. jaMME's
`%o` placeholder in `mme_pipeCommand` resolves to `mme/capture/<projectname>/clip`,
where `<projectname>` matches the capture project loaded by the demolist.

`render_clips.py` moves `mme/capture/<cid>/clip.mp4` → `mme/captures/<cid>.mp4`
on success. The flat `mme/captures/` directory is the canonical "final clip"
location.

### ffmpeg `-preset slow` finalizes asynchronously

Up to 90 seconds after jaMME exits, ffmpeg is still draining its frame queue
and finalizing the mp4 mux. Checking `clip.mp4` immediately after jaMME exit
gives a false-negative.

**Fix**: `render_clips.py` polls for size stability (file exists AND size
unchanged for 2s, up to 90s total). The right knobs are constants at the top
of the file — `FFMPEG_MAX_FINALIZE_S`, `FFMPEG_STABLE_S`.

### Linux mount metadata lag (development-only)

When working on this from a Linux sandbox view of the Windows filesystem,
the mount sometimes serves stale metadata (mtime, size, content with
trailing NUL bytes) for several seconds after a Windows-side write. The
file is correct on Windows; the mount just hasn't refreshed.

**Symptom**: bash sees an old version, Python on Windows sees the new
version. JSON parse fails on the mount with "Extra data" or "Unterminated
string" while the file is genuinely valid.

**Fix**: nothing — wait, or read via the Windows path directly. Doesn't
affect production runs.

### `update_good.py` / `update_bad.py` are hardcoded to `xen_frags.json`

Line 23 of each script: `JSON_FILE = ROOT / "xen_frags.json"`. The legacy
text-file label-merge workflow (`new_good_frags.txt` → `update_good.py` →
`good_client_frags_aggregated.json`) only resolves frags from the `xen` set.
Feeding a JACTF clip name through this path produces
`[WARN] time_raw N not in frags JSON` for every line.

**Workaround**: Stage 6's `python/review/label_io.py` generalizes the source
lookup over each clip's `predict_csv` field, so the Flask UI works for any
set without modifying the legacy scripts. If you want a single code path,
add `--source` CLI flag to `update_good.py` / `update_bad.py` and have it
default to `xen_frags.json`.

## Operational runbook

### Daily / weekly review cycle

```powershell
# fetch + render + review
cd C:\jactf_pipeline
.\run_pipeline.ps1 -Since 1d
cd python\review; .\run.bat
# label, finish session, shutdown
```

### After labels are in, retrain

```powershell
cd C:\jactf_pipeline\python\predict
.\venv\Scripts\python.exe predict_frags_ensemble.py _jactf_new_frags.json
# new ensemble_predictions.csv replaces the old one; old model decisions
# are not retained. The training set has now grown by N labels.
```

### "I rendered a bad batch — start over"

```powershell
cd C:\jactf_pipeline\a full jka install\game_directory\Jedi Academy\GameData

# wipe all stage 4/5 artifacts
Remove-Item mme\demos\f????.dm_26 -Force -ErrorAction SilentlyContinue
Remove-Item mme\mmedemos\f????.mme -Force -ErrorAction SilentlyContinue
Remove-Item mme\capture\f???? -Force -Recurse -ErrorAction SilentlyContinue
Remove-Item mme\captures\f????.mp4 -Force -ErrorAction SilentlyContinue
Remove-Item mme\render_state.json -Force -ErrorAction SilentlyContinue
Remove-Item ffmpeglog.txt -Force -ErrorAction SilentlyContinue

# re-stage and re-render
cd C:\jactf_pipeline
python python\predict\build_jamme_demolist.py --csv python\predict\_jactf_new_frags.ensemble_predictions.csv
cd "a full jka install\game_directory\Jedi Academy\GameData"
.\render_resilient.bat
```

### "I want to re-label something I already locked in"

```powershell
# launch review UI again — review_state.json persists, so labelled clips
# show as labelled. Click a different vote button or press 1/2/3/4 to
# update. label_io.apply_label removes from old corpus and adds to new
# atomically.
cd C:\jactf_pipeline\python\review
.\run.bat
```

### "predict isn't picking up new training data"

`predict_frags_ensemble.py` reads `good_client_frags_aggregated.json` and
`bad_client_frags_aggregated.json` directly at training time — there's no
intermediate cache. If your labels aren't reflected, check:

```powershell
# how many rows in each corpus?
python -c "import json; print('good:', len(json.load(open(r'C:\jactf_pipeline\python\predict\good_client_frags_aggregated.json'))))"
python -c "import json; print('bad:', len(json.load(open(r'C:\jactf_pipeline\python\predict\bad_client_frags_aggregated.json'))))"

# find your most recent backup to confirm a write happened
Get-ChildItem C:\jactf_pipeline\python\predict\backup\good_client_frags_aggregated_*.json |
  Sort-Object LastWriteTime -Descending | Select -First 3
```

If a Stage 6 write attempt failed silently (rare), the backup wouldn't be
created. Check `python/review/review_state.json` to confirm what the UI
thinks is in the corpus, and the predict-dir aggregated files for what
actually is.

## Verification

After any pipeline run, sanity checks:

```powershell
# 1. fresh demos arrived
Get-ChildItem "C:\jactf_pipeline\python\trimming\demos source\_jactf_new\*.dm_26" |
  Sort-Object LastWriteTime -Descending | Select -First 5 Name, Length, LastWriteTime

# 2. dm_meta files generated
Get-ChildItem "C:\jactf_pipeline\python\trimming\demos source\_jactf_new\*.dm_meta" |
  Sort-Object LastWriteTime -Descending | Select -First 5 Name, Length, LastWriteTime

# 3. predict CSV produced
Get-ChildItem C:\jactf_pipeline\python\predict\_jactf_new_frags.ensemble_predictions.csv |
  Format-Table Name, Length, LastWriteTime

# 4. clip_manifest written
$cm = Get-Content "C:\jactf_pipeline\a full jka install\game_directory\Jedi Academy\GameData\mme\clip_manifest.json" -Raw
($cm | ConvertFrom-Json).Count

# 5. mp4s rendered
Get-ChildItem "C:\jactf_pipeline\a full jka install\game_directory\Jedi Academy\GameData\mme\captures\f????.mp4" |
  Format-Table Name, Length, LastWriteTime
```

All five should produce reasonable counts and recent timestamps.

## Future work

- **Discord bot for community labelling** (Stage 7). Replaces the Flask UI's
  reach problem with native Discord identity, role gates, and inline mp4
  playback. See `python/review/README.md` future-work section for the
  architecture sketch.
- **Generalize `update_good.py` / `update_bad.py`** to take a `--source`
  flag, removing the `xen_frags.json` hardcoding so a single label-merge
  path works for both the legacy text-file workflow and JACTF data.
- **Profile `jkdemometadata.exe`** — full-match demos take ~172 CPU-sec each.
  Likely dominated by JSON serialization or memory allocation. Not blocking,
  but the slowest stage by an order of magnitude.
- **Per-weapon coverage analysis** — confirm we have enough labelled examples
  per `mod_name` for the per-weapon councils to converge. Currently good /
  bad are 567 / 1269; some councils may be under-trained on rare weapons.
- **Auto-retrain on label threshold** — every Nth label, automatically
  re-fit the ensemble and surface the new precision/recall to the UI.
- **Inline "play at 6.0s"** in the review UI so the kill moment is centered
  on the controls strip when the user pauses to scrub.

## License

Personal project. No license. Use at your own risk.
