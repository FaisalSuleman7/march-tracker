#!/usr/bin/env python3
"""
================================================================
MARCH MANIA 2026 -- RANK TRACKER  v6
================================================================
Works both locally AND on GitHub Actions.

LOCAL:
  py tracker.py --interval 10

GITHUB ACTIONS:
  python tracker.py --no-server --no-browser
  (credentials come from KAGGLE_USERNAME / KAGGLE_KEY secrets)

DASHBOARD:
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
    print("Run: pip install requests")
    sys.exit(1)

# ── Config ─────────────────────────────────────────────────────
COMPETITION              = 'march-machine-learning-mania-2026'
YOUR_USERNAME            = 'hmfaisal'
DEFAULT_INTERVAL_MINUTES = 120
WEB_PORT                 = 8765
HISTORY_FILE             = Path('tracker_history.json')
DASHBOARD_FILE           = Path('dashboard.html')

SUBMISSION_META = {
    'submission_kaggle7.csv' : {'version': 'v7',  'local_brier': 0.04595, 'notes': 'Platt Scaling -- BEST real score'},
    'submission_kaggle12.csv': {'version': 'v12', 'local_brier': None,    'notes': 'LuckAdj + Pressure'},
    'submission_kaggle8.csv' : {'version': 'v8',  'local_brier': 0.04656, 'notes': 'Platt Logit'},
    'submission_kaggle2.csv' : {'version': 'v2?', 'local_brier': None,    'notes': 'Error submission'},
    'submission_kaggle1.csv' : {'version': 'v1?', 'local_brier': None,    'notes': 'Error submission'},
    'submission (1).csv'     : {'version': '?',   'local_brier': None,    'notes': 'Error submission'},
    'submission.csv'         : {'version': '?',   'local_brier': None,    'notes': 'First submission'},
}


# ── Credentials ─────────────────────────────────────────────────
# Reads environment variables first (GitHub Actions),
# then falls back to kaggle.json file (local PC).

def load_creds():
    # GitHub Actions: credentials come from repository secrets
    env_user = os.environ.get('KAGGLE_USERNAME', '').strip()
    env_key  = os.environ.get('KAGGLE_KEY', '').strip()
    if env_user and env_key:
        print(f"✅ Credentials from environment -- username: {env_user}")
        return env_user, env_key

    # Local PC: read from kaggle.json file
    local = Path('kaggle.json')
    if not local.exists():
        print("❌ No credentials found.")
        print("   Local PC  : create kaggle.json with {\"username\":\"...\",\"key\":\"...\"}")
        print("   GitHub    : add KAGGLE_USERNAME and KAGGLE_KEY as repository secrets")
        sys.exit(1)
    with open(local) as f:
        creds = json.load(f)
    username = creds.get('username', '').strip()
    key      = creds.get('key', '').strip()
    if not username or not key:
        print("❌ kaggle.json missing username or key.")
        sys.exit(1)
    print(f"✅ Credentials from kaggle.json -- username: {username}")
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
            print("   ❌ Auth failed (401) -- check credentials")
        elif resp.status_code == 403:
            print("   ❌ Forbidden (403) -- make sure you joined the competition")
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
    url = f'https://www.kaggle.com/api/v1/competitions/submissions/list/{COMPETITION}'
    try:
        resp = requests.get(
            url,
            headers={'Authorization': f'Bearer {key}'},
            params={'pageSize': 100},
            timeout=30
        )
        if resp.status_code == 200:
            data  = resp.json()
            items = data if isinstance(data, list) else data.get('submissions', data.get('results', []))
            subs  = []
            for item in items:
                fname    = (item.get('fileName')    or item.get('file_name')    or
                            item.get('description') or item.get('name')         or 'unknown')
                desc     = (item.get('description') or item.get('publicDescription') or '')
                score    = (item.get('publicScore') or item.get('public_score')  or
                            item.get('score')       or None)
                status   = (item.get('status')      or item.get('submissionStatus') or '')
                date     = (item.get('date')         or item.get('submittedAt')  or
                            item.get('submitted_at') or '')
                selected = bool(item.get('selected') or item.get('isSelected') or False)
                try:
                    score = float(score) if score is not None else None
                except (ValueError, TypeError):
                    score = None
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
        'meta'       : {'username': YOUR_USERNAME, 'competition': COMPETITION,
                        'created': datetime.now(timezone.utc).isoformat()},
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


def fetch_kaggle_games_scored(username, key):
    """
    Fetch Kaggle's official games scored count.
    Tries multiple sources to find:
    'The Leaderboard is current through 62 games (35 NCAAM & 27 NCAAW)'
    Returns (total, men, women) tuple or (estimate, None, None) fallback.
    """
    import re

    def parse_games_text(text):
        """Extract games count from any text containing the Kaggle leaderboard string."""
        # Primary pattern: "current through 62 games (35 NCAAM & 27 NCAAW)"
        patterns = [
            r'current\s+through\s+(\d+)\s+games?\s*\((\d+)\s+NCAAM\D{1,5}(\d+)\s+NCAAW\)',
            r'through\s+(\d+)\s+games?\s*\((\d+)\s+NCAAM\D{1,5}(\d+)\s+NCAAW\)',
            r'(\d+)\s+games?\s*\((\d+)\s+NCAAM\D{1,5}(\d+)\s+NCAAW\)',
            r'current\s+through\s+(\d+)\s+games?',
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                total = int(m.group(1))
                men   = int(m.group(2)) if len(m.groups()) >= 2 else None
                women = int(m.group(3)) if len(m.groups()) >= 3 else None
                return total, men, women
        return None, None, None

    # --- Attempt 1: leaderboard/view API endpoint ---
    try:
        url = f'https://www.kaggle.com/api/v1/competitions/{COMPETITION}/leaderboard/view'
        resp = requests.get(url, headers={'Authorization': f'Bearer {key}'}, timeout=15)
        if resp.status_code == 200:
            # Search raw response text — more reliable than parsing JSON first
            raw = resp.text
            total, men, women = parse_games_text(raw)
            if total:
                print(f"   📊 Kaggle official [view API]: {total} games ({men} NCAAM + {women} NCAAW)")
                return total
            # Also try parsed JSON — walk all string values
            try:
                data = resp.json()
                full_str = str(data)
                total, men, women = parse_games_text(full_str)
                if total:
                    print(f"   📊 Kaggle official [view JSON]: {total} games ({men} NCAAM + {women} NCAAW)")
                    return total
            except Exception:
                pass
            print(f"   ⚠️  view API returned 200 but no games count found. Preview: {raw[:300]}")
        else:
            print(f"   ⚠️  view API returned HTTP {resp.status_code}")
    except Exception as e:
        print(f"   ⚠️  view API error: {e}")

    # --- Attempt 2: competitions API for subtitle/description ---
    try:
        url2 = f'https://www.kaggle.com/api/v1/competitions/{COMPETITION}'
        resp2 = requests.get(url2, headers={'Authorization': f'Bearer {key}'}, timeout=15)
        if resp2.status_code == 200:
            total, men, women = parse_games_text(resp2.text)
            if total:
                print(f"   📊 Kaggle official [competitions API]: {total} games ({men} NCAAM + {women} NCAAW)")
                return total
    except Exception as e:
        print(f"   ⚠️  competitions API error: {e}")

    # --- Fallback: cumulative schedule estimate ---
    from datetime import date
    today = date.today()
    schedule = [
        (date(2026, 3, 19),  8),
        (date(2026, 3, 20), 24),
        (date(2026, 3, 21), 48),
        (date(2026, 3, 22), 72),
        (date(2026, 3, 23), 96),
        (date(2026, 3, 27), 104),
        (date(2026, 3, 28), 112),
        (date(2026, 3, 29), 116),
        (date(2026, 3, 30), 120),
        (date(2026, 4,  5), 124),
        (date(2026, 4,  6), 134),
    ]
    games = 0
    for d, cum in schedule:
        if today >= d:
            games = cum
        else:
            break
    print(f"   📊 Estimated games scored (schedule fallback): {games}")
    return min(games, 134)


def estimate_games_scored(prev_score, curr_score, prev_snap=None):
    """Legacy fallback -- cumulative games by date."""
    from datetime import date
    today = date.today()
    schedule = [
        (date(2026, 3, 19),  8),
        (date(2026, 3, 20), 24),
        (date(2026, 3, 21), 48),
        (date(2026, 3, 22), 72),
        (date(2026, 3, 23), 96),
        (date(2026, 3, 27), 104),
        (date(2026, 3, 28), 112),
        (date(2026, 3, 29), 116),
        (date(2026, 3, 30), 120),
        (date(2026, 4,  5), 124),
        (date(2026, 4,  6), 134),
    ]
    games = 0
    for d, cum in schedule:
        if today >= d:
            games = cum
        else:
            break
    return min(games, 134)


def record_snapshot(history, rank, score, total, top_score, top_name,
                    entries_count, top8=None, kaggle_games=None):
    from datetime import timedelta, date
    now       = datetime.now(timezone.utc)
    munich    = now + timedelta(hours=1)
    prev      = history['snapshots'][-1] if history['snapshots'] else {}

    rank_change  = (rank  - prev['rank'])  if rank  and prev.get('rank')  else None
    score_change = round(score - prev['score'], 6) if score and prev.get('score') else None
    percentile   = round((1 - rank / total) * 100, 2) if rank and total else None
    gap_to_top   = round(score - top_score, 6) if score and top_score else None
    # Use Kaggle's official count if available, else estimate
    games_scored = kaggle_games if kaggle_games is not None else estimate_games_scored(prev.get('score'), score)

    snap = {
        'timestamp'         : now.isoformat(),
        'timestamp_readable': now.strftime('%Y-%m-%d %H:%M UTC'),
        'timestamp_local'   : munich.strftime('%Y-%m-%d %H:%M'),
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
        'games_scored'      : games_scored,
        'top8'              : top8 or [],
    }
    history['snapshots'].append(snap)
    if rank  and (history['best_rank']  is None or rank  < history['best_rank']):
        history['best_rank']  = rank
    if score and (history['best_score'] is None or score < history['best_score']):
        history['best_score'] = score

    # Update daily summary
    day_key = munich.strftime('%Y-%m-%d')
    if 'daily_summaries' not in history:
        history['daily_summaries'] = {}
    if day_key not in history['daily_summaries']:
        history['daily_summaries'][day_key] = {
            'date'          : day_key,
            'start_rank'    : rank,
            'start_score'   : score,
            'best_rank'     : rank,
            'worst_rank'    : rank,
            'end_rank'      : rank,
            'end_score'     : score,
            'checks'        : 1,
            'games_scored'  : games_scored,
        }
    else:
        d = history['daily_summaries'][day_key]
        d['end_rank']   = rank
        d['end_score']  = score
        d['checks']    += 1
        d['games_scored'] = games_scored
        if rank and (d['best_rank']  is None or rank < d['best_rank']):
            d['best_rank']  = rank
        if rank and (d['worst_rank'] is None or rank > d['worst_rank']):
            d['worst_rank'] = rank

    return snap


# ── Terminal display ────────────────────────────────────────────

def print_snap(snap, history):
    rank  = snap.get('rank')
    total = snap.get('total_teams')
    score = snap.get('score')
    pct   = snap.get('percentile')
    rc    = snap.get('rank_change')
    sc    = snap.get('score_change')
    top   = snap.get('top_score')
    gap   = snap.get('gap_to_top')
    ts    = snap.get('timestamp_local', snap.get('timestamp_readable', ''))

    W = 60
    print()
    print("═" * W)
    print("  🏀  MARCH MANIA 2026 -- RANK TRACKER")
    print("═" * W)
    print(f"  📅 {ts}")
    print(f"  {'─' * (W-4)}")

    if rank:
        ch = (f"  ↑ {abs(rc)} places 🎉" if rc and rc < 0 else
              f"  ↓ {rc} places"          if rc and rc > 0 else
              "  → no change"             if rc == 0 else "")
        print(f"  🏆 Rank        : #{rank} / {total}{ch}")
        print(f"  📊 Percentile  : top {100 - pct:.1f}% of all teams")
        sc_str = f"  ({sc:+.5f})" if sc is not None else ""
        print(f"  🎯 Score       : {score:.5f}{sc_str}")
        if top:
            print(f"  👑 Leader      : {top:.5f}  (gap {gap:+.5f})")
    else:
        print(f"  ❓ '{YOUR_USERNAME}' not found  |  Total teams: {total}")

    print(f"  {'─' * (W-4)}")
    br = history.get('best_rank')
    bs = history.get('best_score')
    print(f"  ★ Best rank  : #{br}" if br else "  ★ Best rank  : --")
    print(f"  ★ Best score : {bs:.5f}" if bs else "  ★ Best score : --")

    snaps = history.get('snapshots', [])
    if len(snaps) > 1:
        print(f"\n  📈 History ({len(snaps)} snapshots):")
        print(f"  {'Time':<22} {'Rank':>7} {'Score':>10} {'Δ':>6}")
        print(f"  {'─' * 50}")
        for s in snaps[-10:]:
            t  = s.get('timestamp_local', s.get('timestamp_readable', s['timestamp'][:16]))
            r  = f"#{s['rank']}" if s.get('rank') else '--'
            sc2= f"{s['score']:.5f}" if s.get('score') else '--'
            c  = s.get('rank_change')
            ch2= (f"↑{abs(c)}" if c and c < 0 else
                  f"↓{c}"      if c and c > 0 else
                  "→"          if c == 0 else '--')
            print(f"  {t:<22} {r:>7} {sc2:>10} {ch2:>6}")

    print()
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
        sys.exit(1)

    if verbose:
        print(f"   ✅ Got {len(entries)} entries")
        print(f"📋 Fetching your submissions...")

    submissions = fetch_submissions(username, key)
    if verbose:
        if submissions:
            print(f"   ✅ Got {len(submissions)} submissions")
            for s in submissions:
                sel = ' ✓ SELECTED' if s['selected'] else ''
                sc  = f"{s['score']:.5f}" if s['score'] else 'N/A'
                print(f"   {s['file']:<35} score={sc}{sel}")
        else:
            print(f"   ⚠️  Could not fetch submissions")

    rank, score, total, scored = find_rank(entries)

    if rank is None and verbose:
        print(f"\n⚠️  '{YOUR_USERNAME}' not found. Sample names:")
        for e in scored[:5]:
            print(f"   '{e['name']}'  {e['score']:.5f}")

    top       = scored[0] if scored else None
    top_score = top['score'] if top else None
    top_name  = top['name']  if top else '?'

    # Extract top 8 for leaderboard tracking
    top8 = [{'rank': i+1, 'name': e['name'], 'score': e['score']}
            for i, e in enumerate(scored[:8])]
    if verbose:
        print(f"👑 Top 8:")
        for t in top8:
            print(f"   #{t['rank']}  {t['name']:<30}  {t['score']:.5f}")

    # Fetch Kaggle's official games scored count
    if verbose:
        print(f"🎮 Fetching official games count from Kaggle...")
    kaggle_games = fetch_kaggle_games_scored(username, key)

    history = load_history()
    snap    = record_snapshot(history, rank, score, total,
                              top_score, top_name, len(scored), top8, kaggle_games)
    save_history(history, submissions if submissions else history.get('submissions'))

    if verbose:
        print_snap(snap, history)

    return snap


# ── Watch mode ──────────────────────────────────────────────────

def watch_mode(interval_minutes):
    print(f"\n🔄 WATCH MODE -- every {interval_minutes} min  (Ctrl+C to stop)\n")
    count = 0
    while True:
        count += 1
        print(f"\n{'─'*50}\n[Check #{count}]  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'─'*50}")
        try:
            run_check(verbose=True)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"⚠️  Error: {e}")

        next_time = datetime.fromtimestamp(time.time() + interval_minutes*60).strftime('%H:%M:%S')
        print(f"\n⏳ Next check at {next_time}  |  Ctrl+C to stop")
        try:
            time.sleep(interval_minutes * 60)
        except KeyboardInterrupt:
            raise


# ── Web server ──────────────────────────────────────────────────

class TrackerHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args): pass
    def do_GET(self):
        if self.path in ('/', ''):
            self.path = '/dashboard.html'
        super().do_GET()
    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()


def start_web_server():
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
    history   = load_history()
    snap      = record_snapshot(history, rank, score, total, top_score, top_name, len(scored))
    save_history(history)
    print_snap(snap, history)


# ── History print ───────────────────────────────────────────────

def print_history():
    h     = load_history()
    snaps = h.get('snapshots', [])
    print(f"\n📊 {len(snaps)} snapshots  |  Best rank: #{h.get('best_rank','--')}  Best score: {h.get('best_score','--')}")
    if not snaps:
        print("   No data yet.")
        return
    print(f"\n{'Time':<24} {'Rank':>8} {'Score':>10} {'Δ':>6} {'Leader':>10}")
    print("─" * 62)
    for s in snaps:
        t  = s.get('timestamp_local', s['timestamp'][:16])
        r  = f"#{s['rank']}" if s.get('rank') else '--'
        sc = f"{s['score']:.5f}" if s.get('score') else '--'
        c  = s.get('rank_change')
        ch = (f"↑{abs(c)}" if c and c < 0 else f"↓{c}" if c and c > 0 else "--")
        tp = f"{s['top_score']:.5f}" if s.get('top_score') else '--'
        print(f"{t:<24} {r:>8} {sc:>10} {ch:>6} {tp:>10}")


# ── Entry point ─────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='March Mania 2026 Rank Tracker')
    parser.add_argument('--interval',   type=int, default=DEFAULT_INTERVAL_MINUTES)
    parser.add_argument('--history',    action='store_true')
    parser.add_argument('--no-server',  action='store_true', help='Skip web server (GitHub Actions)')
    parser.add_argument('--no-browser', action='store_true', help='Skip opening browser')
    parser.add_argument('--from-file',  type=str, metavar='FILE')
    args = parser.parse_args()

    if args.history:
        print_history()
        return

    if args.from_file:
        run_from_file(args.from_file)
        return

    # GitHub Actions mode: single check then exit
    if args.no_server:
        print("🤖 GitHub Actions mode -- single check")
        run_check(verbose=True)
        return

    # Local PC mode: web server + watch loop
    if DASHBOARD_FILE.exists():
        start_web_server()
        if not args.no_browser:
            time.sleep(0.5)
            webbrowser.open(f'http://localhost:{WEB_PORT}')

    try:
        watch_mode(args.interval)
    except KeyboardInterrupt:
        print("\n\nStopped. History saved to tracker_history.json")


if __name__ == '__main__':
    main()
