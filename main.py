from fastapi import FastAPI
import httpx
import asyncio
import os
import json
from dotenv import load_dotenv
load_dotenv()
from pydantic import BaseModel
import urllib3
import time
from collections import deque
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
# Scouting results captured pre-game, keyed "name#tagline". The in-game view
# reads from this instead of re-querying Riot, so a long game costs no quota.
_scout_snapshot = {}
# Riot IDs we have already tried to scout, so a failed or mid-game backfill is
# attempted once rather than on every poll.
_scout_attempted = set()

# How many recent matches to pull per player. This is the main lever on rate
# limiting: each match costs one request, and a personal key only affords
# ~10 requests per player when scouting a full 10-player lobby.
MATCH_COUNT = 6


class RiotRateLimiter:
    """Shared token bucket for the Riot API.

    Personal keys allow 20 requests/second and 100 per 2 minutes, applied
    across every endpoint. Scouting 10 players bursts well past both, so all
    Riot calls funnel through here and wait their turn instead of 429ing.
    Limits are set slightly under the real ones to leave room for retries.
    """

    PER_SECOND = 18
    PER_WINDOW = 95
    WINDOW = 120.0

    def __init__(self):
        self._lock = asyncio.Lock()
        self._sent = deque()

    async def acquire(self):
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._sent and now - self._sent[0] > self.WINDOW:
                    self._sent.popleft()

                in_last_second = sum(1 for t in self._sent if now - t < 1.0)
                if len(self._sent) < self.PER_WINDOW and in_last_second < self.PER_SECOND:
                    self._sent.append(now)
                    return

                if in_last_second >= self.PER_SECOND:
                    wait = 1.0 - (now - self._sent[-self.PER_SECOND])
                else:
                    wait = self.WINDOW - (now - self._sent[0])

            await asyncio.sleep(max(0.05, min(wait, 5.0)))


_limiter = RiotRateLimiter()


async def riot_get(client, url, headers=None, max_retries=2):
    """GET a Riot API endpoint through the rate limiter, retrying on 429."""
    headers = headers or {"X-Riot-Token": API_KEY}
    resp = None
    for attempt in range(max_retries + 1):
        await _limiter.acquire()
        resp = await client.get(url, headers=headers)
        if resp.status_code != 429 or attempt == max_retries:
            return resp
        retry_after = float(resp.headers.get("Retry-After", "1") or 1)
        print(f"[ratelimit] 429 on {url[:60]}..., waiting {retry_after}s", flush=True)
        await asyncio.sleep(retry_after + 0.1)
    return resp

async def get_champion_map() -> dict:
    """Map numeric champion ids to names, via Data Dragon.

    Data Dragon is a static CDN, not the rate-limited Riot API, so it does not
    go through riot_get. Its champion `id` strings ("Chogath") match the
    championName field in match data, so tags compare cleanly.
    """
    cached = cache_get("champion-map")
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient() as client:
            versions = await client.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=5.0)
            if versions.status_code != 200:
                print(f"[ddragon] versions {versions.status_code}", flush=True)
                return {}
            version = versions.json()[0]

            champs = await client.get(
                f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json",
                timeout=5.0,
            )
            if champs.status_code != 200:
                print(f"[ddragon] champion.json {champs.status_code}", flush=True)
                return {}

        mapping = {int(c["key"]): c["id"] for c in champs.json()["data"].values()}
        cache_set("champion-map", mapping, ttl_seconds=86400)
        print(f"[ddragon] loaded {len(mapping)} champions (patch {version})", flush=True)
        return mapping
    except Exception as e:
        print(f"[ddragon] EXCEPTION: {type(e).__name__}: {e}", flush=True)
        return {}


def is_riot_puuid(value: str | None) -> bool:
    """Riot API PUUIDs are long encrypted strings, not 36-char LCU UUID placeholders."""
    return bool(value and len(value) >= 60)

