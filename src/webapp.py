"""
Phase 6+: rich web dashboard (FastAPI).

Tabs: Overview (stats + equity curve), Positions, History, Watchlist, Logs
(debug/errors), and Settings. Everything reads from SQLite, auto-refreshes, and
shows exactly what the bot is doing and why.

Run:  python -m scripts.dashboard   (or: uvicorn src.webapp:app --host 0.0.0.0 --port 8000)
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Header
from fastapi.responses import HTMLResponse, JSONResponse

import config
from . import binance_data as bd
from . import broker
from . import database as db
from . import scanner
from . import signals

app = FastAPI(title="Top Gainers Bot")

_watchlist_cache: dict = {"ts": 0.0, "data": []}
_WATCHLIST_TTL = 60


def _clean(o):
    """Recursively replace NaN/Infinity floats with None so JSON never breaks."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def _minutes_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(iso)).total_seconds() / 60
    except Exception:
        return None


def _equity_curve(start_balance: float) -> list[dict]:
    closed = sorted(db.get_closed_trades(limit=100000),
                    key=lambda t: t["close_time"] or "")
    bal = start_balance
    points = [{"t": "start", "balance": round(bal, 4)}]
    for t in closed:
        bal += t["pnl"]
        points.append({"t": t["close_time"], "balance": round(bal, 4)})
    return points


def _settings_snapshot() -> dict:
    return {
        "Coin selection": {
            "Min 24h gain": f"{config.MIN_CHANGE_PCT}%",
            "Min 24h volume": f"${config.MIN_QUOTE_VOLUME:,.0f}",
            "Candle size": config.SCAN_INTERVAL,
            "Coins scanned": config.MAX_CANDIDATES,
        },
        "Flat base": {
            "Lookback candles": config.BASE_LOOKBACK,
            "Max base range": f"{config.MAX_BASE_RANGE_PCT}%",
            "Min pump size": f"{config.MIN_PUMP_PCT}%",
        },
        "Entry": {
            "RSI overbought": config.RSI_OVERBOUGHT,
            "Min above VWAP": f"{config.MIN_DIST_ABOVE_VWAP_PCT}%",
            "Confirmations needed": config.MIN_CONFIRMATIONS,
        },
        "Leverage & risk": {
            "Leverage": f"{config.LEVERAGE}x "
                        f"({'fixed' if config.USE_FIXED_LEVERAGE else 'dynamic'})",
            "Margin mode": config.MARGIN_MODE,
            "Margin per trade": f"{config.MARGIN_PER_TRADE_PCT}%",
            "Max open trades": config.MAX_CONCURRENT_TRADES,
        },
        "Exits": {
            "Max stop distance": f"{config.MAX_STOP_PCT}%",
            "Cash out 1": f"+{config.TP1_R:g}x margin (~${config.TP1_R*3:.0f})",
            "Cash out 2": f"+{config.TP2_R:g}x margin (~${config.TP2_R*3:.0f})",
            "Runner timeout": f"{config.RUNNER_TIMEOUT_MINUTES} min",
            "Trailing": f"{config.TRAIL_PCT}%",
            "Time stop": f"{config.TIME_STOP_MINUTES} min",
        },
        "Safety": {
            "Daily max loss": f"{config.DAILY_MAX_LOSS_PCT}%",
            "Account kill switch": f"{config.ACCOUNT_KILL_SWITCH_PCT}%",
        },
        "Video filters": {
            "Min funding rate": f"{config.MIN_FUNDING_RATE*100:.3f}%",
            "BTC dump threshold": f"{config.BTC_DUMP_PCT}%",
            "Wick/body ratio": config.WICK_BODY_RATIO,
        },
    }


