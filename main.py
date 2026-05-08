from fastapi import FastAPI
import httpx
import asyncio
import os
from dotenv import load_dotenv
load_dotenv()
from pydantic import BaseModel
import urllib3
import time
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API_KEY = os.getenv("RIOT_API_KEY")
LOCKFILE_PATH = "/Applications/League of Legends.app/Contents/LoL/lockfile"

app = FastAPI()
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_cache = {}
_last_champ_select = None

def cache_get(key):
    """Return cached value if not expired, else None."""
    if key in _cache:
        value, expiry = _cache[key]
        if time.time() < expiry:
            return value
        else:
            del _cache[key]  # expired, clean up
    return None

def cache_set(key, value, ttl_seconds):
    """Store value in cache with expiry."""
    _cache[key] = (value, time.time() + ttl_seconds)

@app.get("/")
def root():
    return {"message": "server is running"}

async def get_recent_matches(puuid: str, region: str, queue_id: int, count: int = 10):
    """Fetch recent matches. Returns:
       - results/winrate from the current queue (for dots + streak)
       - all_matches: broader pool across queues (for mains + KDA averages)
    """
    cache_key = f"matches:{puuid}:{region}:{queue_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    
    headers = {"X-Riot-Token": API_KEY}
    
    async with httpx.AsyncClient() as client:
        valid_queues = {420, 440, 400, 450}
        
        # Build two URLs: one queue-filtered (recent dots), one unfiltered (broader pool)
        if queue_id in valid_queues:
            queue_ids_url = f"https://{region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?queue={queue_id}&count={count}"
        else:
            queue_ids_url = f"https://{region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count={count}"
        
        broad_ids_url = f"https://{region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count=20"
        
        # Fetch both in parallel
        queue_ids_resp, broad_ids_resp = await asyncio.gather(
            client.get(queue_ids_url, headers=headers),
            client.get(broad_ids_url, headers=headers),
        )
        
        if queue_ids_resp.status_code != 200 or broad_ids_resp.status_code != 200:
            empty = {"results": [], "winrate": 0.0, "matches": [], "all_matches": []}
            return empty
        
        queue_ids = queue_ids_resp.json()
        broad_ids = broad_ids_resp.json()
        
        # De-dupe: queue-filtered IDs are a subset of broad IDs, so we just fetch the union
        all_ids = list(dict.fromkeys(queue_ids + broad_ids))  # preserves order, removes dupes
        
        if not all_ids:
            empty = {"results": [], "winrate": 0.0, "matches": [], "all_matches": []}
            cache_set(cache_key, empty, ttl_seconds=600)
            return empty
        
        match_calls = [
            client.get(f"https://{region}.api.riotgames.com/lol/match/v5/matches/{match_id}", headers=headers)
            for match_id in all_ids
        ]
        match_responses = await asyncio.gather(*match_calls)
        
        # Build a lookup: match_id -> parsed match dict
        match_data_by_id = {}
        for match_id, resp in zip(all_ids, match_responses):
            if resp.status_code != 200:
                continue
            data = resp.json()
            if 'info' not in data:
                continue
            participants = data['info']['participants']
            me = next((p for p in participants if p['puuid'] == puuid), None)
            if me is None:
                continue
            
            match_data_by_id[match_id] = {
                "win": me['win'],
                "champion": me['championName'],
                "kills": me['kills'],
                "deaths": me['deaths'],
                "assists": me['assists'],
                "cs": me['totalMinionsKilled'] + me['neutralMinionsKilled'],
                "vision_score": me['visionScore'],
                "position": me.get('teamPosition', '') or me.get('individualPosition', ''),
                "game_duration": data['info']['gameDuration'],
                "game_end": data['info'].get('gameEndTimestamp', 0),
                "queue_id": data['info'].get('queueId', 0),
            }
        
        # Build queue-filtered list (recent in current queue)
        matches = [match_data_by_id[mid] for mid in queue_ids if mid in match_data_by_id]
        # Build broad list (all queues, for mains/averages)
        all_matches = [match_data_by_id[mid] for mid in broad_ids if mid in match_data_by_id]
        
        results = [m['win'] for m in matches]
        wins = sum(1 for r in results if r)
        winrate = round((wins / len(results)) * 100, 1) if results else 0.0
        
        result = {
            "results": results,
            "winrate": winrate,
            "matches": matches,        # queue-filtered, for dots/streak
            "all_matches": all_matches  # broader, for mains/averages
        }
        cache_set(cache_key, result, ttl_seconds=600)
        return result
    
