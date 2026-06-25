"""
Phase 6: web dashboard (FastAPI).

A single page you can open from anywhere showing balance, P&L curve, open
positions (with live unrealized P&L), trade history, the current ranked
watchlist, and bot status. Reads everything from the SQLite database.

Run:  python -m scripts.dashboard   (or: uvicorn src.webapp:app --host 0.0.0.0 --port 8000)
"""
from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

import config
from . import binance_data as bd
from . import database as db
from . import scanner
from . import signals

app = FastAPI(title="Top Gainers Bot")

# cache the (slow, network-heavy) watchlist scan so page refreshes are cheap
_watchlist_cache: dict = {"ts": 0.0, "data": []}
_WATCHLIST_TTL = 60  # seconds


def _equity_curve(start_balance: float) -> list[dict]:
    closed = sorted(db.get_closed_trades(limit=100000),
                    key=lambda t: t["close_time"] or "")
    bal = start_balance
    points = [{"t": "start", "balance": round(bal, 4)}]
    for t in closed:
        bal += t["pnl"]
        points.append({"t": t["close_time"], "balance": round(bal, 4)})
    return points


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

    return JSONResponse({
        "account": acct,
        "net": acct["balance"] - acct["start_balance"],
        "open": opens,
        "closed": db.get_closed_trades(limit=50),
        "stats": db.stats(),
        "equity": _equity_curve(acct["start_balance"]),
        "config": {
            "leverage": config.LEVERAGE,
            "margin_mode": config.MARGIN_MODE,
            "max_concurrent": config.MAX_CONCURRENT_TRADES,
            "min_change_pct": config.MIN_CHANGE_PCT,
        },
    })


@app.get("/api/watchlist")
def api_watchlist() -> JSONResponse:
    now = time.time()
    if now - _watchlist_cache["ts"] > _WATCHLIST_TTL:
        rows = []
        try:
            for c in scanner.scan():
                sig = signals.evaluate(c)
                rows.append({
                    "symbol": c.symbol,
                    "score": c.score,
                    "change_pct": c.change_pct,
                    "rsi": round(c.rsi),
                    "dist_vwap": round(c.dist_vwap_pct, 1),
                    "funding": c.funding,
                    "flags": c.flags,
                    "shortable": sig.should_short,
                    "note": sig.summary(),
                })
        except Exception as e:
            rows = [{"symbol": "scan error", "note": str(e)}]
        _watchlist_cache.update(ts=now, data=rows)
    return JSONResponse({"watchlist": _watchlist_cache["data"],
                         "age": round(now - _watchlist_cache["ts"])})


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _PAGE


_PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Top Gainers Bot</title>
<style>
  :root{--bg:#0e1117;--card:#161b22;--line:#283142;--fg:#e6edf3;--mut:#8b949e;
        --grn:#2ea043;--red:#f85149;--acc:#388bfd;--warn:#d29922}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font-family:system-ui,Segoe UI,Roboto,sans-serif;font-size:14px}
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;
         border-bottom:1px solid var(--line)}
  header h1{font-size:16px;margin:0}
  .pill{padding:2px 10px;border-radius:999px;font-size:12px;border:1px solid var(--line)}
  .ok{color:var(--grn);border-color:var(--grn)}
  .bad{color:var(--red);border-color:var(--red)}
  .warn{color:var(--warn);border-color:var(--warn)}
  .wrap{padding:20px;display:grid;gap:16px;max-width:1100px;margin:0 auto}
  .row{display:grid;gap:16px;grid-template-columns:repeat(4,1fr)}
  @media(max-width:760px){.row{grid-template-columns:repeat(2,1fr)}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
  .card h2{font-size:12px;color:var(--mut);margin:0 0 8px;text-transform:uppercase;
           letter-spacing:.04em}
  .big{font-size:26px;font-weight:700}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:right;padding:7px 8px;border-bottom:1px solid var(--line)}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--mut);font-weight:600}
  .pos{color:var(--grn)} .neg{color:var(--red)}
  .tag{display:inline-block;background:#21262d;border:1px solid var(--line);
       border-radius:6px;padding:1px 6px;margin:1px;font-size:11px;color:var(--mut)}
  .muted{color:var(--mut)} svg{width:100%;height:120px}
  .short{color:var(--grn);font-weight:700}
</style></head><body>
<header>
  <h1>📉 Top Gainers Bot</h1>
  <span id="status" class="pill">…</span>
  <span class="pill muted" id="cfg"></span>
  <span class="pill muted" style="margin-left:auto" id="clock"></span>
</header>
<div class="wrap">
  <div class="row">
    <div class="card"><h2>Balance</h2><div class="big" id="bal">—</div>
      <div class="muted" id="net"></div></div>
    <div class="card"><h2>Closed trades</h2><div class="big" id="ntrades">—</div>
      <div class="muted" id="winrate"></div></div>
    <div class="card"><h2>Flat-base win%</h2><div class="big" id="fbwin">—</div>
      <div class="muted" id="fbpnl"></div></div>
    <div class="card"><h2>Open positions</h2><div class="big" id="nopen">—</div>
      <div class="muted">max """ + str(config.MAX_CONCURRENT_TRADES) + """</div></div>
  </div>

  <div class="card"><h2>Equity curve</h2><svg id="curve" viewBox="0 0 600 120"
      preserveAspectRatio="none"></svg></div>

  <div class="card"><h2>Open positions</h2>
    <table id="opent"><thead><tr><th>Symbol</th><th>Lev</th><th>Entry</th>
      <th>Now</th><th>Stop</th><th>Liq</th><th>Unreal. P&L</th></tr></thead>
      <tbody></tbody></table></div>

  <div class="card"><h2>Watchlist (top gainers, ranked)</h2>
    <table id="wlt"><thead><tr><th>Symbol</th><th>Score</th><th>24h%</th>
      <th>RSI</th><th>vsVWAP</th><th>Signal</th></tr></thead><tbody></tbody></table>
    <div class="muted" id="wlage"></div></div>

  <div class="card"><h2>Trade history</h2>
    <table id="histt"><thead><tr><th>Symbol</th><th>Lev</th><th>Entry</th>
      <th>Exit</th><th>Reason</th><th>P&L</th><th>Base</th></tr></thead>
      <tbody></tbody></table></div>
