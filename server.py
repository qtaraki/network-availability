#!/usr/bin/env python3
"""
Network health check - host server.

Receives heartbeats from one or more clients, stores them in SQLite, and serves
a dashboard showing uptime / downtime over the last 24h.

Outage detection is gap-based (a "dead man's switch"): the client is expected to
send a heartbeat every EXPECTED_INTERVAL seconds. If no heartbeat arrives for
OUTAGE_THRESHOLD seconds, that gap is treated as an outage, because when the
network is down the client cannot reach the host and the heartbeats simply stop
arriving.

Config via environment variables:
  NETCHECK_PORT             Port to listen on            (default 8080)
  NETCHECK_DB               SQLite file path             (default ./heartbeats.db)
  NETCHECK_INTERVAL         Expected client interval (s) (default 5)
  NETCHECK_OUTAGE_AFTER     Declare an outage when no heartbeat has arrived for
                            this many seconds            (default 10)
  NETCHECK_TOKEN            Shared secret; if set, clients must send it as
                            Authorization: Bearer <token>   (default: no auth)

Note: NETCHECK_OUTAGE_AFTER must be larger than NETCHECK_INTERVAL, otherwise
normal beats look like outages. A good rule is threshold >= 2x interval.
"""

import json
import os
import sqlite3
import time
from contextlib import closing

from flask import Flask, Response, g, jsonify, render_template_string, request

