#!/usr/bin/env python3
"""
Trakt Collection & AllDebrid Periodic Sync & Cleanup Script
Audits Trakt Collection against AllDebrid magnets:
1. Identifies phantom movies and show episodes in Trakt Collection that have no files in AllDebrid.
2. Removes phantom items from Trakt Collection so plex_debrid can discover and download them.
3. Detects and cleans duplicate/redundant magnets in AllDebrid.
"""

import os
import sys
import re
import json
import argparse
import requests
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
SETTINGS_FILE = os.path.join(PROJECT_DIR, 'settings.json')
TRAKT_CLIENT_ID = 'pHbVNzLR5da9P4-GODsYtV6rZohyyhyLgCH73LQK6R0'

def normalize(s):
    if not s:
        return ''
    s = s.lower()
    s = re.sub(r'&', ' and ', s)
    s = re.sub(r'[\'"\:\,\.\_\-\(\)\[\]]', ' ', s)
    s = re.sub(r'\b(4|four)\b', '4', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        raise FileNotFoundError(f"Settings file not found at {SETTINGS_FILE}")
    with open(SETTINGS_FILE, 'r') as f:
        return json.load(f)

def get_alldebrid_magnets(apikey):
    url = f"https://api.alldebrid.com/v4.1/magnet/status?agent=plex_debrid&apikey={apikey}"
    try:
        r = requests.get(url, timeout=20)
        data = r.json()
        if data.get('status') == 'success':
            magnets = data.get('data', {}).get('magnets', [])
            return [m for m in magnets if m.get('statusCode') == 4 or m.get('status') == 'Ready']
    except Exception as e:
        print(f"[Error] Failed to fetch AllDebrid magnets: {e}")
    return []

def get_trakt_collection(token):
    headers = {
        'trakt-api-key': TRAKT_CLIENT_ID,
        'trakt-api-version': '2',
        'Authorization': f'Bearer {token}'
    }
    shows, movies = [], []
    try:
        r_shows = requests.get('https://api.trakt.tv/sync/collection/shows?extended=metadata', headers=headers, timeout=25)
        if r_shows.status_code == 200:
            shows = r_shows.json()
    except Exception as e:
        print(f"[Error] Failed to fetch Trakt shows collection: {e}")

    try:
        r_movies = requests.get('https://api.trakt.tv/sync/collection/movies?extended=metadata', headers=headers, timeout=25)
        if r_movies.status_code == 200:
            movies = r_movies.json()
    except Exception as e:
        print(f"[Error] Failed to fetch Trakt movies collection: {e}")

    return shows, movies

def parse_magnet_info(m, apikey=None):
    filename = m['filename']
    raw = filename
    fn_no_ext = re.sub(r'\.(mkv|mp4|avi|m4v|ts)$', '', raw, flags=re.I)
    
    # 1. Episode range on raw (e.g. S01E01-13 or S01E01-E13)
    ep_range = re.search(r'\bs(\d{1,2})e(\d{1,2})[-–]e?(\d{1,2})\b', raw, re.I)
    if ep_range:
        season_num = int(ep_range.group(1))
        ep_start = int(ep_range.group(2))
        ep_end = int(ep_range.group(3))
        title_part = normalize(raw[:ep_range.start()])
        return {
            'type': 'episode_range',
            'title': title_part,
            'season': season_num,
            'episodes': list(range(ep_start, ep_end + 1)),
            'raw': filename
        }

    # 2. Multi-season pack on raw (e.g. S01-S04 or Season 1-4)
    multi_season = re.search(r'\bs(\d{1,2})\s*[-–to]\s*s?(\d{1,2})\b', raw, re.I)
    if multi_season:
        s_start, s_end = int(multi_season.group(1)), int(multi_season.group(2))
        title_part = normalize(raw[:multi_season.start()])
        return {
            'type': 'multi_season',
            'title': title_part,
            'seasons': list(range(s_start, s_end + 1)),
            'raw': filename
        }

    # 3. Single episode on raw (e.g. S01E02)
    ep_match = re.search(r'\bs(\d{1,2})e(\d{1,2})\b', raw, re.I)
    if ep_match:
        season_num = int(ep_match.group(1))
        ep_num = int(ep_match.group(2))
        title_part = normalize(raw[:ep_match.start()])
        return {
            'type': 'episode',
            'title': title_part,
            'season': season_num,
            'episode': ep_num,
            'raw': filename
        }

    # 4. Single season pack on raw (e.g. S01, Season 1)
    s_pack = re.search(r'\b(?:s|season\s*)(\d{1,2})\b', raw, re.I)
    if s_pack:
        season_num = int(s_pack.group(1))
        title_part = normalize(raw[:s_pack.start()])
        return {
            'type': 'season',
            'title': title_part,
            'season': season_num,
            'raw': filename
        }

    norm = normalize(fn_no_ext)
    # Check if files inside contain seasons (like THE SOPRANOS)
    if apikey and m.get('nbLinks', 0) > 1:
        try:
            r = requests.get(f"https://api.alldebrid.com/v4.1/magnet/status?agent=plex_debrid&apikey={apikey}&id={m['id']}", timeout=5)
            f_data = r.json().get('data', {}).get('magnets', {}).get('files', [])
            seasons_found = set()
            def find_seasons(flist):
                for item in flist:
                    if 'n' in item:
                        sm = re.search(r'\bs(\d{1,2})e\d{1,2}\b', item['n'], re.I)
                        if sm:
                            seasons_found.add(int(sm.group(1)))
                    if 'e' in item:
                        find_seasons(item['e'])
            find_seasons(f_data)
            if seasons_found:
                return {
                    'type': 'multi_season',
                    'title': norm,
                    'seasons': sorted(list(seasons_found)),
                    'raw': filename
                }
        except Exception:
            pass

    # Movie (title + year)
    year_match = re.search(r'\b(19\d\d|20\d\d)\b', norm)
    year = int(year_match.group(1)) if year_match else None
    title_part = norm[:year_match.start()].strip() if year_match else norm
    return {
        'type': 'movie',
        'title': title_part,
        'year': year,
        'raw': filename
    }

def match_show_title(trakt_title, magnet_title):
    t = normalize(trakt_title)
    m = normalize(magnet_title)
    if not t or not m:
        return False
    if t in m or m in t:
        return True
    t_no_space = t.replace(' ', '')
    m_no_space = m.replace(' ', '')
    if t_no_space in m_no_space or m_no_space in t_no_space:
        return True
    return False

def match_movie_title(trakt_title, trakt_year, magnet_parsed):
    t = normalize(trakt_title)
    m = normalize(magnet_parsed['title'])
    if t == m or t in m or m in t:
        return True
    t_no_space = t.replace(' ', '')
    m_no_space = m.replace(' ', '')
    if t_no_space in m_no_space or m_no_space in t_no_space:
        return True
    return False

def audit_library(magnets, trakt_shows, trakt_movies, apikey=None):
    parsed_magnets = []
    for m in magnets:
        info = parse_magnet_info(m, apikey)
        info['id'] = m['id']
        info['size'] = m.get('size', 0)
        parsed_magnets.append(info)

    # 1. Audit Movies
    phantom_movies = []
    for item in trakt_movies:
        movie = item['movie']
        m_title = movie['title']
        m_year = movie.get('year')
        found = False
        for pm in parsed_magnets:
            if pm['type'] == 'movie' and match_movie_title(m_title, m_year, pm):
                found = True
                break
        if not found:
            phantom_movies.append({
                'title': m_title,
                'year': m_year,
                'ids': movie['ids']
            })

    # 2. Audit Shows & Episodes
    phantom_shows = []
    for item in trakt_shows:
        show = item['show']
        s_title = show['title']
        s_ids = show['ids']
        
        show_magnets = [pm for pm in parsed_magnets if match_show_title(s_title, pm['title'])]
        
        missing_seasons = []
        for season in item.get('seasons', []):
            s_num = season['number']
            if s_num == 0:  # specials
                continue
            
            season_pack_found = any(
                (pm['type'] == 'season' and pm['season'] == s_num) or
                (pm['type'] == 'multi_season' and s_num in pm['seasons'])
                for pm in show_magnets
            )
            
            if season_pack_found:
                continue
            
            missing_episodes = []
            for ep in season.get('episodes', []):
                e_num = ep['number']
                ep_found = any(
                    (pm['type'] == 'episode' and pm['season'] == s_num and pm['episode'] == e_num) or
                    (pm['type'] == 'episode_range' and pm['season'] == s_num and e_num in pm['episodes'])
                    for pm in show_magnets
                )
                if not ep_found:
                    missing_episodes.append({'number': e_num})
            
            if missing_episodes:
                missing_seasons.append({
                    'number': s_num,
                    'episodes': missing_episodes
                })
        
        if missing_seasons:
            phantom_shows.append({
                'title': s_title,
                'ids': s_ids,
                'seasons': missing_seasons
            })

    # 3. Detect AllDebrid Duplicate Magnets
    duplicate_magnets = []
    groups = {}
    for pm in parsed_magnets:
        key = None
        if pm['type'] == 'movie':
            key = f"movie:{normalize(pm['title'])}"
        elif pm['type'] == 'season':
            key = f"show:{normalize(pm['title'])}:s{pm['season']}"
        elif pm['type'] == 'multi_season':
            key = f"show:{normalize(pm['title'])}:multi:{'-'.join(map(str, pm['seasons']))}"
        elif pm['type'] == 'episode':
            key = f"show:{normalize(pm['title'])}:s{pm['season']}e{pm['episode']}"

        if key:
            groups.setdefault(key, []).append(pm)

    for key, items in groups.items():
        if len(items) > 1:
            items.sort(key=lambda x: x['size'], reverse=True)
            kept = items[0]
            for redundant in items[1:]:
                duplicate_magnets.append({
                    'kept': kept,
                    'remove': redundant,
                    'reason': f"Duplicate for {key}"
                })

    return phantom_movies, phantom_shows, duplicate_magnets

def execute_trakt_removal(token, phantom_movies, phantom_shows):
    headers = {
        'trakt-api-key': TRAKT_CLIENT_ID,
        'trakt-api-version': '2',
        'Authorization': f'Bearer {token}'
    }
    payload = {}
    if phantom_movies:
        payload['movies'] = [{'ids': m['ids']} for m in phantom_movies]
    if phantom_shows:
        payload['shows'] = [{'ids': s['ids'], 'seasons': s['seasons']} for s in phantom_shows]

    if not payload:
        print("[Trakt] Nothing to remove.")
        return

    try:
        r = requests.post('https://api.trakt.tv/sync/collection/remove', headers=headers, json=payload, timeout=25)
        if r.status_code in [200, 201]:
            data = r.json()
            deleted_movies = data.get('deleted', {}).get('movies', 0)
            deleted_episodes = data.get('deleted', {}).get('episodes', 0)
            print(f"[Trakt Sync] Successfully removed {deleted_movies} phantom movies and {deleted_episodes} phantom episodes from Trakt Collection.")
        else:
            print(f"[Trakt Sync] Removal response ({r.status_code}): {r.text}")
    except Exception as e:
        print(f"[Trakt Sync Error] Failed to remove items from Trakt: {e}")

def execute_alldebrid_cleanup(apikey, duplicate_magnets):
    if not duplicate_magnets:
        print("[AllDebrid] No duplicates to clean.")
        return

    deleted_count = 0
    for item in duplicate_magnets:
        m_id = item['remove']['id']
        name = item['remove']['raw']
        url = f"https://api.alldebrid.com/v4.1/magnet/delete?agent=plex_debrid&apikey={apikey}&id={m_id}"
        try:
            r = requests.get(url, timeout=15)
            data = r.json()
            if data.get('status') == 'success':
                print(f"[AllDebrid Clean] Deleted redundant magnet #{m_id}: {name}")
                deleted_count += 1
            else:
                print(f"[AllDebrid Clean] Failed to delete #{m_id}: {data}")
        except Exception as e:
            print(f"[AllDebrid Clean Error] Failed to delete #{m_id}: {e}")
        time.sleep(0.5)

    print(f"[AllDebrid Clean] Successfully removed {deleted_count} duplicate magnets.")

def main():
    parser = argparse.ArgumentParser(description="Audit and sync Trakt Collection with AllDebrid magnets.")
    parser.add_argument('--apply', action='store_true', help="Execute removals on Trakt and AllDebrid.")
    parser.add_argument('--clean-duplicates', action='store_true', help="Also delete duplicate magnets from AllDebrid.")
    parser.add_argument('--dry-run', action='store_true', help="Only display the audit report without modifying anything.")
    args = parser.parse_args()

    apply_mode = args.apply and not args.dry_run

    settings = load_settings()
    apikey = settings.get('All Debrid API Key')
    trakt_users = settings.get('Trakt users', [])
    if not apikey or not trakt_users:
        print("[Error] Missing AllDebrid API key or Trakt user token in settings.json.")
        sys.exit(1)

    token = trakt_users[0][1]

    print("=" * 70)
    print(f"Trakt Collection & AllDebrid Sync Audit - {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Mode: {'APPLY (Changes will be executed)' if apply_mode else 'DRY RUN (Report only)'}")
    print("=" * 70)

    print("\n1. Fetching AllDebrid magnets...")
    magnets = get_alldebrid_magnets(apikey)
    print(f"   Found {len(magnets)} active/ready magnets.")

    print("\n2. Fetching Trakt Collection...")
    trakt_shows, trakt_movies = get_trakt_collection(token)
    print(f"   Found {len(trakt_shows)} shows and {len(trakt_movies)} movies in Trakt Collection.")

    print("\n3. Reconciling library...")
    phantom_movies, phantom_shows, duplicate_magnets = audit_library(magnets, trakt_shows, trakt_movies, apikey)

    print("\n" + "-" * 70)
    print(f"AUDIT SUMMARY:")
    print(f"  • Phantom Movies in Trakt (missing from AllDebrid): {len(phantom_movies)}")
    print(f"  • Shows with Phantom Episodes in Trakt: {len(phantom_shows)}")
    print(f"  • Redundant Magnet Duplicates in AllDebrid: {len(duplicate_magnets)}")
    print("-" * 70)

    if phantom_movies:
        print("\n[!] Phantom Movies in Trakt Collection (not in AllDebrid):")
        for m in phantom_movies:
            print(f"    - {m['title']} ({m.get('year')})")

    if phantom_shows:
        print("\n[!] Shows with Phantom Episodes in Trakt Collection (not in AllDebrid):")
        for s in phantom_shows:
            seasons_summary = []
            for sea in s['seasons']:
                ep_nums = [e['number'] for e in sea['episodes']]
                if len(ep_nums) > 5:
                    seasons_summary.append(f"S{sea['number']} ({len(ep_nums)} eps: E{ep_nums[0]}-E{ep_nums[-1]})")
                else:
                    seasons_summary.append(f"S{sea['number']} (eps: {ep_nums})")
            print(f"    - {s['title']}: {', '.join(seasons_summary)}")

    if duplicate_magnets:
        print("\n[!] Redundant Magnet Duplicates in AllDebrid:")
        for d in duplicate_magnets:
            print(f"    - Remove #{d['remove']['id']} ({d['remove']['raw']}) : {d['reason']}")
            print(f"      Keeping #{d['kept']['id']} ({d['kept']['raw']})")

    if apply_mode:
        print("\n4. Executing Removals...")
        execute_trakt_removal(token, phantom_movies, phantom_shows)
        if args.clean_duplicates:
            execute_alldebrid_cleanup(apikey, duplicate_magnets)
        else:
            print("[Note] Skipped AllDebrid duplicate cleanup (use --clean-duplicates to enable).")
        print("\nSync completed successfully!")
    else:
        print("\n[Dry Run Completed] No modifications were made. Run with --apply to execute.")

if __name__ == '__main__':
    main()