@app.get("/api/state")
def api_state() -> JSONResponse:
    db.init_db()
    acct = db.get_account()

    opens = db.get_open_trades()
    for t in opens:
        try:
            price = bd.get_price(t["symbol"])
        except Exception:
            price = t["entry"]
        t["current"] = price
        t["unrealized"] = (t["entry"] - price) * t["remaining_qty"]
        t["unrealized_pct"] = (t["unrealized"] / t["margin"] * 100) if t["margin"] else 0
        t["minutes_open"] = _minutes_since(t["open_time"])
        t["to_stop_pct"] = (t["stop"] - price) / price * 100
        t["to_tp2_pct"] = (price - t["tp2"]) / price * 100
        t["to_liq_pct"] = (t["liq_price"] - price) / price * 100

    meta = {
        "cycles": int(db.meta_get("cycles", 0) or 0),
        "errors": int(db.meta_get("errors", 0) or 0),
        "last_cycle": db.meta_get("last_cycle"),
        "last_cycle_ago_s": (None if not db.meta_get("last_cycle")
                             else round((_minutes_since(db.meta_get("last_cycle")) or 0) * 60)),
        "started_at": db.meta_get("started_at"),
        "uptime_min": _minutes_since(db.meta_get("started_at")),
        "btc_regime": db.meta_get("btc_regime", "—"),
        "last_scan_count": int(db.meta_get("last_scan_count", 0) or 0),
        "paused": int(db.meta_get("paused", 0) or 0),
        "admin_enabled": bool(config.ADMIN_KEY),
    }

    return JSONResponse(_clean({
        "account": acct,
        "net": acct["balance"] - acct["start_balance"],
        "net_pct": (acct["balance"] - acct["start_balance"]) / acct["start_balance"] * 100,
        "open": opens,
        "closed": db.get_closed_trades(limit=100),
        "stats": db.stats(acct["start_balance"]),
        "equity": _equity_curve(acct["start_balance"]),
        "meta": meta,
        "settings": _settings_snapshot(),
    }))


@app.get("/api/logs")
def api_logs(level: str = "all", limit: int = 200) -> JSONResponse:
    db.init_db()
    return JSONResponse({"logs": db.get_logs(limit=limit, level=level)})


@app.get("/api/watchlist")
def api_watchlist() -> JSONResponse:
    now = time.time()
    if now - _watchlist_cache["ts"] > _WATCHLIST_TTL:
        rows = []
        try:
            for c in scanner.scan():
                sig = signals.evaluate(c)
                checks = [
                    {"name": "Flat base", "ok": "flat-base" in c.flags},
                    {"name": "Over-extended",
                     "ok": "not over-extended/overbought" not in sig.blockers},
                    {"name": "Not in uptrend", "ok": "STRONG-UPTREND" not in c.flags},
                    {"name": "Funding ok",
                     "ok": not any("funding" in b for b in sig.blockers)},
                    {"name": "BTC ok", "ok": not any("BTC" in b for b in sig.blockers)},
                    {"name": f"Signals {len(sig.reasons)}/{config.MIN_CONFIRMATIONS}",
                     "ok": len(sig.reasons) >= config.MIN_CONFIRMATIONS},
                ]
                rows.append({
                    "symbol": c.symbol, "score": c.score, "change_pct": c.change_pct,
                    "rsi": round(c.rsi), "dist_vwap": round(c.dist_vwap_pct, 1),
                    "funding": c.funding, "flags": c.flags,
                    "shortable": sig.should_short, "note": sig.summary(),
                    "blockers": sig.blockers, "reasons": sig.reasons, "checks": checks,
                })
        except Exception as e:
            rows = [{"symbol": "scan error", "note": str(e), "checks": []}]
        _watchlist_cache.update(ts=now, data=rows)
    return JSONResponse(_clean({"watchlist": _watchlist_cache["data"],
                                "age": round(now - _watchlist_cache["ts"])}))


@app.post("/api/admin/{action}")
def api_admin(action: str, x_admin_key: str = Header(None)) -> JSONResponse:
    if not config.ADMIN_KEY:
        return JSONResponse(
            {"ok": False, "error": "Admin disabled — set the ADMIN_KEY env var."},
            status_code=403)
    if x_admin_key != config.ADMIN_KEY:
        return JSONResponse({"ok": False, "error": "Wrong admin key."}, status_code=403)

    db.init_db()
    if action == "reset":
        db.reset_account()
        db.log("info", "admin", "Account reset to starting balance.")
        return JSONResponse({"ok": True, "msg": "Balance reset and trades cleared."})
    if action == "clear_logs":
        db.clear_logs()
        return JSONResponse({"ok": True, "msg": "Logs cleared."})
    if action == "pause":
        db.meta_set("paused", 1)
        db.log("info", "admin", "Trading paused by admin.")
        return JSONResponse({"ok": True, "msg": "Paused — no new trades will open."})
    if action == "resume":
        db.meta_set("paused", 0)
        db.log("info", "admin", "Trading resumed by admin.")
        return JSONResponse({"ok": True, "msg": "Resumed."})
    if action == "close_all":
        acct = db.get_account()
        n = 0
        for t in db.get_open_trades():
            try:
                price = bd.get_price(t["symbol"])
            except Exception:
                price = t["entry"]
            broker.force_close(t, price, acct)
            n += 1
        db.log("info", "admin", f"Admin closed {n} open trade(s).")
        return JSONResponse({"ok": True, "msg": f"Closed {n} open trade(s)."})
    return JSONResponse({"ok": False, "error": "Unknown action."}, status_code=400)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _PAGE


