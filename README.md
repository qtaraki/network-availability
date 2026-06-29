# Network Health Check

A lightweight uptime monitor. A **client** on your Linux machine sends a heartbeat
to a **host** server in the cloud every few seconds. The host stores heartbeats in
SQLite and serves a webpage showing uptime, outages, and latency over the last 24h.

## How it detects outages

The host doesn't ping the client. Instead the client pushes a heartbeat on a fixed
interval, and the host watches for **missing** beats. If no heartbeat arrives for
`NETCHECK_OUTAGE_AFTER` seconds (default 10), that gap is recorded as an outage.
The default heartbeat interval is 5s, so a single missed beat trips the threshold.

This is deliberate: when the network goes down, the client physically cannot reach
the host, so the *absence* of heartbeats is the signal. It also sidesteps NAT and
firewalls, since all traffic is outbound from the client. Detection latency is
roughly the threshold you set, so a 10s threshold flags an outage within ~10s.

Keep the threshold larger than the interval (at least 2x), or normal beats get
flagged as outages. The server prints a warning at startup if you misconfigure it.

```
  Linux client                         Cloud host
  ┌────────────┐   POST /heartbeat     ┌─────────────────────────┐
  │ client.py  │ ───────every 15s────▶ │ server.py (Flask)       │
  │            │                       │  ├─ SQLite: heartbeats   │
  └────────────┘   (gap = outage)      │  └─ GET /  dashboard     │
                                       └─────────────────────────┘
                                          browse http://HOST:8080
```

## Files

| File | Where it runs | Purpose |
|------|---------------|---------|
| `server.py` | cloud host | Flask app: receives heartbeats, stores them, serves the dashboard |
| `client.py` | your client | Loop that POSTs a heartbeat every interval |
| `netcheck-server.service` | cloud host | systemd unit for the server |
| `netcheck-client.service` | your client | systemd unit for the client |
| `requirements.txt` | both | Python deps (`Flask`; `gunicorn` optional) |

## Setup

### 1. Host (cloud server)

```bash
sudo mkdir -p /opt/netcheck && sudo cp server.py /opt/netcheck/
pip3 install Flask
# quick start (dev server):
NETCHECK_PORT=8080 NETCHECK_INTERVAL=15 python3 /opt/netcheck/server.py
```

Open the firewall / security group for the port (e.g. 8080) so the client and your
browser can reach it. Then visit `http://YOUR_HOST_IP:8080/`.

To run it as a service:

```bash
sudo cp netcheck-server.service /etc/systemd/system/
# edit the Environment= lines first (port, interval, optional token)
sudo systemctl daemon-reload
sudo systemctl enable --now netcheck-server
```

### 2. Client (the machine being monitored)

```bash
sudo mkdir -p /opt/netcheck && sudo cp client.py /opt/netcheck/
export NETCHECK_HOST=http://YOUR_HOST_IP:8080
export NETCHECK_INTERVAL=15          # must match the server
python3 /opt/netcheck/client.py
```

As a service:

```bash
sudo cp netcheck-client.service /etc/systemd/system/
# edit NETCHECK_HOST (and NETCHECK_INTERVAL / token) in the file first
sudo systemctl daemon-reload
sudo systemctl enable --now netcheck-client
```

## Configuration (environment variables)

**Server**

| Var | Default | Meaning |
|-----|---------|---------|
| `NETCHECK_PORT` | `8080` | Listen port |
| `NETCHECK_DB` | `./heartbeats.db` | SQLite file path |
| `NETCHECK_INTERVAL` | `5` | Expected client interval, seconds |
| `NETCHECK_OUTAGE_AFTER` | `10` | Declare an outage when no heartbeat for this many seconds. Must be > interval |
| `NETCHECK_TOKEN` | (none) | If set, clients must send `Authorization: Bearer <token>` |

**Client**

| Var | Default | Meaning |
|-----|---------|---------|
| `NETCHECK_HOST` | (required) | Host base URL, e.g. `http://1.2.3.4:8080` |
| `NETCHECK_CLIENT_ID` | hostname | Label shown in the dashboard |
| `NETCHECK_INTERVAL` | `5` | Seconds between beats (keep below the server's outage threshold) |
| `NETCHECK_TIMEOUT` | `10` | Per-request timeout |
| `NETCHECK_TOKEN` | (none) | Shared secret, must match the server |

## The dashboard

`http://HOST:PORT/` shows, for a selectable client and window (1h / 6h / 24h / 7d):

- **Uptime %**, outage count, total downtime, heartbeat count
- An **availability timeline** strip (green = up, red bars = outages)
- A **latency** line chart
- An **outage log** table with start, end, and duration of each outage

It auto-refreshes every 15 seconds. Multiple clients are supported; pick one from
the dropdown.

## Notes and hardening

- **Production serving.** `server.py` uses Flask's built-in dev server, which is
  fine for a single client. For something sturdier:
  `gunicorn -w 2 -b 0.0.0.0:8080 server:app` (run `init_db()` once first, or just
  start `server.py` once to create the table).
- **Use a token.** Set `NETCHECK_TOKEN` on both ends so random internet traffic
  can't inject fake heartbeats into your DB.
- **Put TLS in front.** For a public host, run nginx/Caddy as a reverse proxy with
  HTTPS rather than exposing the port directly.
- **Interval vs. resolution.** The default 5s interval with a 10s outage threshold
  detects outages within ~10s and writes ~17k rows/day. Raise both proportionally
  (e.g. 15s interval / 30s threshold) for fewer rows and coarser resolution.
- **Clock.** Outage math uses the host's receive time, so the client's clock does
  not need to be accurate.

## Acknowledgments

Built by Quais Taraki with Claude (Anthropic) as a coding collaborator.
