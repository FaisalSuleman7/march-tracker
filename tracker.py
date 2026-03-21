#!/usr/bin/env python3
"""
================================================================
MARCH MANIA 2026 — RANK TRACKER  v5
================================================================
FEATURES:
  - Fetches leaderboard via Kaggle API (KGAT token)
  - Built-in web server so dashboard.html works in browser
  - Auto-checks every 2 hours (configurable)
  - Records exact timestamp of every check
  - Detects and alerts on rank changes
  - Saves full history to tracker_history.json

RUN (does everything — tracking + web server):
  py tracker.py

CUSTOM INTERVAL (minutes):
  py tracker.py --interval 60

VIEW HISTORY ONLY:
  py tracker.py --history

THEN OPEN BROWSER:
  http://localhost:8765
================================================================
"""

import json
import time
import argparse
import os
import sys
import csv
import zipfile
import io
import threading
import http.server
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("Run: py -m pip install requests")
    sys.exit(1)

# ── Config ─────────────────────────────────────────────────────
COMPETITION    = 'march-machine-learning-mania-2026'
YOUR_USERNAME  = 'hmfaisal'       # display name on leaderboard
DEFAULT_INTERVAL_MINUTES = 120    # check every 2 hours
WEB_PORT       = 8765
HISTORY_FILE   = Path('tracker_history.json')
DASHBOARD_FILE = Path('dashboard.html')

# Local knowledge about each submission — maps filename to version info
# Score and description will be fetched live from Kaggle API
SUBMISSION_META = {
    'submission_kaggle7.csv' : {'version': 'v7',  'local_brier': 0.04595, 'notes': 'Platt Scaling — BEST real score'},
    'submission_kaggle12.csv': {'version': 'v12', 'local_brier': None,    'notes': 'LuckAdj + Pressure'},
    'submission_kaggle8.csv' : {'version': 'v8',  'local_brier': 0.04656, 'notes': 'Platt Logit'},
    'submission_kaggle2.csv' : {'version': 'v2?', 'local_brier': None,    'notes': 'Error submission'},
    'submission_kaggle1.csv' : {'version': 'v1?', 'local_brier': None,    'notes': 'Error submission'},
    'submission (1).csv'     : {'version': '?',   'local_brier': None,    'notes': 'Error submission'},
    'submission.csv'         : {'version': '?',   'local_brier': None,    'notes': 'First submission'},
}


# ── Credentials ─────────────────────────────────────────────────

def load_creds():
    local = Path('kaggle.json')
    if not local.exists():
        print("❌ kaggle.json not found in this folder.")
        sys.exit(1)
    with open(local) as f:
        creds = json.load(f)
    username = creds.get('username', '').strip()
    key      = creds.get('key', '').strip()
    if not username or not key:
        print("❌ kaggle.json missing username or key.")
        sys.exit(1)
    return username, key


# ── Leaderboard fetch ───────────────────────────────────────────

def fetch_leaderboard(username, key):
    url = f'https://www.kaggle.com/api/v1/competitions/{COMPETITION}/leaderboard/download'
    try:
        resp = requests.get(
            url,
            headers={'Authorization': f'Bearer {key}'},
            timeout=30,
            allow_redirects=True
        )
        if resp.status_code == 200:
            ct = resp.headers.get('Content-Type', '')
            if 'zip' in ct or resp.content[:2] == b'PK':
                return parse_zip(resp.content)
            if 'csv' in ct or 'text' in ct:
                return parse_csv_text(resp.text)
            if 'json' in ct:
                return parse_json(resp.json())
        elif resp.status_code == 401:
            print("   ❌ Auth failed (401) — check kaggle.json key")
        elif resp.status_code == 403:
            print("   ❌ Forbidden (403) — make sure you joined the competition")
        else:
            print(f"   ❌ HTTP {resp.status_code}")
    except requests.Timeout:
        print("   ❌ Request timed out")
    except Exception as e:
        print(f"   ❌ Error: {e}")
    return None


