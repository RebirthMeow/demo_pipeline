# Stage 6 — review UI

Local Flask app for grading rendered highlight clips. Each thumbs-up/ok/down/ignore vote merges the underlying frag into the appropriate `*_client_frags_aggregated.json` file used by `predict_frags_ensemble.py` for the next training run.

For the full project context (stages 0–5, file layout, gotchas), see [`C:\jactf_pipeline\README.md`](../../README.md).

## Run

```powershell
cd C:\jactf_pipeline\python\review
.\run.bat
# open http://127.0.0.1:5057/
```

First run creates a per-tool venv at `.\venv\` and installs Flask. Re-runs reuse it.

## Keyboard

| key | action |
|---|---|
| `1` | bad |
| `2` | ok |
| `3` | good |
| `4` | ignore |
| `←` / `→` | prev / next clip |
| `space` | pause / play |
| `j` / `l` | seek ±1s |
| `,` / `.` | frame step ±1 (60fps clips) |
| `u` | undo current clip's label |
| `m` | mute / unmute (state persists across clips) |
| `r` | replay current clip from start |
| `Esc` | close any open modal |

A repeated vote on a clip's already-set label clears it (toggle). Each vote auto-advances to the next clip.

## Session lifecycle

A "session" is the period between server starts and either Ctrl+C or a "Finish session" click. Boundaries:

- **Server start** opens a session. `current_session.json` is written with the start timestamp and a snapshot of the four corpus sizes.
- **Each vote** is durably persisted immediately to the matching `*_client_frags_aggregated.json` and to `review_state.json`. There's no buffering — losing the server doesn't lose votes.
- **Finish session** writes a snapshot to `sessions/session_YYYYMMDD_HHMMSS.json` (start/end timestamps, label counts, per-clip table, corpus before/after). The session log is the durable audit trail. After finishing, you can:
  - Keep the server running — a new session opens automatically on the next vote.
  - Shut down the server — `/api/shutdown` does response-first then `os._exit(0)` after 300ms so the browser sees the acknowledgement.
- **Server shutdown** doesn't auto-write a session log; click Finish first if you want one. Votes are durable regardless.

The Finish button is in the header. It's disabled until at least one clip is labelled (an all-unset session has no signal to log). Press `Esc` to dismiss the confirmation modal at any step.

## How writes work

When you vote `good` on clip `f0001`:

1. App finds `f0001` in `mme/clip_manifest.json` — gets `predict_csv`, `time_raw`, `source_demo`.
2. Loads the source frags JSON (e.g. `_jactf_new_frags.json`) inferred from `predict_csv`.
3. Looks up the canonical frag by `(time_raw, normalised _source_file)` — same algorithm as `update_good.py:31-94`.
4. Tags the frag with `{consensus: good, label_source: review_ui, label_ts: now}`.
5. Backs up `good_client_frags_aggregated.json` to `predict\backup\good_client_frags_aggregated_YYYYMMDD_HHMMSS.json`.
6. Appends to `good_client_frags_aggregated.json`, dedup-keyed by `(_source_file, time_raw)`.
7. Updates `review/review_state.json` so the UI knows this clip is labeled.

Re-labelling a clip removes it from the previous label's aggregated file before adding to the new one — predict_frags_ensemble.py would see contradictory training signal otherwise.

## Why this bypasses `update_good.py`

`update_good.py` and `update_bad.py` are hardcoded at line 23 to read `xen_frags.json` as the source corpus. JACTF clips come from `_jactf_new_frags.json`, so the legacy path can't resolve them — `time_raw` lookups would fail with `[WARN] not in frags JSON`.

The `label_io.py` module replicates `update_good.py`'s dedup logic verbatim (`normalise_name`, `dedup_key`) but generalizes the source lookup over `predict_csv` (the field is on each clip in `clip_manifest.json`). End result: same aggregated-file shape, same dedup behaviour, more sources supported.

If you want to fold this generalization back into `update_good.py` / `update_bad.py` for the legacy text-file workflow, the easy path is to add an `--source` CLI flag and keep the old default of `xen_frags.json`.

## File layout

```
review/
├── app.py                  Flask routes
├── label_io.py             corpus merge + session lifecycle — reusable from CLI too
├── requirements.txt        flask
├── run.bat                 venv bootstrap + launch
├── review_state.json       (auto-created) {clip_id: {label, ts, frag_key}}
├── current_session.json    (transient) session-start marker, deleted on Finish
├── sessions/
│   └── session_*.json      durable session logs (audit trail)
├── templates/index.html
└── static/
    ├── style.css
    └── app.js
```

## Sanity-checking a label round-trip

```powershell
# 1. baseline
python -c "import json; print(len(json.load(open(r'C:\jactf_pipeline\python\predict\good_client_frags_aggregated.json'))))"
# e.g. 567

# 2. label one clip via UI as good

# 3. confirm
python -c "import json; print(len(json.load(open(r'C:\jactf_pipeline\python\predict\good_client_frags_aggregated.json'))))"
# 568

# 4. confirm backup
ls C:\jactf_pipeline\python\predict\backup\good_client_frags_aggregated_*.json | sort -bottom 1

# 5. confirm review_state
type C:\jactf_pipeline\python\review\review_state.json
```

## API surface

| route | method | body | returns |
|---|---|---|---|
| `/` | GET | — | `index.html` |
| `/video/<cid>.mp4` | GET | — | streams from `mme/captures/<cid>.mp4` (honors Range for scrubbing) |
| `/api/clips` | GET | — | `{clips: [...], counts: {...}, total: N}` |
| `/api/state` | GET | — | raw `review_state.json` |
| `/api/label` | POST | `{clip_id, label}` (`label` ∈ `good`/`ok`/`bad`/`ignore`/`unset`) | merge result |
| `/api/finish` | POST | — | session summary + log_path |
| `/api/shutdown` | POST | — | acknowledgement; server `os._exit(0)`s 300ms later |

The server binds to `127.0.0.1` only — this is a local-only tool, never expose it on a public network.

## Future work — Stage 7 ideas

- **Discord bot** for community-scale labelling. Native identity, free hosting, anti-spam baked in via role gates. The label-merge logic in `label_io.py` is directly reusable on the ingest side; only the "where do votes come from" plumbing changes. See top-level [`README.md`](../../README.md) for the architecture sketch.
- Auto-trigger `predict_frags_ensemble.py` retrain on every Nth label, surface new precision/recall to the UI.
- Inline "play at 6.0s" so the kill moment is centered on the controls.
- Bulk-import an existing labeling session (CSV of `clip_id,label`) so you can rapid-grade offline and apply.
- Skip-back nav history (`Ctrl+Z` for "undo navigation step").
- Surface predict's original `consensus` next to the clip's current label so the UI shows where you agreed or disagreed with the model — useful for spot-checking model drift over time.
