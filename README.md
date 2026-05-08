# MacScout

A native macOS desktop overlay for League of Legends that auto-detects every stage of the matchmaking flow and surfaces real-time stats — rank, winrate, recent match history, and live in-game data for every player in your lobby. Inspired by [Porofessor](https://porofessor.gg) and [OP.GG Desktop](https://op.gg/desktop), which don't ship Mac builds.

## Features

- **Auto-detects champ select** by reading the League Client lockfile — no login, no manual input
- **Pulls ranked data for all 10 players in parallel** from the Riot API, with a built-in caching layer that reduces external API calls by ~95%
- **Reads live in-game data** from the Live Client API (port 2999) when a match is active — KDA, level, items, team, respawn timers
- **Gameflow-aware overlay states** — automatically shows during champ select & loading screen, hides itself once you're actually playing, reappears post-game
- **Frameless, transparent, click-through Electron overlay** that floats over the game without intercepting input
- **Auto-detects region** from the running LCU client (NA, EUW, KR, EUNE supported)
- **Works in any queue** — auto-passes the queue ID through to filter recent matches by mode (Solo, Flex, Draft, ARAM)

## Tech stack

**Backend** — Python 3.13, FastAPI, httpx (async), asyncio, python-dotenv

**Frontend** — React 18 (Vite), JavaScript

**Desktop wrapper** — Electron, with `concurrently` + `wait-on` for dev workflow

**External integrations**
- LCU API (League Client UX) — local auth via lockfile, used for champ select detection, region detection, and gameflow phase
- Live Client API — local, no-auth endpoint at `https://127.0.0.1:2999`, used for live in-game data
- Riot Public API — used for puuid lookup, ranked entries, and match-v5 history

## Architecture

The backend exposes a single state-based endpoint (`/champ-select`) that returns one of four states:

| State | Trigger | Returned data |
|---|---|---|
| `idle` | LCU phase is `None`, `Lobby`, or `Matchmaking` | Empty player list |
| `champ_select` | LCU phase is `ChampSelect` | All 10 players with rank, winrate, recent matches |
| `loading` | LCU phase is `InProgress` AND Live Client API is unreachable | Cached champ select roster |
| `in_game` | LCU phase is `InProgress` AND Live Client API responds | Empty list (overlay hides) |

This three-API merge is the core of the project. The frontend polls one endpoint and the backend handles all the orchestration: lockfile reads, LCU auth, Live Client probing, and Riot API calls. Players are matched across data sources by their `name#tagline` Riot ID.

### Caching

A simple in-memory TTL cache wraps the two most expensive operations:

- **Player lookups** (puuid → ranked entries → recent matches) cache for 5 minutes
- **Match history** (match-v5 fan-out) caches for 10 minutes

Errors are deliberately not cached, so transient failures retry on the next poll. This is what keeps the overlay usable on Riot's dev key, which allows only ~100 requests per 2 minutes — without caching, a single 5-second poll cycle blows the limit.

### Pre-game vs in-game

The Electron overlay is a single window with conditional content. During champ select and loading screen it shows player cards; during actual gameplay (Live Client API responding) it renders nothing, ceding screen real estate back to the game. A planned in-game stats widget (CS/min, gold/min, vs rank average) will live in the same window in a future release.

## Running locally

Three processes need to be running:

**1. Backend** (FastAPI server, port 8000)
```bash
cd "MacScout"
uvicorn main:app --reload
```

**2. Frontend dev server** (Vite, port 5173)
```bash
cd "Overlay MacScout/frontend"
npm install   # first time only
npm run dev
```

**3. Electron overlay** (transparent floating window)
```bash
cd "MacScout/frontend"
npm run electron
```

You'll need a Riot API dev key from [developer.riotgames.com](https://developer.riotgames.com) in a `.env` file at the project root:

```
RIOT_API_KEY=RGAPI-your-key-here
```

Note that personal dev keys expire every 24 hours.

### Hotkeys

- `Cmd + Shift + H` — show/hide the overlay
- `Cmd + Shift + Q` — quit

## What's next

- **In-game performance widget** — CS/min, gold/min, vision/min, KP, level, each shown vs your rank's median (Porofessor-style)
- **Production Riot API key** to remove the 24h expiry
- **Champion icons & rank badges** instead of plain text
- **Settings UI** — region picker, hotkey customization, opacity slider
- **Backend deployment** to Railway/Fly.io for users who'd rather not run a local Python server
- **`.dmg` packaging** so the whole stack ships as a single installer

## Why this exists

Every major League of Legends stats overlay (OP.GG Desktop, Porofessor, Mobalytics) is Windows-only. Mac players have to alt-tab to a browser. This project closes that gap and was a chance to build something against three completely different APIs (a local websocket-ish auth flow, an undocumented localhost endpoint, and a public REST API).