def parse_zip(content):
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        for name in z.namelist():
            if name.endswith('.csv'):
                with z.open(name) as f:
                    return parse_csv_text(f.read().decode('utf-8'))
    except Exception as e:
        print(f"   ZIP error: {e}")
    return None


def parse_csv_text(text):
    entries = []
    try:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            name = (row.get('TeamName') or row.get('teamName') or
                    row.get('team_name') or row.get('UserName') or
                    row.get('username') or row.get('name') or '').strip()
            raw_score = (row.get('Score') or row.get('score') or
                         row.get('PublicScore') or row.get('public_score'))
            try:
                score = float(raw_score) if raw_score else None
            except (ValueError, TypeError):
                score = None
            if name:
                entries.append({'name': name, 'score': score})
    except Exception as e:
        print(f"   CSV error: {e}")
    return entries if entries else None


def parse_json(data):
    entries = []
    items = data if isinstance(data, list) else data.get('submissions', data.get('results', []))
    for item in items:
        if isinstance(item, dict):
            name = (item.get('teamName') or item.get('team_name') or
                    item.get('userName') or item.get('name') or '').strip()
            try:
                score = float(item.get('score') or item.get('publicScore') or 0) or None
            except (ValueError, TypeError):
                score = None
            if name:
                entries.append({'name': name, 'score': score})
    return entries if entries else None


# ── Submissions fetch ───────────────────────────────────────────

def fetch_submissions(username, key):
    """
    Fetch your own submissions from Kaggle API.
    Returns list of dicts with real filename, description, score, status, selected.
    """
    url = f'https://www.kaggle.com/api/v1/competitions/submissions/list/{COMPETITION}'
    try:
        resp = requests.get(
            url,
            headers={'Authorization': f'Bearer {key}'},
            params={'pageSize': 100},
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            items = data if isinstance(data, list) else data.get('submissions', data.get('results', []))
            subs = []
            for item in items:
                # Extract all useful fields — Kaggle API field names vary by version
                fname   = (item.get('fileName')    or item.get('file_name')    or
                           item.get('description') or item.get('name')         or 'unknown')
                desc    = (item.get('description') or item.get('publicDescription') or '')
                score   = (item.get('publicScore') or item.get('public_score')  or
                           item.get('score')       or None)
                status  = (item.get('status')      or item.get('submissionStatus') or '')
                date    = (item.get('date')         or item.get('submittedAt')  or
                           item.get('submitted_at') or '')
                selected= bool(item.get('selected') or item.get('isSelected')  or False)

                try:
                    score = float(score) if score is not None else None
                except (ValueError, TypeError):
                    score = None

                # Match to local meta by filename
                meta = SUBMISSION_META.get(fname, {})
                subs.append({
                    'file'       : fname,
                    'description': desc,
                    'score'      : score,
                    'status'     : str(status).lower(),
                    'date'       : str(date)[:16].replace('T', ' '),
                    'selected'   : selected,
                    'version'    : meta.get('version', '?'),
                    'local_brier': meta.get('local_brier'),
                    'notes'      : meta.get('notes', ''),
                })
            # Sort: selected first, then by score ascending (best first)
            subs.sort(key=lambda x: (not x['selected'], x['score'] or 99))
            return subs
        else:
            print(f"   Submissions API: HTTP {resp.status_code}")
    except Exception as e:
        print(f"   Submissions fetch error: {e}")
    return []


# ── Rank finding ────────────────────────────────────────────────

def find_rank(entries, username=YOUR_USERNAME):
    total  = len(entries)
    scored = sorted(
        [e for e in entries if e['score'] is not None],
        key=lambda x: x['score']
    )
    for i, e in enumerate(scored, 1):
        if username.lower() in e['name'].lower():
            return i, e['score'], total, scored
    return None, None, total, scored


# ── History ─────────────────────────────────────────────────────

def load_history():
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {
        'meta': {
            'username'   : YOUR_USERNAME,
            'competition': COMPETITION,
            'created'    : datetime.now(timezone.utc).isoformat(),
        },
        'submissions': [],
        'snapshots'  : [],
        'best_rank'  : None,
        'best_score' : None,
    }


def save_history(history, submissions=None):
    if submissions is not None:
        history['submissions'] = submissions
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)


