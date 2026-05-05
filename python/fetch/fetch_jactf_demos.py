#!/usr/bin/env python3
"""
fetch_jactf_demos.py — pull new match demos from https://demos.jactf.com/

Stage 0 of the highlight pipeline.  Calls the public JSON-RPC search endpoint
directly (no browser / Selenium), paginates newest-first, and downloads each
match's player POV demos until it hits a stop condition (--since cutoff,
--max-matches, or a streak of already-known matches).

Endpoints (discovered via DevTools):
  GET https://demos.jactf.com/minrpc.py?rpc=searchmatches&match=true&offset=N&limit=M
  GET https://demos.jactf.com/getdemo.php?demo=<urlencoded cygdrive path>

State:
  <repo>/python/fetch/seen_matches.json    {match_id -> {downloaded_at, players, ...}}

Output:
  <repo>/python/trimming/demos source/_jactf_new/<date>__<map>__<id8>__c<NN>_<player>.dm_26
  e.g.  2026-04-25__mpctf4__00af2cf5__c06_best.dm_26
  (full 16-char match_id is preserved in seen_matches.json)

The archive holds ~21K matches.  Default --since is 1 day so a first run
doesn't pull all of history.  HARD SAFETY: each invocation can fetch a window
of at most 30 days — no override flag.  To backfill further, run again with
an explicit older window using --since/--until:

  python fetch_jactf_demos.py --since 30d                 # last 30 days
  python fetch_jactf_demos.py --since 60d --until 30d     # days 30-60 ago
  python fetch_jactf_demos.py --since 90d --until 60d     # days 60-90 ago

Examples:
  python fetch_jactf_demos.py                          # incremental, last 24h
  python fetch_jactf_demos.py --since 30d              # last 30 days
  python fetch_jactf_demos.py --since 2026-04-01       # from explicit date
  python fetch_jactf_demos.py --since 30d --until 0d   # explicit upper bound
  python fetch_jactf_demos.py --max-matches 5          # smoke-test 5 newest
  python fetch_jactf_demos.py --dry-run                # preview only
  python fetch_jactf_demos.py --reset                  # forget state
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("missing dependency: pip install requests")


# ── paths ─────────────────────────────────────────────────────────────────────
# ROOT auto-detects: fetch/<file>.py → up 2 = repo root.  JACTF_ROOT overrides.
ROOT       = Path(os.environ.get("JACTF_ROOT") or Path(__file__).resolve().parents[2])
FETCH_DIR  = ROOT / "python" / "fetch"
LANDING    = ROOT / "python" / "trimming" / "demos source" / "_jactf_new"
SEEN_FILE  = FETCH_DIR / "seen_matches.json"


# ── endpoints ─────────────────────────────────────────────────────────────────
SEARCH_URL   = "https://demos.jactf.com/minrpc.py"
DOWNLOAD_URL = "https://demos.jactf.com/getdemo.php"

REQUEST_HEADERS = {
    "Accept":           "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer":          "https://demos.jactf.com/match.html",
    "User-Agent":       (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    ),
}


# ── tunables ──────────────────────────────────────────────────────────────────
PAGE_SIZE_DEFAULT     = 50
SEEN_STREAK_DEFAULT   = 20      # consecutive already-seen matches → stop paging
SINCE_DEFAULT         = "1d"    # default lower-bound: last 24h
UNTIL_DEFAULT         = "0d"    # default upper-bound: now (i.e. up to the present)
MAX_WINDOW_DAYS       = 30      # HARD SAFETY: max time span per single invocation
MAX_WINDOW_MS         = MAX_WINDOW_DAYS * 86_400 * 1000
TIMEOUT_S             = 90
RETRIES               = 2
MIN_DEMO_BYTES        = 1024    # below this = error page, not a demo

COLOR_CODE_RE = re.compile(r"\^[0-9]")
INVALID_FN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ── state ─────────────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict:
    if not SEEN_FILE.is_file():
        return {"seen": {}, "runs": []}
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        bk = SEEN_FILE.with_name(f"seen_matches.corrupt-{int(time.time())}.json")
        SEEN_FILE.rename(bk)
        print(f"[warn] state corrupt, backed up to {bk.name}")
        return {"seen": {}, "runs": []}


def save_state(state: dict) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(SEEN_FILE)


# ── time argument parsing (--since / --until) ────────────────────────────────
RELATIVE_RE = re.compile(r"^(\d+)\s*([hdwm])$", re.IGNORECASE)
_RELATIVE_UNITS = {"h": 3_600, "d": 86_400, "w": 604_800, "m": 2_592_000}

def parse_time_arg(s: str, name: str) -> int:
    """Convert a --since / --until string to a unix-ms timestamp.

    Accepts:
      'now' / '0' / '0d'                       -> the current instant
      '24h', '7d', '4w', '1m'                  -> now - that interval
      '2026-04-01', '2026-04-01T18:00:00'      -> that UTC instant
    """
    if s is None:
        raise SystemExit(f"{name}: missing value")
    s = s.strip()
    if s.lower() in ("now", "0", "0d", "0h", "0w", "0m"):
        return int(time.time() * 1000)
    m = RELATIVE_RE.fullmatch(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        seconds = n * _RELATIVE_UNITS[unit]
        return int((time.time() - seconds) * 1000)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise SystemExit(f"{name}: can't parse '{s}'.  Use Nh/Nd/Nw/Nm, YYYY-MM-DD, or 'now'.")


def fmt_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── name handling ─────────────────────────────────────────────────────────────
def strip_color(s: str) -> str:
    return COLOR_CODE_RE.sub("", s or "")


def sanitize(s: str, fallback: str = "unknown") -> str:
    s = INVALID_FN_RE.sub("_", s).strip("._ ")
    return s or fallback


def filename_for(match: dict, demo: dict) -> str:
    """<date>__<map>__<id8>__c<NN>_<player>.dm_26 — self-describing, sorts naturally."""
    full_id = match.get("_id") or "unknown"
    id8 = full_id[:8] if len(full_id) >= 8 else full_id

    tc = match.get("time_created")
    ts = tc.get("$date", 0) if isinstance(tc, dict) else 0
    date_str = (datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
                if ts else "unknown-date")

    mapname = match.get("mapname", "unknown")
    # mp/ctf4 -> mpctf4 ; strip path separators before generic sanitize
    mapname = mapname.replace("/", "").replace("\\", "")
    mapname = sanitize(mapname, fallback="unknown-map")

    player = sanitize(strip_color(demo.get("name", "")))
    cid = demo.get("client_id")
    cid_str = f"c{int(cid):02d}" if cid is not None else "cXX"

    return f"{date_str}__{mapname}__{id8}__{cid_str}_{player}.dm_26"


def is_pure_spectator(client_id, scores_list) -> bool:
    """True iff this client only ever appears in specplayers (or nowhere) across
    every score snapshot.  Players who joined late / left early still count as
    players because they appear in red/blue at least once.

    Returns False if we can't tell (no client_id, no scores) — keep on the safe side.
    """
    if client_id is None or not scores_list:
        return False
    for snap in scores_list:
        if not isinstance(snap, dict):
            continue
        for team_key in ("redplayers", "blueplayers", "freeplayers"):
            players = snap.get(team_key) or []
            for p in players:
                if isinstance(p, dict) and p.get("client") == client_id:
                    return False  # appeared as a player at least once
    return True


# ── networking ────────────────────────────────────────────────────────────────
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(REQUEST_HEADERS)
    return s


def search_page(s: requests.Session, offset: int, limit: int) -> dict:
    params = {
        "rpc":    "searchmatches",
        "match":  "true",
        "offset": offset,
        "limit":  limit,
    }
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            r = s.get(SEARCH_URL, params=params, timeout=TIMEOUT_S)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"searchmatches failed after retries: {last_err}")


def download_one(s: requests.Session, demo_id: str, dst: Path, dry: bool) -> tuple[bool, str]:
    if dst.exists() and dst.stat().st_size > 0:
        return True, f"already on disk ({dst.stat().st_size:,}B)"
    if dry:
        return True, "[dry] would download"
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".dm_26.part")
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            r = s.get(DOWNLOAD_URL, params={"demo": demo_id}, stream=True,
                      timeout=TIMEOUT_S, allow_redirects=True)
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
            size = tmp.stat().st_size
            if size < MIN_DEMO_BYTES:
                tmp.unlink(missing_ok=True)
                return False, f"got {size}B — likely error or auth-required page"
            tmp.replace(dst)
            return True, f"{size:,}B"
        except Exception as e:
            last_err = str(e)
            tmp.unlink(missing_ok=True)
            if attempt < RETRIES:
                print(f"      retry ({attempt+1}/{RETRIES}): {last_err[:80]}")
                time.sleep(1.5 * (attempt + 1))
    return False, f"failed after retries: {last_err}"


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--since", default=SINCE_DEFAULT,
                    help=f"window LOWER bound (older edge): Nh/Nd/Nw/Nm, YYYY-MM-DD, "
                         f"or 'now' (default {SINCE_DEFAULT}).")
    ap.add_argument("--until", default=UNTIL_DEFAULT,
                    help=f"window UPPER bound (newer edge): same formats as --since "
                         f"(default {UNTIL_DEFAULT} = present).")
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE_DEFAULT,
                    help=f"matches per page request (default {PAGE_SIZE_DEFAULT})")
    ap.add_argument("--max-pages", type=int, default=500,
                    help="hard cap on pages walked per run (default 500)")
    ap.add_argument("--seen-streak", type=int, default=SEEN_STREAK_DEFAULT,
                    help=f"stop after N consecutive already-seen matches (default {SEEN_STREAK_DEFAULT})")
    ap.add_argument("--max-matches", type=int, default=0,
                    help="hard cap on NEW matches downloaded this run (0 = no cap)")
    ap.add_argument("--include-spectators", action="store_true",
                    help="also download demos from pure spectators (default: skip them — "
                         "their ownfrags is always empty so scanner.py drops them anyway)")
    ap.add_argument("--dry-run", action="store_true",
                    help="scrape and report but don't download or update state")
    ap.add_argument("--reset", action="store_true",
                    help="forget seen_matches.json before starting")
    args = ap.parse_args()

    # ── time window + HARD SAFETY ─────────────────────────────────────────────
    since_ms = parse_time_arg(args.since, "--since")
    until_ms = parse_time_arg(args.until, "--until")
    if until_ms <= since_ms:
        sys.exit(f"\nERROR: --until ({fmt_utc(until_ms)}) must be NEWER than "
                 f"--since ({fmt_utc(since_ms)}).")
    window_ms = until_ms - since_ms
    if window_ms > MAX_WINDOW_MS:
        days = window_ms / 86_400_000
        sys.exit(
            f"\nERROR: requested window {fmt_utc(since_ms)} -> {fmt_utc(until_ms)} "
            f"is {days:.1f} days, max allowed per run is {MAX_WINDOW_DAYS} days.\n"
            f"Run repeatedly with explicit windows to backfill, e.g.:\n"
            f"   python fetch_jactf_demos.py --since 30d --until 0d   # last 30 days\n"
            f"   python fetch_jactf_demos.py --since 60d --until 30d  # days 30-60\n"
            f"   python fetch_jactf_demos.py --since 90d --until 60d  # days 60-90\n"
        )
    print(f"time window: {fmt_utc(since_ms)} -> {fmt_utc(until_ms)}  "
          f"({window_ms / 86_400_000:.2f} days)")

    if args.reset and SEEN_FILE.is_file():
        SEEN_FILE.unlink()
        print("[reset] cleared seen_matches.json")

    state = load_state()
    seen = state.setdefault("seen", {})
    state.setdefault("runs", [])
    LANDING.mkdir(parents=True, exist_ok=True)
    print(f"loaded state: {len(seen)} match(es) previously downloaded")

    s = make_session()

    # ── scrape phase: walk pages newest-first, collect new matches ──
    new_matches: list[dict] = []
    seen_streak = 0
    total = None
    pages_walked = 0

    print()
    for page in range(args.max_pages):
        offset = page * args.page_size
        if total is not None and offset >= total:
            print(f"  reached end of archive (offset {offset} >= total {total})")
            break
        try:
            resp = search_page(s, offset, args.page_size)
        except Exception as e:
            print(f"  [error] page {page+1} fetch failed: {e}")
            break
        if total is None:
            total = resp.get("total", 0)
            print(f"  archive: {total} total match(es)")
        results = resp.get("result", [])
        pages_walked += 1
        if not results:
            print(f"  page {page+1}: empty — done.")
            break

        new_on_page = 0
        too_old_on_page = 0
        too_new_on_page = 0
        for m in results:
            mid = m.get("_id")
            if not mid:
                continue
            tc = m.get("time_created")
            ts = tc.get("$date", 0) if isinstance(tc, dict) else 0
            if ts and ts > until_ms:
                too_new_on_page += 1
                continue
            if ts and ts < since_ms:
                too_old_on_page += 1
                continue
            if mid in seen:
                seen_streak += 1
                continue
            seen_streak = 0
            new_matches.append(m)
            new_on_page += 1

        print(f"  page {page+1:>3} (offset={offset:>5}): {len(results)} match(es), "
              f"{new_on_page} new, {too_new_on_page} too-new, {too_old_on_page} too-old, "
              f"seen-streak={seen_streak}")

        # results come newest-first.  Once a full page is older than the window,
        # nothing further can match — stop.  (too-new pages still need to be paged
        # past, until we reach the window.)
        if too_old_on_page == len(results):
            print(f"  every match on this page is older than --since — stopping")
            break
        if seen_streak >= args.seen_streak:
            print(f"  hit seen-streak {seen_streak} >= {args.seen_streak} — stopping")
            break
        if args.max_matches and len(new_matches) >= args.max_matches:
            new_matches = new_matches[: args.max_matches]
            print(f"  hit --max-matches {args.max_matches} — stopping")
            break

    print(f"\nfound {len(new_matches)} new match(es) over {pages_walked} page(s)")
    if not new_matches:
        state["runs"].append({"started": now_iso(), "found": 0, "new": 0})
        if not args.dry_run:
            save_state(state)
        return

    # ── download phase ──
    n_match_ok = n_demo_ok = n_demo_fail = n_spec_skipped = 0
    run_started = now_iso()

    for i, m in enumerate(new_matches, 1):
        mid     = m["_id"]
        demos   = m.get("demos", [])
        scores  = m.get("scores", [])
        tc      = m.get("time_created")
        ts      = tc.get("$date", 0) if isinstance(tc, dict) else 0
        ts_str  = (datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
                   if ts else "?")
        mapname = m.get("mapname", "?")
        print(f"\n[{i}/{len(new_matches)}] {mid}  {mapname}  ({ts_str} UTC)  {len(demos)} demo(s)")

        ok_players, failed_players, skipped_specs = [], [], []
        for d in demos:
            demo_id = d.get("id")
            if not demo_id:
                continue
            disp = sanitize(strip_color(d.get("name", "?")))
            cid  = d.get("client_id")
            cid_str = f"{cid:>2}" if isinstance(cid, int) else " ?"

            if not args.include_spectators and is_pure_spectator(cid, scores):
                print(f"    [SPEC] c{cid_str}  {disp:<30}  -> skipping (pure spectator)")
                skipped_specs.append({"name": disp, "client_id": cid})
                n_spec_skipped += 1
                continue

            fname = filename_for(m, d)
            dst   = LANDING / fname
            ok, msg = download_one(s, demo_id, dst, args.dry_run)
            tag   = "OK  " if ok else "FAIL"
            print(f"    [{tag}] c{cid_str}  {disp:<30}  ->  {fname}   ({msg})")
            if ok:
                ok_players.append({"name": disp, "client_id": cid})
                n_demo_ok += 1
            else:
                failed_players.append({"name": disp, "demo_id": demo_id, "error": msg})
                n_demo_fail += 1

        if (ok_players or skipped_specs) and not args.dry_run:
            seen[mid] = {
                "downloaded_at": now_iso(),
                "time_created":  ts,
                "mapname":       mapname,
                "demo_count":    len(ok_players),
                "players":       ok_players,
                "failed":        failed_players,
                "skipped_specs": skipped_specs,
            }
            save_state(state)
            if ok_players:
                n_match_ok += 1

    state["runs"].append({
        "started":      run_started,
        "finished":     now_iso(),
        "new":          len(new_matches),
        "matches_ok":   n_match_ok,
        "demos_ok":     n_demo_ok,
        "demos_fail":   n_demo_fail,
        "specs_skipped": n_spec_skipped,
    })
    if not args.dry_run:
        save_state(state)

    print("\n=== fetch complete ===")
    print(f"  new matches:        {len(new_matches)}")
    print(f"  matches downloaded: {n_match_ok}")
    print(f"  demos OK:           {n_demo_ok}")
    print(f"  demos failed:       {n_demo_fail}")
    print(f"  spectators skipped: {n_spec_skipped}")
    if args.dry_run:
        print("(dry-run — nothing was saved.)")
    else:
        print(f"\nfiles landed in: {LANDING}")


if __name__ == "__main__":
    main()
