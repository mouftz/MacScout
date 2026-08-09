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

function App() {
    const [state, setState] = useState("idle");
    const [players, setPlayers] = useState([]);

    useEffect(() => {
        async function fetchState() {
            try {
                const response = await fetch("http://127.0.0.1:8000/champ-select");
                const data = await response.json();
                setState(data.state);
                setPlayers(data.players || []);
            } catch (err) {
                console.error("Backend not reachable", err);
            }
        }
        
        fetchState();
        const intervalMs = state === "loading" || state === "champ_select" ? 5000 : 15000;
        const interval = setInterval(fetchState, intervalMs);
        return () => clearInterval(interval);
    }, [state]);

    const sortedPlayers = [...players].sort((a, b) => {
        const teamOrder = { ORDER: 0, CHAOS: 1 };
        return (teamOrder[a.team] ?? 2) - (teamOrder[b.team] ?? 2);
    });

    return (
        <div className="cards-container">
            {state === "in_game" ? null : (
                <>
                    <div className="status-text">
                        {state === "idle" && "Waiting…"}
                        {state === "loading" && "Loading screen"}
                        {state === "champ_select" && "Champ select"}
                    </div>
                    {sortedPlayers.map((p, i) => (
                        <PlayerCard key={`${p.team || 'player'}-${p.name}-${p.tagline}-${i}`} player={p} />
                    ))}
                </>
            )}
        </div>
    );
}

export default App;