def record_snapshot(history, rank, score, total, top_score, top_name, entries_count):
    now  = datetime.now(timezone.utc)
    local_now = datetime.now()  # local time
    prev = history['snapshots'][-1] if history['snapshots'] else {}

    rank_change  = (rank  - prev['rank'])  if rank  and prev.get('rank')  else None
    score_change = round(score - prev['score'], 6) if score and prev.get('score') else None
    percentile   = round((1 - rank / total) * 100, 2) if rank and total else None
    gap_to_top   = round(score - top_score, 6) if score and top_score else None

    snap = {
        'timestamp'         : now.isoformat(),
        'timestamp_readable': now.strftime('%Y-%m-%d %H:%M UTC'),
        'timestamp_local'   : local_now.strftime('%Y-%m-%d %H:%M'),
        'rank'              : rank,
        'total_teams'       : total,
        'scored_teams'      : entries_count,
        'percentile'        : percentile,
        'score'             : score,
        'rank_change'       : rank_change,
        'score_change'      : score_change,
        'top_score'         : top_score,
        'top_team'          : top_name,
        'gap_to_top'        : gap_to_top,
    }
    history['snapshots'].append(snap)

    if rank  and (history['best_rank']  is None or rank  < history['best_rank']):
        history['best_rank']  = rank
    if score and (history['best_score'] is None or score < history['best_score']):
        history['best_score'] = score

    return snap


# ── Terminal display ────────────────────────────────────────────

def print_snap(snap, history, show_history=True):
    rank  = snap.get('rank')
    total = snap.get('total_teams')
    score = snap.get('score')
    pct   = snap.get('percentile')
    rc    = snap.get('rank_change')
    sc    = snap.get('score_change')
    top   = snap.get('top_score')
    gap   = snap.get('gap_to_top')
    ts    = snap.get('timestamp_local', snap.get('timestamp_readable', snap.get('timestamp','')[:16]))

    W = 60
    print()
    print("═" * W)
    print("  🏀  MARCH MANIA 2026 — RANK TRACKER")
    print("═" * W)
    print(f"  📅 {ts}")
    print(f"  {'─' * (W-4)}")

    if rank:
        ch = (f"  ↑ {abs(rc)} places 🎉" if rc and rc < 0 else
              f"  ↓ {rc} places"          if rc and rc > 0 else
              "  → no change"             if rc == 0 else "")
        print(f"  🏆 Rank        : #{rank} / {total}{ch}")
        print(f"  📊 Percentile  : top {100 - pct:.1f}% of all teams")
        sc_str = f"  ({sc:+.6f})" if sc is not None else ""
        print(f"  🎯 Score       : {score:.6f}{sc_str}")
        if top:
            print(f"  👑 Leader      : {top:.6f}  (you are {gap:+.6f} behind)")
    else:
        print(f"  ❓ '{YOUR_USERNAME}' not found  |  Total teams: {total}")

    print(f"  {'─' * (W-4)}")
    br = history.get('best_rank')
    bs = history.get('best_score')
    print(f"  ★ Best rank    : #{br}" if br else "  ★ Best rank    : —")
    print(f"  ★ Best score   : {bs:.6f}" if bs else "  ★ Best score   : —")
    print(f"  {'─' * (W-4)}")
    print(f"  ✓ Active submission: v7  Kaggle={0.19841}  LocalBrier={0.04595}")

    snaps = history.get('snapshots', [])
    if show_history and len(snaps) > 1:
        print(f"\n  📈 History ({len(snaps)} snapshots):")
        print(f"  {'Time':<22} {'Rank':>7} {'Score':>10} {'Δ Rank':>8}")
        print(f"  {'─' * 52}")
        for s in snaps[-12:]:
            t   = s.get('timestamp_local', s.get('timestamp_readable', s['timestamp'][:16]))
            r   = f"#{s['rank']}" if s.get('rank') else '—'
            sc2 = f"{s['score']:.5f}" if s.get('score') else '—'
            c   = s.get('rank_change')
            ch2 = (f"↑{abs(c)}" if c and c < 0 else
                   f"↓{c}"      if c and c > 0 else
                   "→"          if c == 0 else '—')
            print(f"  {t:<22} {r:>7} {sc2:>10} {ch2:>8}")

    print()
    print(f"  💾 {HISTORY_FILE}  |  🌐 http://localhost:{WEB_PORT}")
    print("═" * W)

    if rc is not None and abs(rc) >= 100:
        arrow = "UP ↑↑" if rc < 0 else "DOWN ↓↓"
        print(f"\n  🚨 BIG MOVE: {arrow} {abs(rc)} places!\n")


