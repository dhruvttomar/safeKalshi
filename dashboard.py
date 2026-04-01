"""
Web dashboard for Kalshi Basketball Bot.

Runs the bot in a background thread and serves a live UI at http://localhost:5000

Usage:
    python dashboard.py

The bot does NOT start automatically — use the Start button in the UI.
"""

import logging
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

from flask import Flask, jsonify, request

import config
from bot import KalshiBasketballBot

# ---------------------------------------------------------------------------
# Log capture
# ---------------------------------------------------------------------------

LOG_BUFFER_SIZE = 300


class _DashboardLogHandler(logging.Handler):
    def __init__(self, buf: deque):
        super().__init__()
        self._buf = buf
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record):
        try:
            self._buf.append(
                {
                    "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                    "level": record.levelname,
                    "msg": self.format(record),
                }
            )
        except Exception:
            pass


_log_buffer: deque = deque(maxlen=LOG_BUFFER_SIZE)
_dash_handler = _DashboardLogHandler(_log_buffer)
logging.getLogger().addHandler(_dash_handler)

# ---------------------------------------------------------------------------
# Bot lifecycle
# ---------------------------------------------------------------------------

_bot: Optional[KalshiBasketballBot] = None
_bot_thread: Optional[threading.Thread] = None
_bot_error: Optional[str] = None
_start_time: Optional[float] = None
_state_lock = threading.Lock()


def _run_bot():
    global _bot_error
    try:
        _bot.run()
    except SystemExit:
        pass
    except Exception as exc:
        _bot_error = str(exc)


def _start_bot():
    global _bot, _bot_thread, _bot_error, _start_time
    with _state_lock:
        if _bot_thread and _bot_thread.is_alive():
            return False, "Bot is already running"
        _bot_error = None
        _bot = KalshiBasketballBot()
        _start_time = time.time()
        _bot_thread = threading.Thread(target=_run_bot, daemon=True, name="bot-main")
        _bot_thread.start()
        return True, "Bot started"


def _stop_bot():
    global _bot
    with _state_lock:
        if _bot is None or not (_bot_thread and _bot_thread.is_alive()):
            return False, "Bot is not running"
        _bot._running = False
        try:
            _bot._shutdown()
        except Exception:
            pass
        return True, "Bot stopping…"


def _get_state() -> dict:
    running = bool(
        _bot_thread and _bot_thread.is_alive() and _bot and _bot._running
    )
    is_demo = "demo" in config.KALSHI_API_BASE.lower()

    limits = {
        "max_daily_loss": config.MAX_DAILY_LOSS,
        "max_total_exposure": config.MAX_TOTAL_EXPOSURE,
        "max_exposure_per_market": config.MAX_EXPOSURE_PER_MARKET,
        "max_open_orders": config.MAX_OPEN_ORDERS,
        "scan_interval_sec": config.SCAN_INTERVAL_SEC,
        "kelly_multiplier": config.KELLY_MULTIPLIER,
        "min_implied_prob": config.MIN_IMPLIED_PROB,
        "max_buy_price": config.MAX_BUY_PRICE,
    }

    if _bot is None:
        return {
            "running": False,
            "is_demo": is_demo,
            "error": _bot_error,
            "balance": 0.0,
            "uptime_sec": 0,
            "risk": {
                "daily_pnl": 0.0,
                "total_exposure": 0.0,
                "open_order_count": 0,
                "position_count": 0,
                "daily_loss_limit_hit": False,
                "position_tickers": [],
                "exposure_by_ticker": {},
            },
            "limits": limits,
            "orders": [],
        }

    s = _bot.risk.state
    orders = []
    for o in _bot.orders.all_orders():
        orders.append(
            {
                "order_id": o.order_id,
                "ticker": o.ticker,
                "price": round(o.price, 2),
                "num_contracts": o.num_contracts,
                "filled_contracts": o.filled_contracts,
                "avg_fill_price": round(o.avg_fill_price, 2),
                "status": o.status.value,
                "exposure": round(o.exposure, 2),
                "submitted_at": datetime.fromtimestamp(o.submitted_at).strftime(
                    "%H:%M:%S"
                ),
                "age_sec": int(time.time() - o.submitted_at),
            }
        )

    orders.sort(key=lambda x: x["age_sec"])

    uptime = int(time.time() - _start_time) if _start_time else 0

    return {
        "running": running,
        "is_demo": is_demo,
        "error": _bot_error,
        "balance": round(_bot._bankroll, 2),
        "uptime_sec": uptime,
        "risk": {
            "daily_pnl": round(s.daily_pnl, 2),
            "total_exposure": round(s.total_exposure, 2),
            "open_order_count": s.open_order_count,
            "position_count": len(s.position_tickers),
            "daily_loss_limit_hit": _bot.risk.daily_loss_limit_hit,
            "position_tickers": sorted(s.position_tickers),
            "exposure_by_ticker": {k: round(v, 2) for k, v in s.exposure_by_ticker.items()},
        },
        "limits": limits,
        "orders": orders,
    }


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/")
def index():
    return _HTML