def split_riot_id(value: str | None) -> tuple[str | None, str | None]:
    if not value or "#" not in value:
        return None, None
    name, tagline = value.rsplit("#", 1)
    if not name or not tagline:
        return None, None
    return name, tagline

def extract_loading_player(player: dict, team: str) -> dict | None:
    """Extract the best identity LCU exposes for a loading-screen player."""
    name = player.get("gameName") or player.get("riotIdGameName")
    tagline = player.get("tagLine") or player.get("tagline") or player.get("riotIdTagLine")
    if name and tagline:
        return {"name": name, "tagline": tagline, "team": team}

    for key in ("riotId", "riotIdName", "summonerName", "displayName"):
        name, tagline = split_riot_id(player.get(key))
        if name and tagline:
            return {"name": name, "tagline": tagline, "team": team}

    for key in ("puuid", "playerPuuid", "riotPuuid", "accountPuuid"):
        puuid = player.get(key)
        if is_riot_puuid(puuid):
            return {"puuid": puuid, "team": team}

    summoner_id = player.get("summonerId")
    if summoner_id:
        return {"summoner_id": summoner_id, "team": team}

    return None

def dump_loading_player_fields(team_one: list, team_two: list) -> None:
    payload = {"teamOne": team_one, "teamTwo": team_two}
    print(f"[loading] raw player objects={json.dumps(payload, default=str)}", flush=True)

def get_lcu_summoner_by_id(summoner_id: int) -> dict | None:
    """Resolve a summonerId through the local League client."""
    cache_key = f"lcu-summoner:{summoner_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        port, password = get_lcu_credentials()
        url = f"https://127.0.0.1:{port}/lol-summoner/v1/summoners/{summoner_id}"
        response = httpx.get(url, auth=("riot", password), verify=False, timeout=2.0)
        if response.status_code != 200:
            print(f"[lcu-summoner] {response.status_code} for summonerId={summoner_id}", flush=True)
            return None

        data = response.json()
        name = data.get("gameName") or data.get("riotIdGameName")
        tagline = data.get("tagLine") or data.get("riotIdTagLine")
        if not name or not tagline:
            riot_id = data.get("displayName") or data.get("gameName")
            name, tagline = split_riot_id(riot_id)
        if not name or not tagline:
            print(f"[lcu-summoner] missing Riot ID for summonerId={summoner_id}", flush=True)
            return None

        result = {"name": name, "tagline": tagline}
        cache_set(cache_key, result, ttl_seconds=300)
        return result
    except Exception as e:
        print(f"[lcu-summoner] EXCEPTION for summonerId={summoner_id}: {type(e).__name__}: {e}", flush=True)
        return None

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