# ── Single check ────────────────────────────────────────────────

def run_check(verbose=True):
    username, key = load_creds()
    if verbose:
        print(f"✅ Credentials: username={username}  key={key[:8]}...")
        print(f"📡 Fetching leaderboard ({datetime.now().strftime('%H:%M:%S')})...")

    entries = fetch_leaderboard(username, key)

    if not entries:
        print("❌ Could not fetch leaderboard.")
        print(f"\n   Manual fallback:")
        print(f"   1. Download CSV from: https://www.kaggle.com/competitions/{COMPETITION}/leaderboard")
        print(f"   2. Save as 'leaderboard.csv' in this folder")
        print(f"   3. Run: py tracker.py --from-file leaderboard.csv")
        return None

    if verbose:
        print(f"   ✅ Got {len(entries)} entries")

    # Fetch your submissions with real names/scores from Kaggle
    if verbose:
        print(f"📋 Fetching your submissions...")
    submissions = fetch_submissions(username, key)
    if verbose:
        if submissions:
            print(f"   ✅ Got {len(submissions)} submissions")
            for s in submissions:
                sel = ' ✓ SELECTED' if s['selected'] else ''
                sc  = f"{s['score']:.5f}" if s['score'] else 'N/A'
                print(f"   {s['file']:<35} score={sc}  status={s['status']}{sel}")
        else:
            print(f"   ⚠️  Could not fetch submissions (leaderboard still works)")

    rank, score, total, scored = find_rank(entries)

    if rank is None and verbose:
        print(f"\n⚠️  '{YOUR_USERNAME}' not found. Sample names:")
        for e in scored[:5]:
            print(f"   '{e['name']}'  {e['score']:.5f}")
        print(f"\n   Edit YOUR_USERNAME in tracker.py if your display name differs")

    top       = scored[0] if scored else None
    top_score = top['score'] if top else None
    top_name  = top['name']  if top else '?'

    history = load_history()
    snap    = record_snapshot(history, rank, score, total,
                              top_score, top_name, len(scored))
    save_history(history, submissions if submissions else history.get('submissions'))

    if verbose:
        print_snap(snap, history)

    return snap


# ── Watch mode ──────────────────────────────────────────────────

def watch_mode(interval_minutes):
    print(f"\n🔄 WATCH MODE — checking every {interval_minutes} minutes")
    print(f"   Dashboard → http://localhost:{WEB_PORT}")
    print(f"   Press Ctrl+C to stop\n")

    count = 0
    while True:
        count += 1
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"\n{'─'*50}")
        print(f"[Check #{count}]  {now}")
        print(f"{'─'*50}")
        try:
            run_check(verbose=True)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"⚠️  Error during check: {e}")

        next_time = datetime.fromtimestamp(
            time.time() + interval_minutes * 60
        ).strftime('%H:%M:%S')
        print(f"\n⏳ Next check at {next_time}  ({interval_minutes} min)  |  Ctrl+C to stop")

        try:
            time.sleep(interval_minutes * 60)
        except KeyboardInterrupt:
            raise