def compute_trends(matches: list, all_matches: list = None, current_champion: str = None) -> dict:
    """Get stats and tags from a list of recent match objects."""
    if not matches:
        return {
            "avg_kda": None,
            "kda_ratio": None,
            "avg_cs_per_min": None,
            "mains": [],
            "streak": {"type": None, "count": 0},
            "main_role": None,
            "games_today": 0,
            "wins_today": 0,
            "tag": None,
        }
    
    # Use broader pool for averages/mains/role when available
    pool = all_matches if all_matches else matches
    
    # === Averages ===
    n = len(pool)
    avg_kills = sum(m['kills'] for m in pool) / n
    avg_deaths = sum(m['deaths'] for m in pool) / n
    avg_assists = sum(m['assists'] for m in pool) / n
    
    kda_ratio = (avg_kills + avg_assists) / avg_deaths if avg_deaths > 0 else (avg_kills + avg_assists)
    
    total_cs = sum(m['cs'] for m in pool)
    total_seconds = sum(m['game_duration'] for m in pool)
    avg_cs_per_min = (total_cs / (total_seconds / 60)) if total_seconds > 0 else 0
    
    # === Most played champions ===
    champ_counts = {}
    for m in pool:
        champ_counts[m['champion']] = champ_counts.get(m['champion'], 0) + 1
    mains = sorted(champ_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    mains = [{"champion": c, "games": count} for c, count in mains]
    
    # === Most played role ===
    role_counts = {}
    for m in pool:
        position = m.get('position', '')
        # Skip empty/invalid positions (ARAM, urf, custom)
        if position and position not in ('Invalid', ''):
            role_counts[position] = role_counts.get(position, 0) + 1
    
    main_role = None
    if role_counts:
        top_role, top_count = max(role_counts.items(), key=lambda x: x[1])
        main_role = {
            "role": top_role,
            "games": top_count,
            "total_with_role": sum(role_counts.values()),
        }
    
    # === Streak (queue-filtered) ===
    streak_type = None
    streak_count = 0
    if matches:
        first_result = matches[0]['win']
        for m in matches:
            if m['win'] == first_result:
                streak_count += 1
            else:
                break
        streak_type = "win" if first_result else "loss"
    
    # === Games today (across all queues) ===
    # game_end is in milliseconds since epoch
    twenty_four_hours_ago_ms = (time.time() - 86400) * 1000
    games_today = 0
    wins_today = 0
    for m in pool:
        if m.get('game_end', 0) >= twenty_four_hours_ago_ms:
            games_today += 1
            if m['win']:
                wins_today += 1
    
    # === Auto-tag (priority order, more specific tags override) ===
    tag = None
    
    # Streak tags
    if streak_type == "win" and streak_count >= 3:
        tag = "ON FIRE"
    elif streak_type == "loss" and streak_count >= 3:
        tag = "ON TILT"
    
    # Heavy session detection
    if games_today >= 6:
        losses_today = games_today - wins_today
        if losses_today >= 4:
            tag = "TILTED"  # 6+ games, 4+ losses
        elif games_today >= 8:
            tag = "GRINDING"
    
    # OTP — 7+ games on same champ
    if mains and mains[0]["games"] >= 7:
        tag = f"{mains[0]['champion']} OTP"
    
    # Smurf flag — high winrate on a low-game-count account
    # We need rank info to do this properly, but as a heuristic:
    # if they have <30 total games but >65% winrate, flag them
    if pool and len(pool) <= 20:  # we only have last 20 anyway, so this is approximate
        wins = sum(1 for m in pool if m['win'])
        recent_wr = wins / len(pool)
        if recent_wr >= 0.7 and len(pool) >= 5 and avg_kda_dominant(pool):
            tag = "SMURF?"
    
    # First time on the champion they're

async def get_account_by_puuid(puuid: str, region: str) -> dict | None:
    """Look up name#tagline from a puuid. Heavily cached because puuids are stable."""
    cache_key = f"account:{puuid}:{region}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    
    headers = {"X-Riot-Token": API_KEY}
    url = f"https://{region}.api.riotgames.com/riot/account/v1/accounts/by-puuid/{puuid}"
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
    
    if resp.status_code != 200:
        return None
    
    data = resp.json()
    result = {"name": data["gameName"], "tagline": data["tagLine"]}
    cache_set(cache_key, result, ttl_seconds=86400)  # 24h — puuids are stable
    return result

async def get_player_info_solo(summoner_name: str, tagline: str, platform: str, region: str, queue_id: int = 420):
    # Cache key includes everything that affects the result
    cache_key = f"player:{summoner_name}:{tagline}:{region}:{queue_id}"
    
    # Check cache first
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    
    headers = {"X-Riot-Token": API_KEY}
    
    async with httpx.AsyncClient() as client:
        puuidurl = f"https://{region}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{summoner_name}/{tagline}"
        puuidraw = await client.get(puuidurl, headers=headers)
        
        if puuidraw.status_code != 200:
            unknown = {
                'name': summoner_name, 'tagline': tagline, 'rank': 'Unknown',
                'lp': 0, 'wins': 0, 'losses': 0, 'winrate': 0.0,
                'recent_matches': {"results": [], "winrate": 0.0}
            }
            return unknown  # so it wont cache errors 
        
        puuid = puuidraw.json()['puuid']
        
        statsurl = f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        statsraw = await client.get(statsurl, headers=headers)
        
        if statsraw.status_code != 200:
            unknown = {
                'name': summoner_name, 'tagline': tagline, 'rank': 'Unknown',
                'lp': 0, 'wins': 0, 'losses': 0, 'winrate': 0.0,
                'recent_matches': {"results": [], "winrate": 0.0}
            }
            return unknown
        
        stats = statsraw.json()
    
    solo_entries = [e for e in stats if e['queueType'] == 'RANKED_SOLO_5x5']

    if not stats or not solo_entries:
        matches = await get_recent_matches(puuid, region, queue_id)
        trends = compute_trends(
        matches.get("matches", []),
        all_matches=matches.get("all_matches", []),
        current_champion=None
        )
        result = {
            'name': summoner_name, 'tagline': tagline, 'rank': 'Unranked',
            'lp': 0, 'wins': 0, 'losses': 0, 'winrate': 0.0,
            'recent_matches': matches,
            'trends': trends,
        }
        cache_set(cache_key, result, ttl_seconds=300)
        return result
    
    solo_duo = solo_entries[0]
    rank = str(solo_duo['tier']) + ' ' + str(solo_duo['rank'])
    lp = solo_duo['leaguePoints']
    wins = solo_duo['wins']
    losses = solo_duo['losses']
    winrate = (wins/(wins+losses)) * 100
    matches = await get_recent_matches(puuid, region, queue_id)
    trends = compute_trends(
        matches.get("matches", []),
        all_matches=matches.get("all_matches", []),
        current_champion=None
    )


    result = {
        'name': summoner_name,
        'tagline': tagline,
        'rank': rank,
        'lp': lp,
        'wins': wins,
        'losses': losses,
        'winrate': round(winrate, 1),
        'recent_matches': matches,
        'trends': trends
    }
    cache_set(cache_key, result, ttl_seconds=300)  # note thats its 5 mins
    return result

class Player(BaseModel):
    name: str
    tagline: str

@app.post("/players")
async def get_players(players: list[Player], region: str = "na", queue_id: int = 420):
    region_map = {
        "na": ("na1", "americas"),
        "euw": ("euw1", "europe"),
    }
    platform, riot_region = region_map[region]
    
    calls = [get_player_info_solo(p.name, p.tagline, platform, riot_region, queue_id) for p in players]
    results = await asyncio.gather(*calls)
    return results

def get_loading_screen_players():
    """Pull both teams from the LCU gameflow session.
    Returns list of {puuid, team} dicts, or None if not available."""
    try:
        port, password = get_lcu_credentials()
        url = f"https://127.0.0.1:{port}/lol-gameflow/v1/session"
        response = httpx.get(url, auth=("riot", password), verify=False, timeout=2.0)
        
        if response.status_code != 200:
            return None
        
        data = response.json()
        game_data = data.get('gameData', {})
        team_one = game_data.get('teamOne', [])
        team_two = game_data.get('teamTwo', [])
        queue_id = game_data.get('queue', {}).get('id', 0)
        
        # Tag each player with which team they're on
        players = []
        for p in team_one:
            if p.get('puuid'):
                players.append({"puuid": p['puuid'], "team": "ORDER"})
        for p in team_two:
            if p.get('puuid'):
                players.append({"puuid": p['puuid'], "team": "CHAOS"})
        
        if not players:
            return None
        
        return {"players": players, "queue_id": queue_id}
    except Exception:
        return None

def get_lcu_region():
    """Detect which region the running League client is on."""
    try:
        port, password = get_lcu_credentials()
        url = f"https://127.0.0.1:{port}/riotclient/region-locale"
        response = httpx.get(url, auth=("riot", password), verify=False)
        
        if response.status_code != 200:
            return None
        
        data = response.json()
        # data['region'] is e.g. 'NA', 'EUW', 'KR', etc.
        return data['region'].lower()
    except Exception:
        return None
    
def get_gameflow_phase():
    """Get the current LCU gameflow phase (Lobby, ChampSelect, InProgress, etc)."""
    try:
        port, password = get_lcu_credentials()
        url = f"https://127.0.0.1:{port}/lol-gameflow/v1/gameflow-phase"
        response = httpx.get(url, auth=("riot", password), verify=False, timeout=2.0)
        
        if response.status_code != 200:
            return None
        
        return response.json()
    except Exception:
        return None

def get_lcu_credentials():
    with open(LOCKFILE_PATH, "r") as f:
        contents = f.read()
    parts = contents.split(":")
    return parts[2], parts[3]

def get_champ_select_players():
    global _last_champ_select
    try:
        port, password = get_lcu_credentials()
        url = f"https://127.0.0.1:{port}/lol-champ-select/v1/session"
        response = httpx.get(url, auth=("riot", password), verify=False)
        
        if response.status_code != 200:
            return None
        
        data = response.json()
        players = data['myTeam'] + data['theirTeam']
        queue_id = data.get('queueId', 0)
        
        player_list = [
            {"name": p['gameName'], "tagline": p['tagLine']}
            for p in players if p['gameName']
        ]
        
        result = {"players": player_list, "queue_id": queue_id}
        _last_champ_select = result  # remember for loading screen fallback
        return result
    except (FileNotFoundError, Exception):
        return None

def get_live_game_data():
    """Fetch live in-game data. Returns None if not in a game."""
    try:
        gamestats_url = "https://127.0.0.1:2999/liveclientdata/gamestats"
        playerlist_url = "https://127.0.0.1:2999/liveclientdata/playerlist"
        
        gamestats_resp = httpx.get(gamestats_url, verify=False, timeout=2.0)
        if gamestats_resp.status_code != 200:
            return None
        
        playerlist_resp = httpx.get(playerlist_url, verify=False, timeout=2.0)
        if playerlist_resp.status_code != 200:
            return None
        
        gamestats = gamestats_resp.json()
        playerlist = playerlist_resp.json()
        
        live_players = {}
        for p in playerlist:
            key = f"{p['riotIdGameName']}#{p['riotIdTagLine']}"
            live_players[key] = {
                "champion": p['championName'],
                "level": p['level'],
                "kills": p['scores']['kills'],
                "deaths": p['scores']['deaths'],
                "assists": p['scores']['assists'],
                "cs": p['scores']['creepScore'],
                "items": [item['displayName'] for item in p.get('items', [])],
                "team": p['team'],
                "is_dead": p['isDead'],
                "respawn_timer": p['respawnTimer'],
                "is_bot": p['isBot'],
            }
        
        return {
            "game_time": gamestats['gameTime'],
            "game_mode": gamestats['gameMode'],
            "players": live_players
        }
    except (httpx.ConnectError, httpx.TimeoutException):
        return None
    except Exception:
        return None

@app.get("/champ-select")
async def champ_select(region: str = None):
    global _last_champ_select
    
    if region is None:
        region = get_lcu_region() or "na"
    
    region_map = {
        "na": ("na1", "americas"),
        "euw": ("euw1", "europe"),
        "kr": ("kr", "asia"),
        "eune": ("eun1", "europe"),
    }
    
    if region not in region_map:
        return {"state": "idle", "players": [], "region": region, "error": f"Unsupported region: {region}"}
    
    platform, riot_region = region_map[region]
    phase = get_gameflow_phase()
    
    # === In-game or loading screen ===
    if phase == "InProgress":
        live = get_live_game_data()
        
        if live is not None and live.get("game_time", 0) > 5.0:
            return {"state": "in_game", "players": [], "region": region, "game_time": live["game_time"]}
        
        # Live Client API not up yet => loading screen, replay last champ select roster
        if _last_champ_select:
            cs_players = _last_champ_select["players"]
            queue_id = _last_champ_select["queue_id"]
            calls = [
                get_player_info_solo(p["name"], p["tagline"], platform, riot_region, queue_id)
                for p in cs_players
            ]
            results = await asyncio.gather(*calls)
            return {
                "state": "loading",
                "players": results,
                "region": region,
                "queue_id": queue_id,
            }
        return {"state": "loading", "players": [], "region": region}
    
    # === Champ select ===
    if phase == "ChampSelect":
        cs_result = get_champ_select_players()
        if not cs_result:
            return {"state": "idle", "players": [], "region": region}
        
        # Cache the roster so loading screen can replay it
        _last_champ_select = cs_result
        
        cs_players = cs_result["players"]
        queue_id = cs_result["queue_id"]
        
        calls = [
            get_player_info_solo(p["name"], p["tagline"], platform, riot_region, queue_id)
            for p in cs_players
        ]
        results = await asyncio.gather(*calls)
        return {
            "state": "champ_select",
            "players": results,
            "region": region,
            "queue_id": queue_id,
        }
    
    # === Idle (lobby, matchmaking, post-game, none) ===
    return {"state": "idle", "players": [], "region": region}

@app.get("/live-game")
def live_game():
    """Fetch live in-game data from the Live Client API."""
    try:
        # Live Client API runs on a fixed port, no auth, self-signed cert
        gamestats_url = "https://127.0.0.1:2999/liveclientdata/gamestats"
        playerlist_url = "https://127.0.0.1:2999/liveclientdata/playerlist"
        
        gamestats_resp = httpx.get(gamestats_url, verify=False, timeout=2.0)
        if gamestats_resp.status_code != 200:
            return {"in_game": False}
        
        playerlist_resp = httpx.get(playerlist_url, verify=False, timeout=2.0)
        if playerlist_resp.status_code != 200:
            return {"in_game": False}
        
        gamestats = gamestats_resp.json()
        playerlist = playerlist_resp.json()
        
        live_players = {}
        for p in playerlist:
            key = f"{p['riotIdGameName']}#{p['riotIdTagLine']}"
            live_players[key] = {
                "champion": p['championName'],
                "level": p['level'],
                "kills": p['scores']['kills'],
                "deaths": p['scores']['deaths'],
                "assists": p['scores']['assists'],
                "cs": p['scores']['creepScore'],
                "items": [item['displayName'] for item in p.get('items', [])],
                "team": p['team'],  # "ORDER" or "CHAOS"
                "is_dead": p['isDead'],
                "respawn_timer": p['respawnTimer'],
                "is_bot": p['isBot'],
            }
        
        return {
            "in_game": True,
            "game_time": gamestats['gameTime'],
            "game_mode": gamestats['gameMode'],
            "players": live_players
        }
    except (httpx.ConnectError, httpx.TimeoutException):
        # Not in a game — Live Client API isn't running
        return {"in_game": False}
    except Exception as e:
        return {"in_game": False, "error": str(e)}