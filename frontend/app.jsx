const { useState, useEffect, useCallback } = React;

const fmtUsd = (n) => n == null ? "—" :
  (n >= 0 ? "+" : "") + "$" + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtUsdPlain = (n) => n == null ? "—" :
  "$" + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPx = (n) => n == null ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: 4 });
const fmtPct = (n) => n == null ? "—" : (n * 100).toFixed(1) + "%";
const fmtTime = (ms) => {
  if (!ms) return "—";
  const d = new Date(ms);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
};
const ageStr = (s) => {
  if (s == null) return "—";
  if (s < 60) return Math.round(s) + "s";
  if (s < 3600) return Math.round(s / 60) + "m";
  return Math.round(s / 3600) + "h";
};

function App() {
  const [token, setToken] = useState("");
  const [status, setStatus] = useState(null);
  const [activity, setActivity] = useState([]);
  const [trades, setTrades] = useState({ paper: [], live: [] });
  const [walls, setWalls] = useState([]);
  const [feedHealth, setFeedHealth] = useState(null);
  const [report, setReport] = useState(null);
  const [rejectStats, setRejectStats] = useState([]);
  const [tab, setTab] = useState("overview");
  const [err, setErr] = useState(null);

  const fetchAll = useCallback(async () => {
    try {
      const [s, a, t, w, fh] = await Promise.all([
        fetch("/api/status").then(r => r.json()),
        fetch("/api/activity?limit=80").then(r => r.json()),
        fetch("/api/trades?limit=30").then(r => r.json()),
        fetch("/api/walls").then(r => r.json()),
        fetch("/api/feed_health").then(r => r.json()),
      ]);
      setStatus(s);
      setActivity(a.items || []);
      setTrades(t || { paper: [], live: [] });
      setWalls(w.items || []);
      setFeedHealth(fh);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  const fetchReport = useCallback(async () => {
    try {
      const r = await fetch("/api/report/daily?days=7").then(r => r.json());
      setReport(r);
    } catch (e) { console.error(e); }
  }, []);

  const fetchRejects = useCallback(async () => {
    try {
      const r = await fetch("/api/reject_stats?hours=24").then(r => r.json());
      setRejectStats(r.items || []);
    } catch (e) { console.error(e); }
  }, []);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 3000);
    return () => clearInterval(id);
  }, [fetchAll]);

  useEffect(() => {
    if (tab === "report") fetchReport();
    if (tab === "rejects") fetchRejects();
  }, [tab, fetchReport, fetchRejects]);

  const authedPost = async (path, body = {}) => {
    if (!token) { alert("Paste your API token first."); return; }
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) alert("Error: " + (j.detail || r.status));
    fetchAll();
    return j;
  };

  const cfg = status?.config || {};
  const rt = status?.runtime || {};
  const st = status?.state || {};
  const stats = status?.stats || {};

  const wsHealthy = st.ws_connected && (st.ws_age_s == null || st.ws_age_s < 30);
  const equity = st.paper_equity_usd ?? 0;
  const start = cfg.paper_starting_balance_usd ?? 0;
  const totalPnl = equity - start;
  const totalRet = start > 0 ? (totalPnl / start * 100) : 0;

  return (
    <div className="app">
      <header>
        <h1>BTC Microstructure Bot</h1>
        <div className="status-row">
          <span className="status-pill">
            <span className={`dot ${wsHealthy ? 'live' : 'bad'}`}></span>
            {wsHealthy ? "WS live" : "disconnected"}
            {st.ws_age_s != null && <span className="dim"> {ageStr(st.ws_age_s)} ago</span>}
          </span>
          {st.is_paused && (
            <span className="status-pill warn">
              <span className="dot warn"></span>
              paused: {st.pause_reason || "unknown"}
            </span>
          )}
          {rt.live_armed && (
            <span className="status-pill bad">
              <span className="dot bad"></span>LIVE ARMED
            </span>
          )}
          {!rt.live_armed && cfg.live_capable && (
            <span className="status-pill" style={{borderColor:'var(--warn)'}}>
              <span className="dot warn"></span>live capable, disarmed
            </span>
          )}
          {/* Liquidation feed status: explicit indicator */}
          {rt.liq_feed_status === "validated" && (
            <span className="status-pill" style={{borderColor:'var(--good)'}}>
              <span className="dot live"></span>liq feed: validated
            </span>
          )}
          {rt.liq_feed_status === "unknown" && (
            <span className="status-pill">
              <span className="dot"></span>liq feed: validating...
            </span>
          )}
          {rt.liq_feed_status === "unavailable" && (
            <span className="status-pill bad">
              <span className="dot bad"></span>liq feed UNAVAILABLE
            </span>
          )}
          {cfg.api_token_default && (
            <span className="status-pill bad">⚠ default API_TOKEN</span>
          )}
        </div>
      </header>

      {err && <div className="panel" style={{ borderColor: 'var(--bad)', color: 'var(--bad)' }}>{err}</div>}

      <div className="tabs">
        {["overview", "trades", "feed", "report", "rejects", "walls", "activity"].map(name => (
          <button key={name} className={`tab ${tab === name ? 'active' : ''}`} onClick={() => setTab(name)}>
            {name}
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <>
          <div className="grid">
            <Kpi label="Paper Equity" value={fmtUsdPlain(equity)}
              sub={<span style={{color: totalPnl >= 0 ? 'var(--good)' : 'var(--bad)'}}>
                {fmtUsd(totalPnl)} ({totalRet.toFixed(2)}%)
              </span>} />
            <Kpi label="Today's PnL" value={fmtUsd(st.daily_pnl_usd)}
              valueClass={(st.daily_pnl_usd ?? 0) >= 0 ? 'good' : 'bad'}
              sub={<span className="dim">{st.daily_pnl_date || "—"}</span>} />
            <Kpi label="Open / Max" value={`${stats.open_paper_trades ?? 0} / ${cfg.max_concurrent_positions}`}
              sub={<span className="dim">live: {stats.open_live_trades ?? 0}</span>} />
            <Kpi label="Signals 24h" value={`${stats.accepted_signals_24h ?? 0} / ${stats.signals_24h ?? 0}`}
              sub={<span className="dim">accepted / total</span>} />
            <Kpi label="Consec losses" value={st.consecutive_losses ?? 0}
              valueClass={(st.consecutive_losses ?? 0) >= 2 ? 'bad' : ''} />
            <Kpi label="Drawdown" value={(() => {
              const peak = st.paper_peak_equity_usd ?? start;
              const dd = peak > 0 ? (1 - equity / peak) * 100 : 0;
              return dd.toFixed(1) + "%";
            })()} />
          </div>

          <div className="panel">
            <h2>Strategies & Live Arming</h2>
            <div className="toggle-row">
              <Toggle name="Liquidation Fade" enabled={rt.liq_fade_enabled}
                disabled={rt.liq_feed_status === "unavailable"}
                onChange={(v) => authedPost("/api/toggle", { strategy: "liq", enabled: v })} />
              <Toggle name="Liquidity Wall (BTC)" enabled={rt.wall_enabled}
                onChange={(v) => authedPost("/api/toggle", { strategy: "wall", enabled: v })} />
              <Toggle name="LIVE ARMED" enabled={rt.live_armed} liveStyle
                disabled={!cfg.live_capable || cfg.require_protected_exits}
                onChange={(v) => {
                  if (v && !confirm(
                    "ARM live execution?\n\nReal orders will be placed on Hyperliquid.\n\n" +
                    "Confirm only if:\n" +
                    "1. You completed paper run with positive Sharpe\n" +
                    "2. Protected exits are implemented and tested\n" +
                    "3. You have small initial position limits set"
                  )) return;
                  authedPost("/api/toggle", { strategy: "live_armed", enabled: v });
                }} />
            </div>
            {rt.liq_feed_status === "unavailable" && (
              <div style={{marginTop:8, padding:10, background:'rgba(255,107,107,0.1)',
                           border:'1px solid var(--bad)', borderRadius:6,
                           color:'var(--bad)', fontSize:13}}>
                ⚠ Liquidation feed marked UNAVAILABLE — no liq-flagged trades seen during validation window.
                liq_fade is blocked. Inspect <code>raw_ws_samples</code> in the DB to verify feed shape,
                fix the parser if needed, then click below to clear:
                <div style={{marginTop:8}}>
                  <button onClick={() => {
                    if (!token) { alert("Paste API token first."); return; }
                    if (!confirm(
                      "Override feed status to 'validated'?\n\n" +
                      "Use only if you've confirmed liquidations DO arrive (e.g. checked raw_ws_samples)."
                    )) return;
                    fetch("/api/liq_feed/override", {
                      method: "POST",
                      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
                      body: JSON.stringify({ status: "validated" }),
                    }).then(r => r.json()).then(j => {
                      if (j.ok) { alert("feed status cleared to 'validated'"); fetchAll(); }
                      else alert("Error: " + (j.detail || "unknown"));
                    });
                  }}>Override → validated</button>
                </div>
              </div>
            )}
            {rt.liq_feed_status === "unknown" && (
              <div className="dim" style={{marginTop:8, fontSize:12}}>
                Liquidation feed validation in progress. Auto-decision after 1 hour
                of trade flow. Effective: liq_fade {rt.liq_fade_effective ? "running" : "blocked"}.
              </div>
            )}
            {!cfg.live_capable && (
              <div className="dim" style={{marginTop:8, fontSize:12}}>
                Live not capable: {cfg.live_init_error || "LIVE_CODE_ENABLED=false or keys missing"}
              </div>
            )}
            {cfg.require_protected_exits && (
              <div style={{marginTop:8, fontSize:12, color:'var(--warn)'}}>
                ⚠ REQUIRE_PROTECTED_EXITS=true: live arming is blocked until exchange-side stop/TP placement is implemented.
              </div>
            )}
            <div className="btns">
              {st.is_paused
                ? <button onClick={() => authedPost("/api/resume")}>Resume</button>
                : <button onClick={() => authedPost("/api/pause")}>Pause</button>}
              <button className="danger" onClick={() => {
                if (confirm("Emergency stop: close ALL open positions immediately and disarm live?"))
                  authedPost("/api/emergency_stop");
              }}>Emergency Stop</button>
            </div>
            <div className="token-input">
              <input type="password" placeholder="API token (Bearer)" value={token}
                onChange={e => setToken(e.target.value)} />
              <span className="dim" style={{fontSize: 11}}>required for controls</span>
            </div>
          </div>
        </>
      )}

      {tab === "trades" && (
        <>
          <div className="panel">
            <h2>Open Trades (Paper)</h2>
            <OpenTrades trades={trades.paper.filter(t => t.status === "open")} />
          </div>
          {trades.live.filter(t => t.status === "open").length > 0 && (
            <div className="panel">
              <h2>Open Trades (LIVE)</h2>
              <OpenTrades trades={trades.live.filter(t => t.status === "open")} live />
            </div>
          )}
          <div className="panel">
            <h2>Recent Closed (Paper)</h2>
            <ClosedTrades trades={trades.paper.filter(t => t.status === "closed").slice(0, 20)} />
          </div>
        </>
      )}

      {tab === "feed" && <FeedHealth fh={feedHealth} />}

      {tab === "report" && <Report report={report} />}

      {tab === "rejects" && <RejectStats items={rejectStats} />}

      {tab === "walls" && (
        <div className="panel">
          <h2>Tracked Walls (BTC)</h2>
          <Walls walls={walls} />
        </div>
      )}

      {tab === "activity" && (
        <div className="panel">
          <h2>Activity</h2>
          <ActivityFeed items={activity} />
        </div>
      )}
    </div>
  );
}