</div>
<script>
const $=s=>document.querySelector(s);
const money=n=>(n>=0?'+':'')+'$'+n.toFixed(2);
const cls=n=>n>=0?'pos':'neg';

function curve(points){
  const svg=$('#curve'); svg.innerHTML='';
  if(points.length<2){return;}
  const ys=points.map(p=>p.balance), mn=Math.min(...ys), mx=Math.max(...ys);
  const pad=(mx-mn)*0.1||1, lo=mn-pad, hi=mx+pad;
  const X=i=>i/(points.length-1)*600, Y=v=>120-((v-lo)/(hi-lo))*120;
  let d=points.map((p,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(p.balance).toFixed(1)).join(' ');
  const up=ys[ys.length-1]>=ys[0];
  const col=up?'#2ea043':'#f85149';
  svg.innerHTML=`<path d="${d}" fill="none" stroke="${col}" stroke-width="2"/>`+
    `<line x1="0" x2="600" y1="${Y(points[0].balance)}" y2="${Y(points[0].balance)}"`+
    ` stroke="#283142" stroke-dasharray="4 4"/>`;
}

async function refresh(){
  try{
    const s=await (await fetch('/api/state')).json();
    const a=s.account;
    $('#bal').textContent='$'+a.balance.toFixed(2);
    $('#net').innerHTML='<span class="'+cls(s.net)+'">'+money(s.net)+'</span> since start';
    $('#ntrades').textContent=s.stats.all.trades;
    $('#winrate').textContent=s.stats.all.win_rate.toFixed(0)+'% win · '+money(s.stats.all.pnl);
    $('#fbwin').textContent=s.stats.flat_base.win_rate.toFixed(0)+'%';
    $('#fbpnl').textContent=s.stats.flat_base.trades+' trades · '+money(s.stats.flat_base.pnl);
    $('#nopen').textContent=s.open.length;
    $('#cfg').textContent=s.config.leverage+'x '+s.config.margin_mode+' · ≥'+s.config.min_change_pct+'%';

    let st=$('#status');
    if(a.halted_kill){st.textContent='KILL SWITCH';st.className='pill bad';}
    else if(a.halted_daily){st.textContent='DAILY STOP';st.className='pill warn';}
    else{st.textContent='RUNNING';st.className='pill ok';}

    curve(s.equity);

    $('#opent').querySelector('tbody').innerHTML = s.open.length? s.open.map(t=>
      `<tr><td>${t.symbol}</td><td>${t.leverage}x</td><td>${(+t.entry).toPrecision(5)}</td>
       <td>${(+t.current).toPrecision(5)}</td><td>${(+t.stop).toPrecision(5)}</td>
       <td>${(+t.liq_price).toPrecision(5)}</td>
       <td class="${cls(t.unrealized)}">${money(t.unrealized)}</td></tr>`).join('')
      : '<tr><td colspan="7" class="muted">No open positions</td></tr>';

    $('#histt').querySelector('tbody').innerHTML = s.closed.length? s.closed.map(t=>
      `<tr><td>${t.symbol}</td><td>${t.leverage}x</td><td>${(+t.entry).toPrecision(5)}</td>
       <td>${t.exit?(+t.exit).toPrecision(5):'—'}</td><td class="muted">${t.close_reason||''}</td>
       <td class="${cls(t.pnl)}">${money(t.pnl)}</td>
       <td>${t.flat_base?'✓':''}</td></tr>`).join('')
      : '<tr><td colspan="7" class="muted">No closed trades yet</td></tr>';
  }catch(e){ $('#status').textContent='offline'; $('#status').className='pill bad'; }
  $('#clock').textContent=new Date().toUTCString().slice(17,25)+' UTC';
}

async function refreshWatch(){
  try{
    const w=await (await fetch('/api/watchlist')).json();
    $('#wlt').querySelector('tbody').innerHTML = w.watchlist.length? w.watchlist.map(r=>
      `<tr><td>${r.symbol}</td><td>${r.score??''}</td><td>${r.change_pct?r.change_pct.toFixed(0)+'%':''}</td>
       <td>${r.rsi??''}</td><td>${r.dist_vwap!=null?r.dist_vwap+'%':''}</td>
       <td class="${r.shortable?'short':'muted'}">${r.shortable?'SHORT':(r.note||'')}</td></tr>`).join('')
      : '<tr><td colspan="6" class="muted">No qualifying gainers right now</td></tr>';
    $('#wlage').textContent='updated '+w.age+'s ago';
  }catch(e){}
}

refresh(); refreshWatch();
setInterval(refresh, 5000);
setInterval(refreshWatch, 30000);
</script></body></html>
"""
