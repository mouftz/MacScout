import { useState, useEffect } from 'react';
import './App.css';

function PlayerCard({ player }) {
    const matches = player.recent_matches;
    const trends = player.trends;
    const teamClass = player.team ? `team-${player.team.toLowerCase()}` : '';
    
    const rankClasses = ['player-rank'];
    if (player.rank === 'Unranked' || player.rank === 'Unknown') {
        rankClasses.push('unranked');
    }
    
    return (
        <div className={`player-card ${teamClass}`}>
            {trends?.tag && (
                <div className={`tag tag-${trends.tag_kind || 'generic'}`}>
                    {trends.tag}
                </div>
            )}
            <div className="player-name">
                {player.name}<span className="tagline">#{player.tagline}</span>
            </div>
            <div className={rankClasses.join(' ')}>{player.rank}</div>
            <div className="player-stats">
                {player.wins}W · {player.losses}L · {player.winrate}% WR
            </div>
            
            {trends?.avg_kda && (
                <div className="trends-section">
                    <div className="trend-row">
                        <span className="trend-label-sm">KDA</span>
                        <span className="trend-value">
                            {trends.avg_kda.kills} / {trends.avg_kda.deaths} / {trends.avg_kda.assists}
                            <span className="kda-ratio">  {trends.kda_ratio}</span>
                        </span>
                    </div>
                    {trends.avg_cs_per_min !== null && trends.avg_cs_per_min > 0 && (
                        <div className="trend-row">
                            <span className="trend-label-sm">CS/min</span>
                            <span className="trend-value">{trends.avg_cs_per_min}</span>
                        </div>
                    )}
                    {trends.mains.length > 0 && (
                        <div className="trend-row">
                            <span className="trend-label-sm">Mains</span>
                            <span className="trend-value">
                                {trends.mains.map(m => m.champion).join(' · ')}
                            </span>
                        </div>
                    )}
                </div>
            )}
            
            {matches.results.length > 0 && (
                <div className="recent-trend">
                    <div className="trend-dots">
                        {matches.results.map((win, i) => (
                            <span key={i} className={`dot ${win ? 'win' : 'loss'}`} />
                        ))}
                    </div>
                    <div className="trend-label">
                        Last {matches.results.length} · {matches.winrate}% WR
                    </div>
                </div>
            )}
        </div>
    );
}

function StatRow({ label, mine, avg, decimals = 1 }) {
    const better = mine != null && avg != null && mine >= avg;
    // Fix the decimals so the two columns stay comparable at a glance —
    // otherwise a whole-number KDA renders as "5" next to an average of "2.31".
    const fmt = v => (v == null ? '—' : v.toFixed(decimals));
    return (
        <div className="stat-row">
            <span className={`stat-mine ${better ? 'ahead' : 'behind'}`}>{fmt(mine)}</span>
            <span className="stat-avg">{fmt(avg)}</span>
            <span className="stat-label">{label}</span>
        </div>
    );
}

function InGamePanel({ you, avg, lobbyRank, gameTime }) {
    const mins = Math.floor(gameTime / 60);

    return (
        <div className="ingame-panel">
            <div className="panel-header">
                <span className="panel-vs">vs</span>
                <span className="panel-rank">{lobbyRank || 'Unranked lobby'}</span>
                <span className="panel-time">~{mins}m</span>
            </div>
            <div className="panel-cols">
                <span className="col-mine">Your</span>
                <span className="col-avg">Avg.</span>
                <span className="col-spacer" />
            </div>
            <StatRow label="CS/Min" mine={you?.cs_per_min} avg={avg?.cs_per_min} decimals={1} />
            <StatRow label="KDA" mine={you?.kda_ratio} avg={avg?.kda_ratio} decimals={2} />
        </div>
    );
}

function App() {
    const [state, setState] = useState("idle");
    const [players, setPlayers] = useState([]);
    const [gameTime, setGameTime] = useState(0);
    const [collapsed, setCollapsed] = useState(false);
    const [you, setYou] = useState(null);
    const [lobbyAvg, setLobbyAvg] = useState(null);
    const [lobbyRank, setLobbyRank] = useState(null);

    // Fired by the Cmd+Shift+C global shortcut in electron.cjs.
    useEffect(() => {
        const toggle = () => setCollapsed(c => !c);
        window.addEventListener("macscout:toggle-collapse", toggle);
        return () => window.removeEventListener("macscout:toggle-collapse", toggle);
    }, []);

    useEffect(() => {
        async function fetchState() {
            try {
                const response = await fetch("http://127.0.0.1:8000/champ-select");
                const data = await response.json();
                setState(data.state);
                setPlayers(data.players || []);
                setGameTime(data.game_time || 0);
                setYou(data.you || null);
                setLobbyAvg(data.lobby_avg || null);
                setLobbyRank(data.lobby_rank || null);
            } catch (err) {
                console.error("Backend not reachable", err);
            }
        }

        fetchState();
        // In-game reads only local APIs, so it can poll fast without cost.
        const intervalMs =
            state === "in_game" ? 2000
            : state === "loading" || state === "champ_select" ? 5000
            : 15000;
        const interval = setInterval(fetchState, intervalMs);
        return () => clearInterval(interval);
    }, [state]);

    const sortedPlayers = [...players].sort((a, b) => {
        const teamOrder = { ORDER: 0, CHAOS: 1 };
        return (teamOrder[a.team] ?? 2) - (teamOrder[b.team] ?? 2);
    });

    if (state === "in_game") {
        return <InGamePanel you={you} avg={lobbyAvg} lobbyRank={lobbyRank} gameTime={gameTime} />;
    }

    if (collapsed) {
        return (
            <div className="collapsed-pill">
                MacScout · {players.length} · ⌘⇧C
            </div>
        );
    }

    const order = sortedPlayers.filter(p => p.team === "ORDER");
    const chaos = sortedPlayers.filter(p => p.team === "CHAOS");
    // Champ select has no team split (Riot hides the enemy team), so those
    // players carry no team and render as a single column.
    const unteamed = sortedPlayers.filter(p => !p.team);

    const renderCard = (p, i) => (
        <PlayerCard key={`${p.team || 'player'}-${p.name}-${p.tagline}-${i}`} player={p} />
    );

    return (
        <div className="overlay-root">
            <div className="status-text">
                {state === "idle" && "Waiting…"}
                {state === "loading" && "Loading screen"}
                {state === "champ_select" && "Champ select"}
            </div>
            <div className="teams-layout">
                {order.length > 0 && (
                    <div className="team-column">{order.map(renderCard)}</div>
                )}
                {chaos.length > 0 && (
                    <div className="team-column">{chaos.map(renderCard)}</div>
                )}
                {unteamed.length > 0 && (
                    <div className="team-column">{unteamed.map(renderCard)}</div>
                )}
            </div>
        </div>
    );
}

export default App;