function Kpi({ label, value, sub, valueClass }) {
  return (
    <div className="kpi">
      <label>{label}</label>
      <div className={`v ${valueClass || ''}`}>{value}</div>
      {sub && <div style={{ fontSize: 11 }}>{sub}</div>}
    </div>
  );
}

function Toggle({ name, enabled, onChange, liveStyle, disabled }) {
  return (
    <label className={`toggle ${enabled ? 'on' : ''} ${liveStyle && enabled ? 'live-on' : ''} ${disabled ? 'disabled' : ''}`}>
      <input type="checkbox" checked={!!enabled} disabled={!!disabled}
        onChange={e => onChange(e.target.checked)} />
      {name}
    </label>
  );
}

function OpenTrades({ trades, live }) {
  if (!trades.length) return <div className="empty">No open positions</div>;
  return <div className="trades">
    {trades.map(t => (
      <div key={`${live ? 'L' : 'P'}-${t.id}`} className="trade-row">
        <span>{t.coin}</span>
        <span className={`dir ${t.direction}`}>{t.direction.toUpperCase()}</span>
        <span className="strategy dim">{t.strategy}</span>
        <span style={{ fontVariant: 'tabular-nums' }}>
          ${t.size_usd.toFixed(0)} @ {fmtPx(t.entry_fill_px)}
          <span className="dim" style={{ marginLeft: 8 }}>
            SL {fmtPx(t.stop_px)} · TP {fmtPx(t.target_px)}
          </span>
          {(t.mae_usd != null) && (
            <span className="dim" style={{ marginLeft: 8 }}>
              MAE {fmtUsd(t.mae_usd)} · MFE {fmtUsd(t.mfe_usd)}
            </span>
          )}
        </span>
        <span className="dim" style={{ fontSize: 11 }}>{fmtTime(t.opened_ms)}</span>
      </div>
    ))}
  </div>;
}

