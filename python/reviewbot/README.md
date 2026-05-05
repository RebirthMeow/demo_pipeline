# Stage 7 — Discord review bot (reaction-based)

Community-curated highlight labelling via Discord reactions. Posts rendered clips to a designated channel, community members react with 👎 / 😐 / 👍 / 🗑️, votes accumulate in a local sqlite DB. Operator periodically exports for analysis.

For the full project context see [`C:\jactf_pipeline\README.md`](../../README.md).

## Architecture at a glance

```
operator (locally, only when posting/syncing):
    /post-batch   → creates a new public thread in the review channel,
                    uploads N clips, seeds 4 emoji reactions on each
    /tally        → posts a public summary of vote counts per clip
    /sync-votes   → re-read Discord reactions, reconcile DB
    /export-labels→ JSON dump of all (user, clip, label) rows

community (whenever, even when bot is offline):
    react with 👎 😐 👍 🗑️ on each clip

bot, on next startup:
    on_ready → sync_all_reactions() walks every posted clip,
               reads Discord reaction state, replaces DB votes.
    Discord is the source of truth.
```

## Why reactions, not buttons

**Reactions persist on the message regardless of bot uptime.** When the bot is offline:

- Buttons → user clicks fail with "this interaction failed" and are silently lost (Discord doesn't queue interactions for offline bots)
- Reactions → user's reaction stays on the message indefinitely; bot reads it on next startup

This matches the operator's intended workflow: run the bot occasionally (to post a batch and sync), shut it down between sessions, and let the community vote async over days/weeks. Zero hosting cost, zero missed votes.

## Two trust tiers — Discord ≠ training corpus

The bot deliberately keeps community votes **separate** from the local training corpus:

| who | how they label | where it goes | when training corpus is updated |
|---|---|---|---|
| **Operator** (you) | local Flask review UI ([`python/review/`](../review/)) | directly into `*_client_frags_aggregated.json` via `label_io.apply_label` | immediately on each vote |
| **Community** | Discord reactions on bot-posted clips | `python/reviewbot/votes.db` (per-(user, clip, label)) | never automatically — manual ingest only |

This is a feature, not an oversight. Community votes accumulate raw with full per-user history, so a future reliability layer can:

- Score each user's agreement with operator's votes / consensus over time
- Detect trolls (votes that consistently contradict majority + operator)
- Apply consensus rules ("≥3 trustworthy users with ≥66% agreement → merge into training")
- Then, and only then, tag as a training row via `label_io.apply_label`

That ingest layer is the **deferred Stage 7.5 work** — `/export-labels` produces the JSON; the analysis + merge script is TBD. Until then, your trained model only learns from your own labels.

## Setup (one-time, ~10 min)

### 1. Create the Discord application + bot

1. https://discord.com/developers/applications → **New Application** → name it (e.g. "JACTF Highlight Bot").
2. **Bot** tab → **Reset Token** → copy. **Treat like a password.**
3. Same page → enable **Server Members Intent** (needed for role gate).

### 2. Invite the bot

**OAuth2 → URL Generator**:

- Scopes: `bot`, `applications.commands`
- Bot permissions: `Send Messages`, `Attach Files`, `Embed Links`, `Add Reactions`, `Manage Messages` (for pruning multi-votes), `Read Message History`, `Use Application Commands`

Open the generated URL → pick the JACTF server → authorize.

### 3. Get the IDs

In Discord settings → Advanced → enable **Developer Mode**, then:

- Right-click server icon → Copy Server ID → `DISCORD_GUILD_ID`
- Right-click target channel → Copy Channel ID → `DISCORD_CHANNEL_ID`
- (optional) Right-click vote role → Copy Role ID → `DISCORD_ROLE_ID`

### 4. Set env vars

The `run.bat` script automatically loads variables from a local `.env` file if it exists. This is the safest way to store your token.

Create a file named exactly `.env` in `python/reviewbot/` (next to `bot.py`) and paste your IDs:

```text
DISCORD_TOKEN=MTAyN...
DISCORD_GUILD_ID=12345...
DISCORD_CHANNEL_ID=98765...
# optional
# DISCORD_ROLE_ID=...
# DISCORD_MIN_ACCOUNT_DAYS=30
```

Do not commit this `.env` file. It is already ignored by `.gitignore`.

### 5. Run

```powershell
cd C:\jactf_pipeline\python\reviewbot
.\run.bat
```

First run creates `.\venv\` and installs `discord.py`. You'll see:

```
[setup] creating venv at ...\reviewbot\venv
[setup] installing requirements
Starting review bot.  Ctrl+C to stop.
HH:MM:SS [INFO] reviewbot: db initialized at .../votes.db
HH:MM:SS [INFO] reviewbot: slash commands synced to guild ...
HH:MM:SS [INFO] reviewbot: logged in as JACTF Highlight Bot#1234 (id=...)
HH:MM:SS [INFO] reviewbot: running initial reaction sync — this catches up
                          votes received while bot was offline
HH:MM:SS [INFO] reviewbot: initial sync: 10 clips processed, 23 votes recorded,
                          0 reactions pruned (gates/duplicates)
HH:MM:SS [INFO] reviewbot: ready — watching guild ..., posting to channel ...
```

Then `/post-batch` is available in any channel of your server.

## Slash commands

| command | who | what |
|---|---|---|
| `/post-batch [name]` | operator | Reads `mme/clip_manifest.json`, creates a new public thread, uploads each `fNNNN.mp4` with embed + 4 seeded reactions, records `(clip_id, message_id, source_demo, time_raw, score)` in sqlite. |
| `/tally [name]` | operator | Posts a public summary table of vote counts per clip in the named batch (or most recent). |
| `/sync-votes [name]` | operator | Force re-read of Discord reaction state; rewrite DB to match. Useful if reactions accumulated while bot was offline and you want to verify the auto-sync worked. |
| `/export-labels [name]` | operator | Dump all `(user, clip, label, ts)` rows as JSON. Attached to your reply + saved at `python/reviewbot/exports/votes_<name>_<ts>.json`. |

**Note:** All commands are hardcoded to the Operator's specific Discord ID for security. No one else can trigger them, even if they have server admin permissions.

## Vote semantics

- **One vote per user per clip.** If a user reacts with multiple vote-emojis, the bot keeps the first-seen and removes the rest.
- **Toggle to change.** Remove your existing reaction, add a new one. Bot updates.
- **Toggle to clear.** Remove your reaction without adding a new one. Bot deletes the row.
- **Other emojis are ignored.** If someone reacts with 🔥 or 😍, the bot leaves it alone — those aren't vote emojis. Only 👎 😐 👍 🗑️ count.

### What happens when bot is offline

1. User reacts → reaction stored on Discord message (no bot needed)
2. (no immediate processing)
3. Bot starts later → on_ready runs `sync_all_reactions()` automatically
4. Sync walks every clip's current reaction state, applies gates retroactively, writes votes to DB
5. Bot is now ready for live events

`/sync-votes` does the same thing manually if you want to re-reconcile without restarting.

## Anti-spam gates

| gate | env var | default | applied |
|---|---|---|---|
| Server membership | `DISCORD_GUILD_ID` | required | always |
| Required role | `DISCORD_ROLE_ID` | optional, off | only when set |
| Account age | `DISCORD_MIN_ACCOUNT_DAYS` | optional, off | only when set |
| One vote per user | always on | hardcoded (sqlite primary key) | always |

For a tournament-community use case I'd suggest:

- `DISCORD_ROLE_ID` set to the "Player" or "Verified" role
- `DISCORD_MIN_ACCOUNT_DAYS=30` to filter throwaway accounts

When a non-eligible user adds a reaction, the bot:

1. Removes the reaction from the message
2. DMs the user explaining why (best-effort — silent if user has DMs disabled)
3. Logs the rejection at INFO level

Same logic during sync — non-eligible voters' reactions get pruned and they don't enter the DB.

## File size cap & Smart Compression

Discord caps attachments at **10 MB** on free-tier servers. To ensure every clip can be posted, the bot implements a **Smart Auto-Compression** loop:

1. The underlying render configuration (`mmeconfig.cfg`) now uses `-crf 26`, which naturally produces very high-quality 7-9 MB files for typical 12s clips.
2. If a longer or more complex clip still exceeds 9.5 MB, the bot intercepts it during `/post-batch` and recursively compresses it using `ffmpeg` with increasingly aggressive settings (CRF 28, 30, 32, etc.) until it safely fits under the limit.

You never have to manually compress clips; the bot guarantees they will fit.

## Embed Formatting
The bot automatically enhances the Discord presentation:
- **JA+ Color Codes:** Full support for standard (`^0`-`^9`) and extended (`^A`-`^Z`, `^a`-`^z`) Jedi Academy color codes, mapped to Discord ANSI colors for the POV player's name.
- **Custom Weapon Names:** A built-in table translates raw engine names (`MOD_REPEATER_ALT_SPLASH`) into clean, readable labels (`Repeater Alt Splash`).
- **Match Metadata:** The embed footer automatically extracts the Match ID and Date from the `seen_matches.json` dataset.

## File layout

```
python/reviewbot/
├── bot.py              ~16 KB — discord client, slash commands, reaction handlers, sync logic
├── config.py            ~3 KB — env-var loading, path resolution
├── db.py                ~9 KB — sqlite schema + queries (idempotent UPSERT, sync's atomic replace)
├── requirements.txt    16 B   — discord.py>=2.4
├── run.bat             ~1 KB  — venv bootstrap + launch
├── README.md           this file
├── .env.example        documenting all env vars
├── venv/               (auto-created on first run, gitignored)
├── votes.db            (auto-created, gitignored — sqlite WAL mode)
└── exports/            (auto-created when /export-labels first runs, gitignored)
```

## Running it locally — the "good enough" pattern

The simplest sustainable pattern:

```
weekend N:
    render clips → fire up bot → /post-batch → shut down bot

week N (bot offline):
    community sees clips, reacts at their leisure

weekend N+1:
    fire up bot → on_ready auto-syncs past week's votes →
    /post-batch next set → shut down bot

repeat
```

The bot only needs to be online to:
- Post new batches (so it can upload mp4s and seed reactions)
- Sync (which happens automatically on every startup, plus manually via `/sync-votes`)
- Export (so it can attach files to the reply)

It does NOT need to be online when:
- Community is voting (reactions accumulate on Discord)
- You're rendering / labelling locally (those are independent paths)

If you want always-on later (immediate vote acknowledgement, no startup-sync delay), Fly.io free tier handles it: copy the bot files, set env vars on Fly's secret store, persistent volume for `votes.db`. Same code, same DB, ~30 min migration.

## Troubleshooting

| symptom | likely cause |
|---|---|
| "DISCORD_TOKEN was rejected" | regenerated the token? Update the env var. |
| Bot online but slash commands not appearing | command sync done; restart your Discord client (Ctrl+R) or wait a minute. |
| `/post-batch` says "no clips found" | `mme/clip_manifest.json` missing — render some clips first. |
| Reactions disappear when added | gate failed (wrong server, missing role, too-young account). Check the bot logs. |
| Bot doesn't recognize reactions on old messages | `/sync-votes` to force a re-read. |
| Vote not in `/tally` after reacting | wait 1-2s (eventual consistency); if it persists, run `/sync-votes` to confirm. |
| Custom server emojis instead of standard 👎 etc. | not supported in v1 — bot only matches the four standard Unicode emojis. |