async def get_recent_matches(puuid: str, region: str, queue_id: int, count: int = MATCH_COUNT):
    """Fetch recent matches for the current queue (falling back to all queues).

    One id request plus `count` detail requests. Keeping this lean matters:
    a full lobby multiplies whatever we spend here by ten.
    """
    cache_key = f"matches:{puuid}:{region}:{queue_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    empty = {"results": [], "winrate": 0.0, "matches": [], "all_matches": []}
    ids_base = f"https://{region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"

    async with httpx.AsyncClient() as client:
        valid_queues = {420, 440, 400, 450}

        all_ids = []
        if queue_id in valid_queues:
            resp = await riot_get(client, f"{ids_base}?queue={queue_id}&count={count}")
            if resp.status_code != 200:
                print(f"[matches] id-fetch failed for {puuid[:8]}...: {resp.status_code}", flush=True)
                return empty
            all_ids = resp.json()

        # Nothing in this queue (or an unranked/rotating mode): widen to any queue
        # so brand-new-to-the-mode players still show something.
        if not all_ids:
            resp = await riot_get(client, f"{ids_base}?count={count}")
            if resp.status_code != 200:
                print(f"[matches] broad id-fetch failed for {puuid[:8]}...: {resp.status_code}", flush=True)
                return empty
            all_ids = resp.json()

        if not all_ids:
            cache_set(cache_key, empty, ttl_seconds=600)
            return empty

        match_calls = [
            riot_get(client, f"https://{region}.api.riotgames.com/lol/match/v5/matches/{match_id}")
            for match_id in all_ids
        ]
        match_responses = await asyncio.gather(*match_calls)
        
        # Build a lookup: match_id -> parsed match dict
        match_data_by_id = {}
        failed_codes = {}
        for match_id, resp in zip(all_ids, match_responses):
            if resp.status_code != 200:
                failed_codes[resp.status_code] = failed_codes.get(resp.status_code, 0) + 1
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
        
        if failed_codes:
            print(
                f"[matches] {puuid[:8]}...: {len(match_data_by_id)}/{len(all_ids)} match fetches ok, "
                f"failures={failed_codes}",
                flush=True,
            )

        # Newest-first, matching the order Riot returned the ids in
        matches = [match_data_by_id[mid] for mid in all_ids if mid in match_data_by_id]

        results = [m['win'] for m in matches]
        wins = sum(1 for r in results if r)
        winrate = round((wins / len(results)) * 100, 1) if results else 0.0

        result = {
            "results": results,
            "winrate": winrate,
            "matches": matches,
            "all_matches": matches,  # single pool now; kept for response-shape compat
        }
        cache_set(cache_key, result, ttl_seconds=600)
        return result

def avg_kda_dominant(matches: list) -> bool:
    """Helper for compute_trends"""
    if not matches:
        return False
    avg_k = sum(m['kills'] for m in matches) / len(matches)
    avg_d = sum(m['deaths'] for m in matches) / len(matches)
    avg_a = sum(m['assists'] for m in matches) / len(matches)
    if avg_d == 0:
        return True
    return (avg_k + avg_a) / avg_d >= 4.0

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
            "tag_kind": None,
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
    # Thresholds scale with the pool so they stay reachable: we only fetch
    # MATCH_COUNT games, and a hardcoded "7 games on one champ" can never fire
    # against a 6-game pool.
    heavy_session = max(4, round(MATCH_COUNT * 0.8))
    otp_games = max(3, round(MATCH_COUNT * 0.8))
    # tag is display text; tag_kind is a stable slug for styling, so the
    # frontend never has to slugify champion names into CSS classes.
    tag = None
    tag_kind = None

    # Streak tags
    if streak_type == "win" and streak_count >= 3:
        tag, tag_kind = "ON FIRE", "on-fire"
    elif streak_type == "loss" and streak_count >= 3:
        tag, tag_kind = "ON TILT", "on-tilt"

    # Heavy session detection
    if games_today >= heavy_session:
        losses_today = games_today - wins_today
        if losses_today >= max(3, round(heavy_session * 0.7)):
            tag, tag_kind = "TILTED", "tilted"
        elif games_today >= MATCH_COUNT:
            tag, tag_kind = "GRINDING", "grinding"

    # OTP — most of their recent games on one champ
    if mains and mains[0]["games"] >= otp_games:
        tag, tag_kind = f"{mains[0]['champion']} OTP", "otp"

    # Smurf flag — high winrate with dominant KDA on a small sample
    if pool:
        wins = sum(1 for m in pool if m['win'])
        recent_wr = wins / len(pool)
        if recent_wr >= 0.7 and len(pool) >= 5 and avg_kda_dominant(pool):
            tag, tag_kind = "SMURF?", "smurf"

    # What they're locking in this game beats any general-purpose tag: it is
    # the one signal you can still act on during champ select.
    if current_champion:
        champ_games = sum(1 for m in pool if m['champion'] == current_champion)
        if champ_games >= otp_games:
            tag, tag_kind = f"{current_champion} OTP", "otp"
        elif champ_games == 0:
            # Only says it is absent from their last MATCH_COUNT games, not that
            # they have never played it — hence the question mark.
            tag, tag_kind = f"1ST TIME {current_champion}?", "first-time"

    return {
        "avg_kda": {
            "kills": round(avg_kills, 1),
            "deaths": round(avg_deaths, 1),
            "assists": round(avg_assists, 1),
        },
        "kda_ratio": round(kda_ratio, 2),
        "avg_cs_per_min": round(avg_cs_per_min, 1),
        "mains": mains,
        "streak": {"type": streak_type, "count": streak_count},
        "main_role": main_role,
        "games_today": games_today,
        "wins_today": wins_today,
        "tag": tag,
        "tag_kind": tag_kind,
    }