function ClosedTrades({ trades }) {
  if (!trades.length) return <div className="empty">No closed trades yet</div>;
  return <div className="trades">
    {trades.map(t => (
      <div key={t.id} className="trade-row">
        <span>{t.coin}</span>
        <span className={`dir ${t.direction}`}>{t.direction.toUpperCase()}</span>
        <span className="strategy dim">{t.exit_reason}</span>
        <span style={{ fontVariant: 'tabular-nums' }}>
          {fmtPx(t.entry_fill_px)} → {fmtPx(t.exit_px)}
        </span>
        <span className={`pnl ${t.pnl_usd >= 0 ? 'up' : 'dn'}`}>{fmtUsd(t.pnl_usd)}</span>
      </div>
    ))}
  </div>;
}

function Walls({ walls }) {
  if (!walls.length) return <div className="empty">No tracked walls</div>;
  const sorted = [...walls].sort((a, b) => b.detected_ms - a.detected_ms);
  return <div className="trades">
    {sorted.slice(0, 12).map(w => (
      <div key={w.wall_id} className="trade-row" style={{ gridTemplateColumns: '60px 70px 1fr 80px' }}>
        <span>{w.side === 'bid' ? '↓ BID' : '↑ ASK'}</span>
        <span style={{ color: w.state === 'confirmed' ? 'var(--good)' :
                              w.state === 'fired' ? 'var(--accent)' :
                              w.state === 'vanished' ? 'var(--bad)' : 'var(--fg-dim)' }}>
          {w.state}
        </span>
        <span style={{ fontVariant: 'tabular-nums' }}>
          @ ${w.px.toFixed(2)} · ${(w.size_usd_current / 1000).toFixed(0)}k of ${(w.size_usd_initial / 1000).toFixed(0)}k
        </span>
        <span className="dim" style={{ fontSize: 11 }}>{fmtTime(w.detected_ms)}</span>
      </div>
    ))}
  </div>;
}

