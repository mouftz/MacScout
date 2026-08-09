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

function LiveRow({ player }) {
    const kda = `${player.kills}/${player.deaths}/${player.assists}`;
    const tag = player.trends?.tag;

    return (
        <div className={`live-row ${player.is_dead ? 'dead' : ''}`}>
            <span className="live-champ">{player.champion}</span>
            <span className="live-level">{player.level}</span>
            <span className="live-kda">{kda}</span>
            <span className="live-cs">{player.cs} cs</span>
            {player.rank && <span className="live-rank">{player.rank}</span>}
            {tag && <span className={`live-tag tag-${player.trends.tag_kind || 'generic'}`}>{tag}</span>}
            {player.is_dead && player.respawn_timer > 0 && (
                <span className="live-respawn">{Math.ceil(player.respawn_timer)}s</span>
            )}
        </div>
    );
}

function InGameOverlay({ players, gameTime }) {
    const order = players.filter(p => p.team === 'ORDER');
    const chaos = players.filter(p => p.team === 'CHAOS');
    const mins = Math.floor(gameTime / 60);
    const secs = String(Math.floor(gameTime % 60)).padStart(2, '0');

    return (
        <div className="ingame-overlay">
            <div className="live-clock">{mins}:{secs}</div>
            <div className="live-teams">
                <div className="live-team live-team-order">
                    {order.map((p, i) => <LiveRow key={`o-${p.name}-${i}`} player={p} />)}
                </div>
                <div className="live-team live-team-chaos">
                    {chaos.map((p, i) => <LiveRow key={`c-${p.name}-${i}`} player={p} />)}
                </div>
            </div>
        </div>
    );
}

function App() {
    const [state, setState] = useState("idle");
    const [players, setPlayers] = useState([]);
    const [gameTime, setGameTime] = useState(0);

    useEffect(() => {
        async function fetchState() {
            try {
                const response = await fetch("http://127.0.0.1:8000/champ-select");
                const data = await response.json();
                setState(data.state);
                setPlayers(data.players || []);
                setGameTime(data.game_time || 0);
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
        return <InGameOverlay players={players} gameTime={gameTime} />;
    }

    return (
        <div className="cards-container">
            <div className="status-text">
                {state === "idle" && "Waiting…"}
                {state === "loading" && "Loading screen"}
                {state === "champ_select" && "Champ select"}
            </div>
            {sortedPlayers.map((p, i) => (
                <PlayerCard key={`${p.team || 'player'}-${p.name}-${p.tagline}-${i}`} player={p} />
            ))}
        </div>
    );
}

export default App;