# ── Web server ──────────────────────────────────────────────────

class TrackerHandler(http.server.SimpleHTTPRequestHandler):
    """Serve dashboard.html and tracker_history.json from current directory."""

    def log_message(self, format, *args):
        pass  # suppress noisy server logs

    def do_GET(self):
        # Serve root as dashboard.html
        if self.path == '/' or self.path == '':
            self.path = '/dashboard.html'
        super().do_GET()

    def end_headers(self):
        # Allow JS to read local files
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()


def start_web_server():
    """Start web server in background thread."""
    os.chdir(Path(__file__).parent)
    server = http.server.HTTPServer(('localhost', WEB_PORT), TrackerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"🌐 Dashboard running at http://localhost:{WEB_PORT}")
    return server


# ── From file ───────────────────────────────────────────────────

def run_from_file(filepath):
    print(f"📂 Loading from {filepath}...")
    with open(filepath, newline='', encoding='utf-8') as f:
        text = f.read()
    entries = parse_csv_text(text)
    if not entries:
        print("❌ Could not parse CSV.")
        return

    print(f"   ✅ Got {len(entries)} entries")
    rank, score, total, scored = find_rank(entries)
    top       = scored[0] if scored else None
    top_score = top['score'] if top else None
    top_name  = top['name']  if top else '?'

    history = load_history()
    snap    = record_snapshot(history, rank, score, total,
                              top_score, top_name, len(scored))
    save_history(history)
    print_snap(snap, history)


# ── History print ───────────────────────────────────────────────

def print_history():
    h     = load_history()
    snaps = h.get('snapshots', [])
    print(f"\n📊 Full history — {len(snaps)} snapshots  |  "
          f"Best rank: #{h.get('best_rank','—')}  Best score: {h.get('best_score','—')}")
    if not snaps:
        print("   No data yet. Run: py tracker.py")
        return
    print(f"\n{'Time':<24} {'Rank':>8} {'Score':>10} {'Δ Rank':>8} {'Leader':>10}")
    print("─" * 65)
    for s in snaps:
        t   = s.get('timestamp_readable', s['timestamp'][:16])
        r   = f"#{s['rank']}" if s.get('rank') else '—'
        sc  = f"{s['score']:.5f}" if s.get('score') else '—'
        c   = s.get('rank_change')
        ch  = (f"↑{abs(c)}" if c and c < 0 else
               f"↓{c}"      if c and c > 0 else "—")
        top = f"{s['top_score']:.5f}" if s.get('top_score') else '—'
        print(f"{t:<24} {r:>8} {sc:>10} {ch:>8} {top:>10}")


# ── Entry point ─────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='March Mania 2026 Rank Tracker')
    parser.add_argument('--interval',  type=int, default=DEFAULT_INTERVAL_MINUTES,
                        help=f'Minutes between checks (default: {DEFAULT_INTERVAL_MINUTES})')
    parser.add_argument('--history',   action='store_true', help='Print history and exit')
    parser.add_argument('--no-server', action='store_true', help='Skip web server')
    parser.add_argument('--no-browser',action='store_true', help='Do not open browser')
    parser.add_argument('--from-file', type=str, metavar='FILE',
                        help='Load leaderboard from manually downloaded CSV')
    args = parser.parse_args()

    if args.history:
        print_history()
        return

    if args.from_file:
        run_from_file(args.from_file)
        return

    # Start web server
    if not args.no_server and DASHBOARD_FILE.exists():
        start_web_server()
        if not args.no_browser:
            time.sleep(0.5)
            webbrowser.open(f'http://localhost:{WEB_PORT}')

    # Watch mode handles the first check itself
    try:
        watch_mode(args.interval)
    except KeyboardInterrupt:
        print("\n\nStopped. History saved to tracker_history.json")


if __name__ == '__main__':
    main()