PORT = int(os.environ.get("NETCHECK_PORT", "8080"))
DB_PATH = os.environ.get("NETCHECK_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "heartbeats.db"))
EXPECTED_INTERVAL = float(os.environ.get("NETCHECK_INTERVAL", "5"))
# Declare an outage when no heartbeat has arrived for this many seconds.
OUTAGE_THRESHOLD = float(os.environ.get("NETCHECK_OUTAGE_AFTER", "10"))
TOKEN = os.environ.get("NETCHECK_TOKEN", "")

app = Flask(__name__)


# ---------------------------------------------------------------- database ---
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS heartbeats (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         REAL NOT NULL,          -- server receive time (epoch s)
                client_id  TEXT NOT NULL,
                latency_ms REAL                    -- client-measured round trip
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_hb_client_ts ON heartbeats (client_id, ts)")
        db.commit()


# ------------------------------------------------------------------- routes ---
@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    if TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {TOKEN}":
            return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    client_id = str(data.get("client_id", "default"))[:128]
    latency_ms = data.get("latency_ms")
    try:
        latency_ms = float(latency_ms) if latency_ms is not None else None
    except (TypeError, ValueError):
        latency_ms = None

    db = get_db()
    db.execute(
        "INSERT INTO heartbeats (ts, client_id, latency_ms) VALUES (?, ?, ?)",
        (time.time(), client_id, latency_ms),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/clients")
def api_clients():
    db = get_db()
    rows = db.execute(
        "SELECT client_id, MAX(ts) AS last FROM heartbeats GROUP BY client_id ORDER BY last DESC"
    ).fetchall()
    return jsonify([r["client_id"] for r in rows])


@app.route("/api/data")
def api_data():
    hours = float(request.args.get("hours", "24"))
    client = request.args.get("client")
    now = time.time()
    since = now - hours * 3600

    db = get_db()
    if not client:
        row = db.execute("SELECT client_id FROM heartbeats ORDER BY ts DESC LIMIT 1").fetchone()
        client = row["client_id"] if row else None

    points = []
    if client:
        rows = db.execute(
            "SELECT ts, latency_ms FROM heartbeats WHERE client_id = ? AND ts >= ? ORDER BY ts",
            (client, since),
        ).fetchall()
        points = [{"ts": r["ts"], "latency_ms": r["latency_ms"]} for r in rows]

    outages = compute_outages([p["ts"] for p in points], window_start=since, now=now)
    total_down = sum(o["duration"] for o in outages)
    window = now - since
    uptime_pct = round(100.0 * (1 - total_down / window), 4) if window > 0 else 100.0

    currently_down = bool(points) and (now - points[-1]["ts"] > OUTAGE_THRESHOLD)
    if not points:
        currently_down = None  # unknown, no data

    return jsonify(
        {
            "client": client,
            "now": now,
            "window_start": since,
            "interval": EXPECTED_INTERVAL,
            "threshold": OUTAGE_THRESHOLD,
            "points": points,
            "outages": outages,
            "total_downtime_s": round(total_down, 1),
            "uptime_pct": uptime_pct,
            "outage_count": len(outages),
            "currently_down": currently_down,
        }
    )


def compute_outages(timestamps, window_start, now):
    """Return list of {start, end, duration} for gaps larger than the threshold.

    Whenever no heartbeat arrives for more than OUTAGE_THRESHOLD seconds, the gap
    between the last good beat and the next one is recorded as an outage. The
    duration is the full time without a confirmed heartbeat. A trailing gap (no
    recent beats) counts as an ongoing outage up to `now`.
    """
    outages = []
    prev = None
    for ts in timestamps:
        if prev is not None and (ts - prev) > OUTAGE_THRESHOLD:
            outages.append({"start": prev, "end": ts, "duration": ts - prev})
        prev = ts

    # ongoing outage at the tail
    if prev is not None and (now - prev) > OUTAGE_THRESHOLD:
        outages.append({"start": prev, "end": now, "duration": now - prev, "ongoing": True})

    return outages


@app.route("/client.py")
def download_client():
    """Serve client.py with this server's address baked in as the default host,
    so it can be downloaded and run with no configuration."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client.py")
    try:
        with open(path, "r") as f:
            source = f.read()
    except FileNotFoundError:
        return Response("client.py not found on the server\n", status=404, mimetype="text/plain")

    base = request.host_url.rstrip("/")  # e.g. http://your-host-ip:8080
    source = source.replace('os.environ.get("NETCHECK_HOST", "")',
                            f'os.environ.get("NETCHECK_HOST", "{base}")')
    return Response(
        source,
        mimetype="text/x-python",
        headers={"Content-Disposition": "attachment; filename=client.py"},
    )


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


# ---------------------------------------------------------------- dashboard ---
DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Network Health</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #0f1117; color: #e6e8ee; }
  header { padding: 20px 24px; border-bottom: 1px solid #232733; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  select { background: #1a1d27; color: #e6e8ee; border: 1px solid #2c3140; border-radius: 6px; padding: 6px 10px; }
  main { padding: 24px; max-width: 1100px; margin: 0 auto; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card { background: #161922; border: 1px solid #232733; border-radius: 10px; padding: 16px 18px; }
  .card .label { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #8b93a7; }
  .card .value { font-size: 26px; font-weight: 650; margin-top: 6px; }
  .ok { color: #3ddc84; } .bad { color: #ff5d5d; } .warn { color: #ffb020; }
  .strip-wrap { background: #161922; border: 1px solid #232733; border-radius: 10px; padding: 16px 18px; margin-bottom: 24px; }
  .strip-wrap h2, .chart-wrap h2, .table-wrap h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: #8b93a7; margin: 0 0 12px; }
  .strip { position: relative; height: 34px; background: #1f7a4d; border-radius: 6px; overflow: hidden; }
  .strip .down { position: absolute; top: 0; bottom: 0; background: #ff5d5d; }
  .axis { display: flex; justify-content: space-between; font-size: 11px; color: #8b93a7; margin-top: 6px; }
  .chart-wrap { background: #161922; border: 1px solid #232733; border-radius: 10px; padding: 16px 18px; margin-bottom: 24px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #232733; }
  th { color: #8b93a7; font-weight: 500; font-size: 12px; text-transform: uppercase; }
  .muted { color: #8b93a7; }
  .install { background: #161922; border: 1px solid #232733; border-radius: 10px; padding: 16px 18px; margin-bottom: 24px; }
  .install code { display: block; background: #0b0d13; border: 1px solid #232733; border-radius: 8px; padding: 12px 14px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; color: #e6e8ee; overflow-x: auto; white-space: pre; }
  .install button { margin-top: 10px; background: #2c5cff; color: #fff; border: 0; border-radius: 7px; padding: 7px 14px; font-size: 13px; cursor: pointer; }
  .install button:hover { background: #1f49d6; }
  .install .ok { margin-left: 10px; font-size: 13px; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .pill.up { background: rgba(61,220,132,.15); color: #3ddc84; }
  .pill.down { background: rgba(255,93,93,.15); color: #ff5d5d; }
</style>
</head>
<body>
<header>
  <h1>Network Health</h1>
  <span id="status"></span>
  <span style="flex:1"></span>
  <label class="muted" style="font-size:13px">Client
    <select id="client"></select>
  </label>
  <label class="muted" style="font-size:13px">Window
    <select id="hours">
      <option value="24" selected>24 hours</option>
      <option value="6">6 hours</option>
      <option value="1">1 hour</option>
      <option value="168">7 days</option>
    </select>
  </label>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="label">Uptime</div><div class="value" id="uptime">--</div></div>
    <div class="card"><div class="label">Outages</div><div class="value" id="outages">--</div></div>
    <div class="card"><div class="label">Total downtime</div><div class="value" id="downtime">--</div></div>
    <div class="card"><div class="label">Heartbeats</div><div class="value" id="beats">--</div></div>
  </div>

  <div class="install">
    <h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:#8b93a7;margin:0 0 12px">Monitor a new machine</h2>
    <p class="muted" style="margin:0 0 10px;font-size:13px">Run this on any Linux client to start sending heartbeats (needs python3):</p>
    <code id="installCmd">loading...</code>
    <button onclick="copyInstall()">Copy</button>
    <span class="ok" id="copyOk"></span>
    <a class="muted" id="dlLink" style="margin-left:12px;font-size:13px" href="client.py">download client.py</a>
  </div>

  <div class="strip-wrap">
    <h2>Availability timeline</h2>
    <div class="strip" id="strip"></div>
    <div class="axis"><span id="axisStart"></span><span id="axisEnd"></span></div>
  </div>

  <div class="chart-wrap">
    <h2>Latency (ms)</h2>
    <canvas id="latency" height="90"></canvas>
  </div>

  <div class="table-wrap">
    <h2>Outage log</h2>
    <table>
      <thead><tr><th>Start</th><th>End</th><th>Duration</th><th></th></tr></thead>
      <tbody id="outageRows"></tbody>
    </table>
  </div>
</main>

<script>
let chart;
const INSTALL_CMD = `curl -sO ${location.origin}/client.py && python3 client.py`;
document.getElementById("installCmd").textContent = INSTALL_CMD;
function copyInstall() {
  navigator.clipboard.writeText(INSTALL_CMD).then(() => {
    const ok = document.getElementById("copyOk");
    ok.textContent = "copied";
    setTimeout(() => ok.textContent = "", 1500);
  });
}
const fmtDur = s => {
  s = Math.round(s);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s/60) + "m " + (s%60) + "s";
  return Math.floor(s/3600) + "h " + Math.floor((s%3600)/60) + "m";
};
const fmtTime = ts => new Date(ts*1000).toLocaleString([], {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"});
const fmtClock = ts => new Date(ts*1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});

async function loadClients() {
  const sel = document.getElementById("client");
  const cur = sel.value;
  const list = await (await fetch("api/clients")).json();
  sel.innerHTML = "";
  if (list.length === 0) { const o = document.createElement("option"); o.textContent = "(no data yet)"; sel.appendChild(o); return; }
  list.forEach(c => { const o = document.createElement("option"); o.value = c; o.textContent = c; sel.appendChild(o); });
  if (cur && list.includes(cur)) sel.value = cur;
}

async function load() {
  const client = document.getElementById("client").value;
  const hours = document.getElementById("hours").value;
  const q = new URLSearchParams({hours}); if (client) q.set("client", client);
  const d = await (await fetch("api/data?" + q)).json();

  // status pill
  const st = document.getElementById("status");
  if (d.currently_down === null) st.innerHTML = '<span class="pill">no data</span>';
  else st.innerHTML = d.currently_down ? '<span class="pill down">DOWN now</span>' : '<span class="pill up">UP</span>';

  // cards
  const up = document.getElementById("uptime");
  up.textContent = d.uptime_pct.toFixed(3) + "%";
  up.className = "value " + (d.uptime_pct >= 99.9 ? "ok" : d.uptime_pct >= 99 ? "warn" : "bad");
  document.getElementById("outages").textContent = d.outage_count;
  document.getElementById("downtime").textContent = fmtDur(d.total_downtime_s);
  document.getElementById("beats").textContent = d.points.length;

  // timeline strip
  const strip = document.getElementById("strip");
  strip.innerHTML = "";
  const span = d.now - d.window_start;
  d.outages.forEach(o => {
    const el = document.createElement("div");
    el.className = "down";
    el.style.left = (100 * (o.start - d.window_start) / span) + "%";
    el.style.width = Math.max(0.3, 100 * o.duration / span) + "%";
    el.title = fmtTime(o.start) + " -> " + fmtTime(o.end) + " (" + fmtDur(o.duration) + ")";
    strip.appendChild(el);
  });
  document.getElementById("axisStart").textContent = fmtTime(d.window_start);
  document.getElementById("axisEnd").textContent = fmtTime(d.now);

  // latency chart
  const pts = d.points.map(p => ({x: p.ts*1000, y: p.latency_ms}));
  const ctx = document.getElementById("latency");
  if (chart) chart.destroy();
  chart = new Chart(ctx, {
    type: "line",
    data: { datasets: [{ label: "latency ms", data: pts, borderColor: "#5b8cff", backgroundColor: "rgba(91,140,255,.15)", pointRadius: 0, borderWidth: 1.5, tension: .2, spanGaps: false }] },
    options: {
      animation: false, parsing: false,
      scales: {
        x: { type: "time", time: { tooltipFormat: "MMM d HH:mm" }, ticks: { color: "#8b93a7" }, grid: { color: "#1c2030" } },
        y: { beginAtZero: true, ticks: { color: "#8b93a7" }, grid: { color: "#1c2030" } }
      },
      plugins: { legend: { display: false } }
    }
  });

  // outage table
  const tb = document.getElementById("outageRows");
  tb.innerHTML = "";
  if (d.outages.length === 0) {
    tb.innerHTML = '<tr><td colspan="4" class="muted">No outages in this window.</td></tr>';
  } else {
    [...d.outages].reverse().forEach(o => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${fmtTime(o.start)}</td><td>${o.ongoing ? '<span class="muted">ongoing</span>' : fmtTime(o.end)}</td><td>${fmtDur(o.duration)}</td><td>${o.ongoing ? '<span class="pill down">ongoing</span>' : ''}</td>`;
      tb.appendChild(tr);
    });
  }
}

document.getElementById("client").addEventListener("change", load);
document.getElementById("hours").addEventListener("change", load);

(async () => { await loadClients(); await load(); setInterval(async () => { await loadClients(); await load(); }, 15000); })();
</script>
</body>
</html>
"""

# Chart.js needs a time adapter; load date-fns adapter alongside.
DASHBOARD_HTML = DASHBOARD_HTML.replace(
    '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>',
    '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/date-fns@3.6.0/cdn.min.js"></script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>',
)


if __name__ == "__main__":
    init_db()
    print(f"netcheck server on :{PORT}  db={DB_PATH}  interval={EXPECTED_INTERVAL}s  outage_after={OUTAGE_THRESHOLD}s")
    if OUTAGE_THRESHOLD <= EXPECTED_INTERVAL:
        print(
            f"WARNING: NETCHECK_OUTAGE_AFTER ({OUTAGE_THRESHOLD}s) <= NETCHECK_INTERVAL "
            f"({EXPECTED_INTERVAL}s); normal heartbeats will be flagged as outages. "
            f"Set the threshold to at least 2x the interval."
        )
    app.run(host="0.0.0.0", port=PORT)