function ActivityFeed({ items }) {
  if (!items.length) return <div className="empty">no activity yet</div>;
  return <div className="feed">
    {items.map(it => (
      <div key={it.id} className={`row ${it.level}`}>
        <span className="ts">{fmtTime(it.ts_ms)}</span>
        <span className="cat">{it.category}</span>
        <span>{it.message}</span>
      </div>
    ))}
  </div>;
}

function FeedHealth({ fh }) {
  if (!fh) return <div className="panel"><div className="empty">loading...</div></div>;
  const c = fh.counters || {};
  const liqRate = c.trades_total > 0 ? (c.trades_with_liq_field / c.trades_total) : 0;
  const liqShape = c.trades_liq_shape_dict > 0 ? "dict (with metadata)"
                 : c.trades_liq_shape_bool > 0 ? "bool flag"
                 : c.trades_liq_shape_other > 0 ? "other"
                 : "—";
  return (
    <>
      <div className="panel">
        <h2>Feed Validation</h2>
        <div className="grid">
          <Kpi label="Trades total" value={(c.trades_total ?? 0).toLocaleString()} />
          <Kpi label="Liq-flagged" value={(c.trades_with_liq_field ?? 0).toLocaleString()}
            sub={<span className="dim">{(liqRate * 100).toFixed(3)}%</span>}
            valueClass={c.trades_with_liq_field > 0 ? 'good' : 'bad'} />
          <Kpi label="Liq shape" value={liqShape} />
          <Kpi label="L2 updates" value={(c.l2_total ?? 0).toLocaleString()} />
          <Kpi label="Reconnects" value={c.reconnects ?? 0}
            valueClass={(c.reconnects ?? 0) > 5 ? 'bad' : ''} />
          <Kpi label="Parse errors" value={(c.trades_parse_errors ?? 0) + (c.l2_parse_errors ?? 0)}
            valueClass={((c.trades_parse_errors ?? 0) + (c.l2_parse_errors ?? 0)) > 0 ? 'warn' : ''} />
        </div>
        {c.trades_total > 100 && c.trades_with_liq_field === 0 && (
          <div style={{ marginTop: 12, padding: 10, background: 'rgba(255,107,107,0.1)',
                        border: '1px solid var(--bad)', borderRadius: 6, color: 'var(--bad)', fontSize: 13 }}>
            ⚠ NO liquidation-flagged trades seen yet. If this persists after 1 hour,
            liq_fade strategy will be auto-disabled. Inspect raw_ws_samples in DB to
            verify the feed shape.
          </div>
        )}
      </div>
      <div className="panel">
        <h2>Per-coin freshness</h2>
        <div className="trades">
          {Object.entries(fh.per_coin_last_trade_age_s || {}).map(([coin, age]) => (
            <div key={coin} className="trade-row" style={{ gridTemplateColumns: '80px 1fr' }}>
              <span>{coin}</span>
              <span className="dim">
                {age == null ? "no trades yet" : `${age.toFixed(1)}s ago`}
                {age != null && age > 60 && <span style={{ color: 'var(--warn)' }}> ⚠ stale</span>}
              </span>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

function Report({ report }) {
  if (!report) return <div className="panel"><div className="empty">loading...</div></div>;
  if (report.n_trades === 0) {
    return <div className="panel"><div className="empty">
      No closed paper trades in the last {report.window_days} days.
    </div></div>;
  }
  const o = report.overall;
  const isInf = (n) => !isFinite(n);
  return (
    <>
      <div className="panel">
        <h2>Report — last {report.window_days} day(s)</h2>
        <div className="grid">
          <Kpi label="Trades" value={o.n} />
          <Kpi label="Win rate" value={fmtPct(o.win_rate)}
            valueClass={o.win_rate >= 0.5 ? 'good' : ''} />
          <Kpi label="Net PnL" value={fmtUsd(o.net_pnl_usd)}
            valueClass={o.net_pnl_usd >= 0 ? 'good' : 'bad'} />
          <Kpi label="Profit factor"
            value={isInf(o.profit_factor) ? "∞" : o.profit_factor.toFixed(2)}
            valueClass={o.profit_factor >= 1.5 ? 'good' : o.profit_factor >= 1 ? '' : 'bad'} />
          <Kpi label="Avg win" value={fmtUsd(o.avg_win_usd)} />
          <Kpi label="Avg loss" value={fmtUsd(-o.avg_loss_usd)} valueClass="bad" />
          <Kpi label="Max DD" value={fmtUsd(-o.max_drawdown_usd)} valueClass="bad" />
          <Kpi label="Fees" value={fmtUsd(-o.fees_usd)} />
          <Kpi label="Avg MAE" value={fmtUsd(o.avg_mae_usd)} valueClass="bad" />
          <Kpi label="Avg MFE" value={fmtUsd(o.avg_mfe_usd)} valueClass="good" />
        </div>
      </div>
      <div className="panel">
        <h2>By strategy</h2>
        <BreakdownTable breakdown={report.by_strategy} />
      </div>
      <div className="panel">
        <h2>By coin</h2>
        <BreakdownTable breakdown={report.by_coin} />
      </div>
    </>
  );
}

function BreakdownTable({ breakdown }) {
  const rows = Object.entries(breakdown);
  if (!rows.length) return <div className="empty">no data</div>;
  return <div className="trades">
    {rows.map(([key, s]) => (
      <div key={key} className="trade-row" style={{ gridTemplateColumns: '120px 60px 80px 1fr 80px' }}>
        <span>{key}</span>
        <span className="dim">{s.n}</span>
        <span style={{ fontVariant: 'tabular-nums' }}>{fmtPct(s.win_rate)}</span>
        <span className="dim" style={{ fontSize: 12 }}>
          PF {isFinite(s.profit_factor) ? s.profit_factor.toFixed(2) : "∞"} ·
          MAE {fmtUsd(s.avg_mae_usd)} · MFE {fmtUsd(s.avg_mfe_usd)}
        </span>
        <span className={`pnl ${s.net_pnl_usd >= 0 ? 'up' : 'dn'}`}>{fmtUsd(s.net_pnl_usd)}</span>
      </div>
    ))}
  </div>;
}

function RejectStats({ items }) {
  if (!items.length) return <div className="panel"><div className="empty">no rejection data yet</div></div>;
  // Group by reason_short
  const byReason = {};
  for (const it of items) {
    const k = `${it.strategy}/${it.reason_short}`;
    byReason[k] = (byReason[k] || 0) + it.count;
  }
  const sorted = Object.entries(byReason).sort((a, b) => b[1] - a[1]).slice(0, 30);
  return (
    <div className="panel">
      <h2>Why signals didn't fire (last 24h)</h2>
      <div className="dim" style={{ fontSize: 12, marginBottom: 8 }}>
        Numbers in reasons replaced with #. Numeric thresholds are in env vars.
      </div>
      <div className="trades">
        {sorted.map(([key, count]) => {
          const [strat, reason] = key.split("/", 2);
          return (
            <div key={key} className="trade-row" style={{ gridTemplateColumns: '140px 1fr 80px' }}>
              <span className="dim" style={{ fontSize: 12 }}>{strat}</span>
              <span style={{ fontSize: 13 }}>{reason}</span>
              <span style={{ fontVariant: 'tabular-nums', textAlign: 'right' }}>{count.toLocaleString()}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
