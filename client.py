#!/usr/bin/env python3
"""
Network health check - client heartbeat sender.

Runs in a loop and POSTs a heartbeat to the host every NETCHECK_INTERVAL seconds.
The POST itself is the probe: its round-trip time is reported as latency. If the
POST fails (timeout, connection refused, DNS error), the beat simply doesn't
arrive at the host, and the host records the gap as an outage. Nothing needs to
be done client-side on failure beyond logging and trying again.

Config via environment variables:
  NETCHECK_HOST       Host base URL, e.g. http://1.2.3.4:8080   (required)
  NETCHECK_CLIENT_ID  Name for this client     (default: system hostname)
  NETCHECK_INTERVAL   Seconds between beats     (default 5) - keep below the
                      server's NETCHECK_OUTAGE_AFTER threshold
  NETCHECK_TIMEOUT    Per-request timeout (s)   (default 10)
  NETCHECK_TOKEN      Shared secret, sent as Authorization: Bearer <token>
"""

import os
import socket
import sys
import time
import urllib.request
import urllib.error

HOST = os.environ.get("NETCHECK_HOST", "").rstrip("/")
CLIENT_ID = os.environ.get("NETCHECK_CLIENT_ID") or socket.gethostname()
INTERVAL = float(os.environ.get("NETCHECK_INTERVAL", "5"))
TIMEOUT = float(os.environ.get("NETCHECK_TIMEOUT", "10"))
TOKEN = os.environ.get("NETCHECK_TOKEN", "")

if not HOST:
    sys.exit("NETCHECK_HOST is required, e.g. export NETCHECK_HOST=http://1.2.3.4:8080")

URL = HOST + "/heartbeat"


def send_one(latency_ms):
    import json

    body = json.dumps({"client_id": CLIENT_ID, "latency_ms": latency_ms}).encode()
    req = urllib.request.Request(URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        resp.read()
        return resp.status


def main():
    print(f"netcheck client '{CLIENT_ID}' -> {URL}  every {INTERVAL}s", flush=True)
    last_latency = None  # carry the previous round trip into this beat
    while True:
        start = time.monotonic()
        try:
            t0 = time.monotonic()
            status = send_one(latency_ms=last_latency)
            last_latency = round((time.monotonic() - t0) * 1000, 2)
            print(f"ok  {last_latency:.1f} ms  (HTTP {status})", flush=True)
        except urllib.error.URLError as e:
            last_latency = None
            print(f"FAIL {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            last_latency = None
            print(f"FAIL {e}", flush=True)

        # keep a steady cadence regardless of how long the request took
        elapsed = time.monotonic() - start
        time.sleep(max(0, INTERVAL - elapsed))


if __name__ == "__main__":
    main()