_PAGE = r"""
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Top Gainers Bot</title>
<style>
  :root{--bg:#0b0e14;--card:#151a23;--card2:#1b212c;--line:#28303d;--fg:#e6edf3;
        --mut:#8b949e;--grn:#3fb950;--red:#f85149;--acc:#388bfd;--warn:#d29922;--pur:#a371f7}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font-family:system-ui,Segoe UI,Roboto,sans-serif;font-size:14px}
  a{color:var(--acc)}
  header{display:flex;align-items:center;gap:14px;padding:12px 20px;flex-wrap:wrap;
         border-bottom:1px solid var(--line);background:var(--card)}
  header h1{font-size:16px;margin:0;white-space:nowrap}
  .pill{padding:3px 10px;border-radius:999px;font-size:12px;border:1px solid var(--line);white-space:nowrap}
  .ok{color:var(--grn);border-color:var(--grn)} .bad{color:var(--red);border-color:var(--red)}
  .warn{color:var(--warn);border-color:var(--warn)} .muted{color:var(--mut)}
  .spacer{margin-left:auto}
  .wrap{padding:18px;max-width:1180px;margin:0 auto;display:grid;gap:16px}
  .grid{display:grid;gap:12px;grid-template-columns:repeat(4,1fr)}
  .grid6{display:grid;gap:12px;grid-template-columns:repeat(6,1fr)}
  @media(max-width:820px){.grid,.grid6{grid-template-columns:repeat(2,1fr)}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
  .kpi h3{font-size:11px;color:var(--mut);margin:0 0 6px;text-transform:uppercase;letter-spacing:.04em}
  .kpi .v{font-size:22px;font-weight:700} .kpi .s{font-size:12px;color:var(--mut);margin-top:2px}
  h2.sec{font-size:13px;color:var(--mut);margin:4px 0 0;text-transform:uppercase;letter-spacing:.05em}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:right;padding:8px;border-bottom:1px solid var(--line);white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--card)}
  .pos{color:var(--grn)} .neg{color:var(--red)} .big{font-size:26px;font-weight:800}
  .tag{display:inline-block;background:#222b38;border:1px solid var(--line);border-radius:6px;
       padding:1px 6px;margin:1px;font-size:11px;color:var(--mut)}
  .tabs{display:flex;gap:6px;flex-wrap:wrap}
  .tab{padding:7px 14px;border:1px solid var(--line);border-radius:9px;cursor:pointer;
       background:var(--card);color:var(--mut);font-size:13px}
  .tab.active{color:var(--fg);border-color:var(--acc);background:var(--card2)}
  .panel{display:none} .panel.active{display:grid;gap:14px}
  svg{width:100%;height:150px}
  .scroll{max-height:60vh;overflow:auto}
  .log{font-family:ui-monospace,Consolas,monospace;font-size:12px;display:flex;gap:8px;
       padding:5px 6px;border-bottom:1px solid var(--line)}
  .log .t{color:var(--mut);white-space:nowrap}
  .lv-error{color:var(--red)} .lv-trade{color:var(--grn)} .lv-warn{color:var(--warn)} .lv-info{color:var(--mut)}
  .seg{display:flex;gap:4px} .seg .tab{padding:4px 10px;font-size:12px}
  .short{color:var(--grn);font-weight:700}
  .setgrid{display:grid;gap:12px;grid-template-columns:repeat(3,1fr)}
  @media(max-width:820px){.setgrid{grid-template-columns:1fr}}
  .kv{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--line)}
  .kv span:first-child{color:var(--mut)}
</style></head><body>
<header>
  <h1>📉 Top Gainers Bot</h1>
  <span id="status" class="pill">…</span>
  <span class="pill muted" id="btc">BTC —</span>
  <span class="pill muted" id="cyc">cycles —</span>
  <span class="pill" id="err">errors —</span>
  <span class="spacer"></span>
  <span class="pill muted" id="last">—</span>
  <span class="pill muted" id="clock"></span>
</header>
<div class="wrap">
  <!-- top KPIs -->
  <div class="grid">
    <div class="card kpi"><h3>Balance</h3><div class="v" id="bal">—</div>
      <div class="s" id="net"></div></div>
    <div class="card kpi"><h3>Win rate</h3><div class="v" id="winrate">—</div>
      <div class="s" id="wl"></div></div>
    <div class="card kpi"><h3>Open / Max</h3><div class="v" id="open">—</div>
      <div class="s">positions</div></div>
    <div class="card kpi"><h3>Uptime</h3><div class="v" id="uptime">—</div>
      <div class="s" id="started"></div></div>
  </div>

  <div class="tabs" id="tabs">
    <div class="tab active" data-p="overview">📊 Overview</div>
    <div class="tab" data-p="positions">📂 Positions</div>
    <div class="tab" data-p="history">📜 History</div>
    <div class="tab" data-p="watchlist">👀 Watchlist</div>
    <div class="tab" data-p="logs">🐞 Logs &amp; Debug</div>
    <div class="tab" data-p="settings">⚙️ Settings</div>
    <div class="tab" data-p="admin">🛠️ Admin</div>
  </div>

  <!-- OVERVIEW -->
  <div class="panel active" id="p-overview">
    <div class="card"><h2 class="sec">Equity curve</h2>
      <svg id="curve" viewBox="0 0 600 150" preserveAspectRatio="none"></svg></div>
    <div class="grid6">
      <div class="card kpi"><h3>Profit factor</h3><div class="v" id="pf">—</div></div>
      <div class="card kpi"><h3>Avg win</h3><div class="v pos" id="avgw">—</div></div>
      <div class="card kpi"><h3>Avg loss</h3><div class="v neg" id="avgl">—</div></div>
      <div class="card kpi"><h3>Best</h3><div class="v pos" id="best">—</div></div>
      <div class="card kpi"><h3>Worst</h3><div class="v neg" id="worst">—</div></div>
      <div class="card kpi"><h3>Max drawdown</h3><div class="v" id="dd">—</div></div>
    </div>
    <div class="grid6">
      <div class="card kpi"><h3>Streak</h3><div class="v" id="streak">—</div></div>
      <div class="card kpi"><h3>Avg leverage</h3><div class="v" id="avglev">—</div></div>
      <div class="card kpi"><h3>Avg hold</h3><div class="v" id="hold">—</div></div>
      <div class="card kpi"><h3>Total fees</h3><div class="v" id="fees">—</div></div>
      <div class="card kpi"><h3>Total trades</h3><div class="v" id="ntr">—</div></div>
      <div class="card kpi"><h3>Last scan</h3><div class="v" id="scan">—</div></div>
    </div>
    <div class="card"><h2 class="sec">Does the flat-base edge work?</h2>
      <table><thead><tr><th>Setup</th><th>Trades</th><th>Win%</th><th>P&L</th>
        <th>Profit factor</th></tr></thead><tbody id="fbcmp"></tbody></table></div>
  </div>

  <!-- POSITIONS -->
  <div class="panel" id="p-positions"><div class="card"><h2 class="sec">Open positions</h2>
    <table><thead><tr><th>Symbol</th><th>Lev</th><th>Entry</th><th>Now</th>
      <th>Unreal.</th><th>%</th><th>→Stop</th><th>→TP2</th><th>→Liq</th><th>Age</th></tr></thead>
      <tbody id="opent"></tbody></table></div></div>

  <!-- HISTORY -->
  <div class="panel" id="p-history"><div class="card"><h2 class="sec">Trade history</h2>
    <div class="scroll"><table><thead><tr><th>Symbol</th><th>Lev</th><th>Entry</th>
      <th>Exit</th><th>Reason</th><th>P&L</th><th>%</th><th>Base</th></tr></thead>
      <tbody id="histt"></tbody></table></div></div></div>

  <!-- WATCHLIST -->
  <div class="panel" id="p-watchlist"><div class="card">
    <h2 class="sec">Watchlist — top gainers, ranked <span id="wlage" class="muted"></span></h2>
    <p class="muted" style="margin:4px 0 10px">Green ✓ = condition met, red ✗ = why it's NOT being shorted.
      A coin is only shorted when every check is green.</p>
    <div class="scroll"><table><thead><tr><th>Symbol</th><th>24h%</th><th>RSI</th>
      <th>Verdict</th><th>Why (checks)</th></tr></thead>
      <tbody id="wlt"></tbody></table></div></div></div>

  <!-- LOGS -->
  <div class="panel" id="p-logs"><div class="card">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
      <h2 class="sec" style="margin:0">Logs &amp; debug</h2>
      <div class="seg" id="logfilter">
        <div class="tab active" data-l="all">All</div>
        <div class="tab" data-l="trade">Trades</div>
        <div class="tab" data-l="error">Errors</div>
        <div class="tab" data-l="info">Info</div>
      </div></div>
    <div class="scroll" id="logs"></div></div></div>

  <!-- SETTINGS -->
  <div class="panel" id="p-settings"><div class="card"><h2 class="sec">Current settings (config.py)</h2>
    <div class="setgrid" id="settings"></div></div></div>

  <!-- ADMIN -->
  <div class="panel" id="p-admin"><div class="card">
    <h2 class="sec">Admin controls</h2>
    <p class="muted" id="adminstate"></p>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0 14px">
      <input id="adminkey" type="password" placeholder="enter admin key"
        style="padding:9px 12px;background:var(--card2);border:1px solid var(--line);border-radius:9px;color:var(--fg);min-width:200px">
      <button class="tab" onclick="saveKey()">💾 Save key</button>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="tab" onclick="admin('pause')">⏸ Pause trading</button>
      <button class="tab" onclick="admin('resume')">▶️ Resume</button>
      <button class="tab" onclick="admin('close_all')">✋ Close all trades</button>
      <button class="tab" onclick="admin('reset')">♻️ Reset balance</button>
      <button class="tab" onclick="admin('clear_logs')">🧹 Clear logs</button>
    </div>
    <p id="adminmsg" style="margin-top:12px"></p>
  </div></div>
</div>
<script>
const $=s=>document.querySelector(s), $$=s=>document.querySelectorAll(s);
const money=n=>(n>=0?'+':'')+'$'+Math.abs(n).toFixed(2);
const cls=n=>n>=0?'pos':'neg';
const pr=n=>n==null?'—':(+n).toPrecision(5);
const ago=s=>s==null?'—':(s<60?s+'s ago':Math.floor(s/60)+'m ago');
const dur=m=>m==null?'—':(m<60?Math.round(m)+'m':(m/60).toFixed(1)+'h');
let logLevel='all';

// tabs
$('#tabs').onclick=e=>{const t=e.target.closest('.tab');if(!t)return;
  $$('#tabs .tab').forEach(x=>x.classList.remove('active'));t.classList.add('active');
  $$('.panel').forEach(p=>p.classList.remove('active'));
  $('#p-'+t.dataset.p).classList.add('active');};
$('#logfilter').onclick=e=>{const t=e.target.closest('.tab');if(!t)return;
  $$('#logfilter .tab').forEach(x=>x.classList.remove('active'));t.classList.add('active');
  logLevel=t.dataset.l;refreshLogs();};

function curve(points){
  const svg=$('#curve');svg.innerHTML='';if(points.length<2)return;
  const ys=points.map(p=>p.balance),mn=Math.min(...ys),mx=Math.max(...ys);
  const pad=(mx-mn)*0.1||1,lo=mn-pad,hi=mx+pad;
  const X=i=>i/(points.length-1)*600,Y=v=>150-((v-lo)/(hi-lo))*150;
  const d=points.map((p,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(p.balance).toFixed(1)).join(' ');
  const up=ys[ys.length-1]>=ys[0],col=up?'#3fb950':'#f85149';
  const area=d+` L600 150 L0 150 Z`;
  svg.innerHTML=`<path d="${area}" fill="${col}" opacity="0.08"/>`+
    `<path d="${d}" fill="none" stroke="${col}" stroke-width="2"/>`+
    `<line x1="0" x2="600" y1="${Y(points[0].balance)}" y2="${Y(points[0].balance)}" stroke="#28303d" stroke-dasharray="4 4"/>`;
}

async function refresh(){
  try{
    const s=await (await fetch('/api/state')).json();
    const a=s.account,st=s.stats,m=s.meta;
    $('#bal').textContent='$'+a.balance.toFixed(2);
    $('#net').innerHTML='<span class="'+cls(s.net)+'">'+money(s.net)+' ('+s.net_pct.toFixed(1)+'%)</span>';
    $('#winrate').textContent=st.all.win_rate.toFixed(0)+'%';
    $('#wl').textContent=st.all.wins+'W / '+st.all.losses+'L · '+st.all.trades+' trades';
    $('#open').textContent=s.open.length+' / '+a_max(s);
    $('#uptime').textContent=dur(m.uptime_min);
    $('#started').textContent=m.started_at?('since '+m.started_at.slice(0,16).replace('T',' ')):'';

    // header pills
    let stp=$('#status');
    if(a.halted_kill){stp.textContent='⛔ SAFETY STOP';stp.className='pill bad';}
    else if(a.halted_daily){stp.textContent='🟧 DAILY STOP';stp.className='pill warn';}
    else if(m.paused){stp.textContent='⏸ PAUSED';stp.className='pill warn';}
    else{stp.textContent='🟢 RUNNING';stp.className='pill ok';}
    $('#adminstate').textContent=m.admin_enabled?
      'Admin is enabled. Enter your key once, then use the buttons.':
      '⚠️ Admin is OFF. Set an ADMIN_KEY env var on the server to enable these buttons.';
    $('#btc').textContent='BTC '+m.btc_regime;
    $('#cyc').textContent=m.cycles+' cycles';
    const ep=$('#err');ep.textContent=m.errors+' errors';ep.className='pill '+(m.errors>0?'bad':'muted');
    $('#last').textContent='last cycle '+ago(m.last_cycle_ago_s);

    // overview metrics
    curve(s.equity);
    $('#pf').textContent=(st.all.profit_factor==null)?(st.all.trades?'∞':'—'):st.all.profit_factor.toFixed(2);
    $('#avgw').textContent=money(st.all.avg_win);
    $('#avgl').textContent=money(st.all.avg_loss);
    $('#best').textContent=money(st.best);
    $('#worst').textContent=money(st.worst);
    $('#dd').textContent=st.max_drawdown.toFixed(1)+'%';
    $('#streak').innerHTML=st.streak>0?('<span class="pos">'+st.streak+' W</span>'):
      st.streak<0?('<span class="neg">'+(-st.streak)+' L</span>'):'—';
    $('#avglev').textContent=st.avg_leverage.toFixed(1)+'x';
    $('#hold').textContent=dur(st.avg_hold_min);
    $('#fees').textContent='$'+st.fees.toFixed(2);
    $('#ntr').textContent=st.all.trades;
    $('#scan').textContent=m.last_scan_count+' coins';

    const cmp=(n,d)=>`<tr><td>${n}</td><td>${d.trades}</td><td>${d.win_rate.toFixed(0)}%</td>
      <td class="${cls(d.pnl)}">${money(d.pnl)}</td>
      <td>${d.profit_factor==null?(d.trades?'∞':'—'):d.profit_factor.toFixed(2)}</td></tr>`;
    $('#fbcmp').innerHTML=cmp('🟢 Flat-base',st.flat_base)+cmp('⚪ Other',st.non_flat_base);

    // positions
    $('#opent').innerHTML=s.open.length?s.open.map(t=>
      `<tr><td>${t.symbol}</td><td>${t.leverage}x</td><td>${pr(t.entry)}</td><td>${pr(t.current)}</td>
       <td class="${cls(t.unrealized)}">${money(t.unrealized)}</td>
       <td class="${cls(t.unrealized_pct)}">${t.unrealized_pct.toFixed(0)}%</td>
       <td>${t.to_stop_pct.toFixed(1)}%</td><td>${t.to_tp2_pct.toFixed(1)}%</td>
       <td>${t.to_liq_pct.toFixed(1)}%</td><td>${dur(t.minutes_open)}</td></tr>`).join('')
      :'<tr><td colspan="10" class="muted">No open positions right now</td></tr>';

    // history
    $('#histt').innerHTML=s.closed.length?s.closed.map(t=>
      `<tr><td>${t.symbol}</td><td>${t.leverage}x</td><td>${pr(t.entry)}</td>
       <td>${pr(t.exit)}</td><td class="muted">${t.close_reason||''}</td>
       <td class="${cls(t.pnl)}">${money(t.pnl)}</td>
       <td class="${cls(t.pnl_pct)}">${(t.pnl_pct||0).toFixed(0)}%</td>
       <td>${t.flat_base?'🟢':''}</td></tr>`).join('')
      :'<tr><td colspan="8" class="muted">No closed trades yet — the bot is being patient.</td></tr>';

    // settings
    $('#settings').innerHTML=Object.entries(s.settings).map(([grp,kv])=>
      `<div class="card2 card"><b>${grp}</b>`+Object.entries(kv).map(([k,v])=>
        `<div class="kv"><span>${k}</span><span>${v}</span></div>`).join('')+`</div>`).join('');
  }catch(e){$('#status').textContent='offline';$('#status').className='pill bad';}
  $('#clock').textContent=new Date().toUTCString().slice(17,25)+' UTC';
}
function a_max(s){return (s.settings&&s.settings['Leverage & risk'])?
  s.settings['Leverage & risk']['Max open trades']:'?';}

function badge(c){const col=c.ok?'var(--grn)':'var(--red)';
  return `<span class="tag" style="border-color:${col};color:${col}">${c.ok?'✓':'✗'} ${c.name}</span>`;}
async function refreshWatch(){
  try{const w=await (await fetch('/api/watchlist')).json();
    $('#wlt').innerHTML=w.watchlist.length?w.watchlist.map(r=>
      `<tr><td>${r.symbol}</td>
       <td>${r.change_pct?r.change_pct.toFixed(0)+'%':''}</td><td>${r.rsi??''}</td>
       <td class="${r.shortable?'short':'muted'}">${r.shortable?'✅ SHORT':'⛔ no'}</td>
       <td style="text-align:left;white-space:normal">${(r.checks||[]).map(badge).join(' ')||(r.note||'')}</td></tr>`).join('')
      :'<tr><td colspan="5" class="muted">No qualifying gainers right now</td></tr>';
    $('#wlage').textContent='· updated '+w.age+'s ago';
  }catch(e){}
}

function saveKey(){localStorage.setItem('adminkey',$('#adminkey').value);
  $('#adminmsg').innerHTML='<span class="pos">Key saved in this browser.</span>';}
async function admin(action){
  const key=localStorage.getItem('adminkey')||$('#adminkey').value;
  if(action==='reset'&&!confirm('Reset balance to start and DELETE all trades?'))return;
  if(action==='close_all'&&!confirm('Close all open trades now?'))return;
  try{const r=await fetch('/api/admin/'+action,{method:'POST',headers:{'x-admin-key':key}});
    const j=await r.json();
    $('#adminmsg').innerHTML=j.ok?('<span class="pos">✅ '+j.msg+'</span>')
      :('<span class="neg">❌ '+(j.error||'failed')+'</span>');
    refresh();refreshLogs();
  }catch(e){$('#adminmsg').innerHTML='<span class="neg">❌ '+e+'</span>';}
}

async function refreshLogs(){
  try{const j=await (await fetch('/api/logs?level='+logLevel)).json();
    $('#logs').innerHTML=j.logs.length?j.logs.map(l=>
      `<div class="log"><span class="t">${(l.ts||'').slice(11,19)}</span>
       <span class="lv-${l.level}">[${l.type}]</span><span>${l.message}</span></div>`).join('')
      :'<div class="muted" style="padding:10px">No log entries yet.</div>';
  }catch(e){}
}

const _sk=localStorage.getItem('adminkey');if(_sk)$('#adminkey').value=_sk;
refresh();refreshWatch();refreshLogs();
setInterval(refresh,5000);
setInterval(refreshWatch,30000);
setInterval(refreshLogs,7000);
</script></body></html>
"""