async def get_account_by_puuid(puuid: str, region: str) -> dict | None:
    """Look up name#tagline from a puuid. Heavily cached because puuids are stable."""
    cache_key = f"account:{puuid}:{region}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    
    url = f"https://{region}.api.riotgames.com/riot/account/v1/accounts/by-puuid/{puuid}"

    try:
        async with httpx.AsyncClient() as client:
            resp = await riot_get(client, url)
    except Exception as e:
        print(f"[account] EXCEPTION for {puuid[:8]}... region={region}: {type(e).__name__}: {e}", flush=True)
        return None
    
    if resp.status_code != 200:
        print(f"[account] {resp.status_code} for {puuid[:8]}... region={region} url={url[:80]}", flush=True)
        return None
    
    data = resp.json()
    result = {"name": data["gameName"], "tagline": data["tagLine"]}
    cache_set(cache_key, result, ttl_seconds=86400)  # 24h 
    return result

async def get_player_info_solo(summoner_name: str, tagline: str, platform: str, region: str, queue_id: int = 420,
                               current_champion: str | None = None):
    # Cache key includes everything that affects the result. current_champion
    # changes the tag, so it belongs here — the underlying match fetch is cached
    # separately, so a champion swap recomputes tags without new API calls.
    cache_key = f"player:{summoner_name}:{tagline}:{region}:{queue_id}:{current_champion or '-'}"
    
    # Check cache first
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    
    async with httpx.AsyncClient() as client:
        puuidurl = f"https://{region}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{summoner_name}/{tagline}"
        puuidraw = await riot_get(client, puuidurl)
        
        if puuidraw.status_code != 200:
            unknown = {
                'name': summoner_name, 'tagline': tagline, 'rank': 'Unknown',
                'lp': 0, 'wins': 0, 'losses': 0, 'winrate': 0.0,
                'recent_matches': {"results": [], "winrate": 0.0}
            }
            return unknown  # so it wont cache errors 
        
        puuid = puuidraw.json()['puuid']
        
        statsurl = f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        statsraw = await riot_get(client, statsurl)
        
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
        current_champion=current_champion,
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
        current_champion=current_champion,
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
            print(f"[loading] bad status: {response.status_code}", flush=True)
            return None
        
        data = response.json()
        game_data = data.get('gameData', {})
        team_one = game_data.get('teamOne', [])
        team_two = game_data.get('teamTwo', [])
        queue_id = game_data.get('queue', {}).get('id', 0)
        print(f"[loading] teamOne={len(team_one)} teamTwo={len(team_two)} queue={queue_id} phase={data.get('phase')}", flush=True)
        
        players = []
        invalid_players = []
        for team, team_players in (("ORDER", team_one), ("CHAOS", team_two)):
            for p in team_players:
                extracted = extract_loading_player(p, team)
                if extracted:
                    players.append(extracted)
                else:
                    invalid_players.append(p)
        
        if not players:
            print("[loading] no usable Riot IDs or Riot PUUIDs extracted", flush=True)
            dump_loading_player_fields(team_one, team_two)
            return None

        if invalid_players:
            print(f"[loading] skipped {len(invalid_players)} players with no usable Riot identity", flush=True)
            dump_loading_player_fields(team_one, team_two)
        
        print(f"[loading] returning {len(players)} players", flush=True)
        return {"players": players, "queue_id": queue_id}
    except Exception as e:
        print(f"[loading] EXCEPTION: {type(e).__name__}: {e}", flush=True)
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
        
        # championId is 0 until they lock in; championPickIntent shows the
        # hover, which is what you actually want to react to during the pick.
        player_list = [
            {
                "name": p['gameName'],
                "tagline": p['tagLine'],
                "champion_id": p.get('championId') or p.get('championPickIntent') or 0,
            }
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
            print(f"[live] gamestats {gamestats_resp.status_code}", flush=True)
            return None

        playerlist_resp = httpx.get(playerlist_url, verify=False, timeout=2.0)
        if playerlist_resp.status_code != 200:
            print(f"[live] playerlist {playerlist_resp.status_code}", flush=True)
            return None

        gamestats = gamestats_resp.json()
        playerlist = playerlist_resp.json()

        # Defensive throughout: one unexpected entry (a bot, a renamed field)
        # must not blank the whole overlay.
        live_players = {}
        for p in playerlist:
            name = p.get('riotIdGameName') or p.get('summonerName') or ''
            tagline = p.get('riotIdTagLine') or ''
            if not name:
                continue
            key = f"{name}#{tagline}" if tagline else name
            scores = p.get('scores') or {}
            live_players[key] = {
                "champion": p.get('championName'),
                "level": p.get('level'),
                "kills": scores.get('kills', 0),
                "deaths": scores.get('deaths', 0),
                "assists": scores.get('assists', 0),
                "cs": scores.get('creepScore', 0),
                "items": [i.get('displayName') for i in p.get('items', [])],
                "team": p.get('team'),
                "is_dead": p.get('isDead', False),
                "respawn_timer": p.get('respawnTimer', 0),
                "is_bot": p.get('isBot', False),
            }

        return {
            "game_time": gamestats.get('gameTime', 0),
            "game_mode": gamestats.get('gameMode'),
            "players": live_players
        }
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        # Expected outside a game: the API only listens while one is running.
        print(f"[live] not reachable: {type(e).__name__}", flush=True)
        return None
    except Exception as e:
        print(f"[live] EXCEPTION: {type(e).__name__}: {e}", flush=True)
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
        print(
            f"[in_game] live={'present' if live else 'None'} "
            f"game_time={live.get('game_time') if live else 'n/a'} "
            f"live_players={len(live.get('players', {})) if live else 0} "
            f"snapshot={len(_scout_snapshot)}",
            flush=True,
        )

        if live is not None and live.get("game_time", 0) > 5.0:
            # Merge live stats with the scouting snapshot taken at the loading
            # screen. Deliberately no Riot API calls here: the Live Client API
            # is local and unmetered, and a 25-minute game polling every few
            # seconds would otherwise burn the rate limit for nothing.
            # If we never scouted these players (backend restarted, or the
            # overlay was launched mid-game), backfill once. Guarded by
            # _scout_attempted, which is updated before the await so
            # overlapping polls cannot start duplicate backfills.
            live_ids = list(live.get("players", {}).keys())
            missing = [
                rid for rid in live_ids
                if rid not in _scout_snapshot and rid not in _scout_attempted
            ]
            if missing:
                _scout_attempted.update(missing)
                backfill_queue = (_last_champ_select or {}).get("queue_id", 0)
                print(f"[in_game] backfilling {len(missing)} unscouted players", flush=True)
                pairs = [split_riot_id(rid) for rid in missing]
                backfill = await asyncio.gather(*[
                    get_player_info_solo(n, t, platform, riot_region, backfill_queue)
                    for n, t in pairs if n and t
                ])
                _scout_snapshot.update({f"{r['name']}#{r['tagline']}": r for r in backfill})

            players = []
            for riot_id, lp in live.get("players", {}).items():
                scouted = _scout_snapshot.get(riot_id, {})
                name, tagline = split_riot_id(riot_id)
                players.append({
                    "name": name or riot_id,
                    "tagline": tagline or "",
                    "team": lp.get("team"),
                    "champion": lp.get("champion"),
                    "level": lp.get("level"),
                    "kills": lp.get("kills"),
                    "deaths": lp.get("deaths"),
                    "assists": lp.get("assists"),
                    "cs": lp.get("cs"),
                    "is_dead": lp.get("is_dead"),
                    "respawn_timer": lp.get("respawn_timer"),
                    "is_bot": lp.get("is_bot"),
                    # From the pre-game snapshot; absent if we never scouted them.
                    "rank": scouted.get("rank"),
                    "trends": scouted.get("trends"),
                })
            return {
                "state": "in_game",
                "players": players,
                "region": region,
                "game_time": live["game_time"],
            }
        
        # Live Client API not up yet => loading screen
        # Try to get both teams from LCU gameflow first
        loading_info = get_loading_screen_players()
        print(f"[champ_select] loading_info={'present' if loading_info else 'None'}", flush=True)
        
        if loading_info:
            queue_id = loading_info["queue_id"]
            summoner_id_players = [p for p in loading_info["players"] if p.get("summoner_id")]
            puuid_players = [p for p in loading_info["players"] if p.get("puuid")]
            lcu_accounts = [get_lcu_summoner_by_id(p["summoner_id"]) for p in summoner_id_players]
            account_calls = [get_account_by_puuid(p["puuid"], riot_region) for p in puuid_players]
            accounts = await asyncio.gather(*account_calls) if account_calls else []
            
            valid_players = [
                {"name": p["name"], "tagline": p["tagline"], "team": p["team"]}
                for p in loading_info["players"]
                if p.get("name") and p.get("tagline")
            ]
            for p, acct in zip(summoner_id_players, lcu_accounts):
                if acct and acct.get("name") and acct.get("tagline"):
                    valid_players.append({"name": acct["name"], "tagline": acct["tagline"], "team": p["team"]})
            for p, acct in zip(puuid_players, accounts):
                if acct and acct.get("name") and acct.get("tagline"):
                    valid_players.append({"name": acct["name"], "tagline": acct["tagline"], "team": p["team"]})
            
            print(f"[champ_select] resolved {len(valid_players)}/{len(loading_info['players'])} loading identities", flush=True)
            
            info_calls = [
                get_player_info_solo(vp["name"], vp["tagline"], platform, riot_region, queue_id)
                for vp in valid_players
            ]
            results = await asyncio.gather(*info_calls)
            
            for r, vp in zip(results, valid_players):
                r["team"] = vp["team"]

            # Snapshot for the in-game view, which makes no API calls of its own.
            _scout_snapshot.update({f"{r['name']}#{r['tagline']}": r for r in results})

            return {
                "state": "loading",
                "players": results,
                "region": region,
                "queue_id": queue_id,
            }
        
        # Fallback: use cached champ select roster
        if _last_champ_select:
            cs_players = _last_champ_select["players"]
            queue_id = _last_champ_select["queue_id"]
            calls = [
                get_player_info_solo(p["name"], p["tagline"], platform, riot_region, queue_id)
                for p in cs_players
            ]
            results = await asyncio.gather(*calls)
            for r in results:
                r["team"] = "ORDER"
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

        champion_map = await get_champion_map()
        for p in cs_players:
            p["champion"] = champion_map.get(p.get("champion_id") or 0)

        calls = [
            get_player_info_solo(
                p["name"], p["tagline"], platform, riot_region, queue_id,
                current_champion=p.get("champion"),
            )
            for p in cs_players
        ]
        results = await asyncio.gather(*calls)
        for r, p in zip(results, cs_players):
            r["champion"] = p.get("champion")

        # Seed the snapshot early; the loading screen tops it up with the enemy team.
        _scout_snapshot.update({f"{r['name']}#{r['tagline']}": r for r in results})

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