@app.route("/api/state")
def api_state():
    return jsonify(_get_state())


@app.route("/api/logs")
def api_logs():
    since = int(request.args.get("since", 0))
    logs = list(_log_buffer)
    return jsonify({"logs": logs[since:], "total": len(logs)})


@app.route("/api/start", methods=["POST"])
def api_start():
    ok, msg = _start_bot()
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@app.route("/api/stop", methods=["POST"])
def api_stop():
    ok, msg = _stop_bot()
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


# ---------------------------------------------------------------------------
# Embedded dashboard HTML
# ---------------------------------------------------------------------------

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Basketball Bot</title>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #222536;
    --border: #2e3148;
    --text: #e2e8f0;
    --muted: #8892a4;
    --green: #22c55e;
    --red: #ef4444;
    --yellow: #f59e0b;
    --blue: #3b82f6;
    --purple: #a855f7;
    --accent: #4f7cff;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
    min-height: 100vh;
  }

  /* ── Header ── */
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 14px 24px;
    display: flex;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
  }

  header h1 {
    font-size: 18px;
    font-weight: 700;
    letter-spacing: -0.3px;
    flex: 1;
  }

  header h1 span { color: var(--accent); }

  .badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.3px;
  }

  .badge-running { background: #14532d; color: var(--green); }
  .badge-stopped { background: #3f1515; color: var(--red); }
  .badge-demo    { background: #1e3a5f; color: #60a5fa; }
  .badge-prod    { background: #451a03; color: var(--yellow); }

  .dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: currentColor;
    animation: pulse 1.5s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }

  .badge-stopped .dot { animation: none; }

  #uptime { color: var(--muted); font-size: 12px; }

  .btn {
    padding: 8px 18px;
    border-radius: 8px;
    border: none;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.15s;
  }
  .btn:hover { opacity: 0.85; }
  .btn-start { background: var(--green); color: #000; }
  .btn-stop  { background: var(--red);   color: #fff; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }

  /* ── Main layout ── */
  main { padding: 20px 24px; display: flex; flex-direction: column; gap: 20px; }

  /* ── Stat cards ── */
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 14px;
  }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px 20px;
  }

  .card-label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--muted);
    margin-bottom: 8px;
  }

  .card-value {
    font-size: 28px;
    font-weight: 700;
    line-height: 1;
  }

  .card-sub {
    margin-top: 6px;
    font-size: 11px;
    color: var(--muted);
  }

  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .neu { color: var(--text); }

  /* ── Progress bars ── */
  .risk-section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px 20px;
  }

  .risk-section h2 {
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: var(--muted);
    margin-bottom: 16px;
  }

  .risk-bars { display: flex; flex-direction: column; gap: 14px; }

  .risk-bar-row { display: flex; flex-direction: column; gap: 6px; }

  .risk-bar-header {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
  }

  .risk-bar-label { color: var(--text); font-weight: 500; }
  .risk-bar-val   { color: var(--muted); }

  .progress-track {
    height: 8px;
    background: var(--surface2);
    border-radius: 999px;
    overflow: hidden;
  }

  .progress-fill {
    height: 100%;
    border-radius: 999px;
    transition: width 0.4s ease;
    background: var(--accent);
  }

  .progress-fill.warn  { background: var(--yellow); }
  .progress-fill.danger { background: var(--red); }

  /* ── Section panels ── */
  .panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
  }

  .panel-header {
    padding: 14px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .panel-header h2 {
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: var(--muted);
    flex: 1;
  }

  /* ── Tab strip ── */
  .tabs { display: flex; gap: 4px; }

  .tab {
    padding: 5px 12px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    background: transparent;
    border: none;
    color: var(--muted);
    transition: background 0.15s, color 0.15s;
  }

  .tab.active { background: var(--surface2); color: var(--text); }
  .tab:hover:not(.active) { color: var(--text); }

  /* ── Orders table ── */
  .table-wrap { overflow-x: auto; }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }

  thead th {
    padding: 10px 16px;
    text-align: left;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: var(--muted);
    background: var(--surface2);
    border-bottom: 1px solid var(--border);
  }

  tbody tr {
    border-bottom: 1px solid var(--border);
    transition: background 0.1s;
  }

  tbody tr:last-child { border-bottom: none; }
  tbody tr:hover { background: var(--surface2); }

  td { padding: 10px 16px; }

  .status-pill {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
  }

  .status-open      { background: #1e3a5f; color: #60a5fa; }
  .status-filled    { background: #14532d; color: var(--green); }
  .status-cancelled { background: #292929; color: var(--muted); }
  .status-pending   { background: #3b2800; color: var(--yellow); }
  .status-rejected  { background: #3f1515; color: var(--red); }

  .empty-state {
    padding: 40px;
    text-align: center;
    color: var(--muted);
    font-size: 13px;
  }

  /* ── Log panel ── */
  #log-box {
    padding: 12px 16px;
    height: 280px;
    overflow-y: auto;
    font-family: "SF Mono", "Fira Code", monospace;
    font-size: 12px;
    line-height: 1.6;
  }

  .log-line { display: flex; gap: 10px; }
  .log-time { color: var(--muted); flex-shrink: 0; }
  .log-msg  { white-space: pre-wrap; word-break: break-all; }

  .log-INFO    .log-msg { color: var(--text); }
  .log-DEBUG   .log-msg { color: var(--muted); }
  .log-WARNING .log-msg { color: var(--yellow); }
  .log-ERROR   .log-msg { color: var(--red); }
  .log-CRITICAL .log-msg { color: var(--red); font-weight: 700; }

  /* ── Error banner ── */
  #error-banner {
    background: #3f1515;
    border: 1px solid #7f1d1d;
    color: var(--red);
    border-radius: 10px;
    padding: 12px 16px;
    font-size: 13px;
    display: none;
  }

  /* ── Positions panel ── */
  .tag {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 500;
    background: var(--surface2);
    color: var(--muted);
    margin: 2px;
  }

  .positions-wrap { padding: 14px 20px; display: flex; flex-wrap: wrap; gap: 6px; }

  /* ── Log controls ── */
  .log-controls {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .log-toggle {
    font-size: 11px;
    padding: 3px 8px;
    border-radius: 5px;
    border: 1px solid var(--border);
    background: var(--surface2);
    color: var(--muted);
    cursor: pointer;
  }

  .log-toggle.active { border-color: var(--accent); color: var(--accent); }
</style>
</head>
<body>

<header>
  <h1>Kalshi <span>Basketball Bot</span></h1>
  <span id="status-badge" class="badge badge-stopped"><span class="dot"></span>Stopped</span>
  <span id="env-badge" class="badge badge-demo">DEMO</span>
  <span id="uptime"></span>
  <button id="start-btn" class="btn btn-start" onclick="startBot()">Start Bot</button>
  <button id="stop-btn"  class="btn btn-stop"  onclick="stopBot()" disabled>Stop Bot</button>
</header>

<main>

  <div id="error-banner"></div>

  <!-- Stat cards -->
  <div class="cards">
    <div class="card">
      <div class="card-label">Balance</div>
      <div class="card-value neu" id="card-balance">$0.00</div>
      <div class="card-sub">Account balance</div>
    </div>
    <div class="card">
      <div class="card-label">Daily P&amp;L</div>
      <div class="card-value neu" id="card-pnl">$0.00</div>
      <div class="card-sub" id="card-pnl-sub">Today's realized P&L</div>
    </div>
    <div class="card">
      <div class="card-label">Total Exposure</div>
      <div class="card-value neu" id="card-exposure">$0.00</div>
      <div class="card-sub" id="card-exposure-sub">Capital at risk</div>
    </div>
    <div class="card">
      <div class="card-label">Open Orders</div>
      <div class="card-value neu" id="card-orders">0</div>
      <div class="card-sub" id="card-orders-sub">0 filled positions</div>
    </div>
  </div>

  <!-- Risk bars -->
  <div class="risk-section">
    <h2>Risk Gauges</h2>
    <div class="risk-bars">
      <div class="risk-bar-row">
        <div class="risk-bar-header">
          <span class="risk-bar-label">Daily Loss</span>
          <span class="risk-bar-val" id="risk-loss-label">$0 / $25 limit</span>
        </div>
        <div class="progress-track">
          <div class="progress-fill" id="risk-loss-bar" style="width:0%"></div>
        </div>
      </div>
      <div class="risk-bar-row">
        <div class="risk-bar-header">
          <span class="risk-bar-label">Portfolio Exposure</span>
          <span class="risk-bar-val" id="risk-exp-label">$0 / $500 limit</span>
        </div>
        <div class="progress-track">
          <div class="progress-fill" id="risk-exp-bar" style="width:0%"></div>
        </div>
      </div>
      <div class="risk-bar-row">
        <div class="risk-bar-header">
          <span class="risk-bar-label">Open Orders</span>
          <span class="risk-bar-val" id="risk-orders-label">0 / 10 limit</span>
        </div>
        <div class="progress-track">
          <div class="progress-fill" id="risk-orders-bar" style="width:0%"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Orders table -->
  <div class="panel">
    <div class="panel-header">
      <h2>Orders</h2>
      <div class="tabs">
        <button class="tab active" onclick="setTab('all', this)">All</button>
        <button class="tab" onclick="setTab('open', this)">Open</button>
        <button class="tab" onclick="setTab('filled', this)">Filled</button>
        <button class="tab" onclick="setTab('cancelled', this)">Cancelled</button>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Ticker</th>
            <th>Contracts</th>
            <th>Price</th>
            <th>Filled</th>
            <th>Avg Fill</th>
            <th>Exposure</th>
            <th>Status</th>
            <th>Time</th>
            <th>Age</th>
          </tr>
        </thead>
        <tbody id="orders-tbody">
          <tr><td colspan="9" class="empty-state">No orders yet</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Positions -->
  <div class="panel">
    <div class="panel-header">
      <h2>Active Positions</h2>
    </div>
    <div class="positions-wrap" id="positions-wrap">
      <span class="tag" style="color: var(--muted)">None</span>
    </div>
  </div>

  <!-- Logs -->
  <div class="panel">
    <div class="panel-header">
      <h2>Live Logs</h2>
      <div class="log-controls">
        <button class="log-toggle active" id="autoscroll-btn" onclick="toggleAutoscroll()">Auto-scroll</button>
        <button class="log-toggle" onclick="clearLogs()">Clear</button>
      </div>
    </div>
    <div id="log-box"></div>
  </div>

</main>

<script>
let currentTab = 'all';
let allOrders = [];
let logOffset = 0;
let autoscroll = true;
let localLogs = [];

function fmt$(v) {
  const sign = v < 0 ? '-' : (v > 0 ? '+' : '');
  return (v < 0 ? '-' : '') + '$' + Math.abs(v).toFixed(2);
}

function fmtUptime(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return [h, m, s].map(n => String(n).padStart(2, '0')).join(':');
}

function fmtAge(sec) {
  if (sec < 60) return sec + 's';
  if (sec < 3600) return Math.floor(sec / 60) + 'm ' + (sec % 60) + 's';
  return Math.floor(sec / 3600) + 'h ' + Math.floor((sec % 3600) / 60) + 'm';
}

function setClass(el, cls) {
  el.className = el.className.replace(/\\bpos\\b|\\bneg\\b|\\bneu\\b/g, '').trim() + ' ' + cls;
}

function progressClass(pct) {
  if (pct >= 90) return 'danger';
  if (pct >= 60) return 'warn';
  return '';
}

function setBar(barId, labelId, value, limit, prefix='$') {
  const pct = Math.min(100, limit > 0 ? (Math.abs(value) / limit) * 100 : 0);
  const bar = document.getElementById(barId);
  const lbl = document.getElementById(labelId);
  bar.style.width = pct + '%';
  bar.className = 'progress-fill ' + progressClass(pct);
  lbl.textContent = prefix + Math.abs(value).toFixed(2) + ' / ' + prefix + limit + ' limit';
}

function renderOrders() {
  const tbody = document.getElementById('orders-tbody');
  let filtered = allOrders;
  if (currentTab !== 'all') {
    filtered = allOrders.filter(o => o.status === currentTab);
  }

  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No orders</td></tr>';
    return;
  }

  tbody.innerHTML = filtered.map(o => {
    const statusCls = 'status-' + o.status;
    return `<tr>
      <td style="font-weight:600;font-family:monospace">${o.ticker}</td>
      <td>${o.num_contracts}</td>
      <td>$${o.price.toFixed(2)}</td>
      <td>${o.filled_contracts}</td>
      <td>${o.avg_fill_price > 0 ? '$' + o.avg_fill_price.toFixed(2) : '—'}</td>
      <td>$${o.exposure.toFixed(2)}</td>
      <td><span class="status-pill ${statusCls}">${o.status}</span></td>
      <td>${o.submitted_at}</td>
      <td style="color:var(--muted)">${fmtAge(o.age_sec)}</td>
    </tr>`;
  }).join('');
}

function setTab(tab, btn) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderOrders();
}

function renderPositions(tickers) {
  const wrap = document.getElementById('positions-wrap');
  if (!tickers || tickers.length === 0) {
    wrap.innerHTML = '<span class="tag" style="color:var(--muted)">None</span>';
    return;
  }
  wrap.innerHTML = tickers.map(t => `<span class="tag">${t}</span>`).join('');
}

function updateState(state) {
  // Status badge
  const badge = document.getElementById('status-badge');
  const startBtn = document.getElementById('start-btn');
  const stopBtn = document.getElementById('stop-btn');

  if (state.running) {
    badge.className = 'badge badge-running';
    badge.innerHTML = '<span class="dot"></span>Running';
    startBtn.disabled = true;
    stopBtn.disabled = false;
  } else {
    badge.className = 'badge badge-stopped';
    badge.innerHTML = '<span class="dot"></span>Stopped';
    startBtn.disabled = false;
    stopBtn.disabled = true;
  }

  // Env badge
  const envBadge = document.getElementById('env-badge');
  if (state.is_demo) {
    envBadge.className = 'badge badge-demo';
    envBadge.textContent = 'DEMO';
  } else {
    envBadge.className = 'badge badge-prod';
    envBadge.textContent = 'PROD';
  }

  // Uptime
  document.getElementById('uptime').textContent =
    state.running ? 'Uptime: ' + fmtUptime(state.uptime_sec) : '';

  // Error banner
  const errBanner = document.getElementById('error-banner');
  if (state.error) {
    errBanner.style.display = 'block';
    errBanner.textContent = 'Error: ' + state.error;
  } else {
    errBanner.style.display = 'none';
  }

  // Cards
  const bal = document.getElementById('card-balance');
  bal.textContent = '$' + state.balance.toFixed(2);

  const pnl = state.risk.daily_pnl;
  const pnlEl = document.getElementById('card-pnl');
  pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
  setClass(pnlEl, pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'neu');
  if (state.risk.daily_loss_limit_hit) {
    document.getElementById('card-pnl-sub').textContent = '⚠ Daily loss limit hit';
    document.getElementById('card-pnl-sub').style.color = 'var(--red)';
  } else {
    document.getElementById('card-pnl-sub').textContent = "Today's realized P&L";
    document.getElementById('card-pnl-sub').style.color = '';
  }

  const expEl = document.getElementById('card-exposure');
  expEl.textContent = '$' + state.risk.total_exposure.toFixed(2);
  document.getElementById('card-exposure-sub').textContent =
    'Limit: $' + state.limits.max_total_exposure;

  const ordEl = document.getElementById('card-orders');
  ordEl.textContent = state.risk.open_order_count;
  document.getElementById('card-orders-sub').textContent =
    state.risk.position_count + ' filled position' + (state.risk.position_count !== 1 ? 's' : '');

  // Risk bars
  setBar('risk-loss-bar', 'risk-loss-label', -pnl, state.limits.max_daily_loss);
  setBar('risk-exp-bar', 'risk-exp-label', state.risk.total_exposure, state.limits.max_total_exposure);

  const orderPct = state.limits.max_open_orders > 0
    ? (state.risk.open_order_count / state.limits.max_open_orders) * 100 : 0;
  const orderBar = document.getElementById('risk-orders-bar');
  orderBar.style.width = Math.min(100, orderPct) + '%';
  orderBar.className = 'progress-fill ' + progressClass(orderPct);
  document.getElementById('risk-orders-label').textContent =
    state.risk.open_order_count + ' / ' + state.limits.max_open_orders + ' limit';

  // Orders
  allOrders = state.orders;
  renderOrders();

  // Positions
  renderPositions(state.risk.position_tickers);
}

// ── Logs ──

function appendLogs(newLogs) {
  const box = document.getElementById('log-box');
  newLogs.forEach(entry => {
    localLogs.push(entry);
    const div = document.createElement('div');
    div.className = 'log-line log-' + entry.level;
    div.innerHTML =
      '<span class="log-time">' + entry.time + '</span>' +
      '<span class="log-msg">' + escapeHtml(entry.msg) + '</span>';
    box.appendChild(div);
  });

  // Keep DOM lean
  while (box.children.length > 500) box.removeChild(box.firstChild);

  if (autoscroll && newLogs.length > 0) {
    box.scrollTop = box.scrollHeight;
  }
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function toggleAutoscroll() {
  autoscroll = !autoscroll;
  const btn = document.getElementById('autoscroll-btn');
  btn.classList.toggle('active', autoscroll);
}

function clearLogs() {
  document.getElementById('log-box').innerHTML = '';
  localLogs = [];
  logOffset = 0;
}

// ── Bot controls ──

async function startBot() {
  document.getElementById('start-btn').disabled = true;
  const res = await fetch('/api/start', { method: 'POST' });
  const data = await res.json();
  if (!data.ok) {
    document.getElementById('start-btn').disabled = false;
    alert(data.msg);
  }
}

async function stopBot() {
  document.getElementById('stop-btn').disabled = true;
  const res = await fetch('/api/stop', { method: 'POST' });
  const data = await res.json();
  if (!data.ok) {
    document.getElementById('stop-btn').disabled = false;
    alert(data.msg);
  }
}

// ── Polling ──

async function pollState() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    updateState(data);
  } catch (e) { /* server may be briefly restarting */ }
}

async function pollLogs() {
  try {
    const res = await fetch('/api/logs?since=' + logOffset);
    const data = await res.json();
    if (data.logs && data.logs.length > 0) {
      appendLogs(data.logs);
      logOffset = data.total;
    }
  } catch (e) {}
}

// Initial load + start polling
pollState();
pollLogs();
setInterval(pollState, 2000);
setInterval(pollLogs, 1500);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Dashboard running at http://localhost:8080")
    print("Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH before starting the bot.")
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
