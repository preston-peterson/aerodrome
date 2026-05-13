"""
Mode A — synthetic ADS-B feeder HTTP server.

Serves /data/aircraft.json with a rolling fleet of synthetic
aircraft. Aerodrome's collector polls this URL on its normal
schedule; from the collector's perspective there's no
difference between this and a real dump1090/tar1090 receiver.

Usage::

    # Start the feeder on port 8080, 100 aircraft visible,
    # receiver "located" at 40N 75W with 250km coverage:
    python3 -m tools.synthetic_feeder.serve \\
        --port 8080 --visible 100 \\
        --home-lat 40.0 --home-lon -75.0 --range 250

Then in your Aerodrome config.yaml::

    receiver:
      ip: 127.0.0.1
      port: 8080
      path: /data/aircraft.json

Restart Aerodrome and the Live tab will populate from the
synthetic feed within one poll cycle.

Implementation note: stdlib http.server only. No Flask, no
FastAPI — keeping the feeder zero-deps means it's trivial to
run on a fresh test bench without a virtualenv setup step.
The fleet ticks on a background thread at 1 Hz regardless of
poll rate, so motion looks natural even if the collector polls
every 5 or 10 seconds.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Allow running as `python3 -m tools.synthetic_feeder.serve` OR directly
# as a script. The latter is convenient when iterating but requires the
# explicit path adjustment.
try:
    from .generator import Fleet
except ImportError:  # pragma: no cover — direct-script fallback
    from generator import Fleet  # type: ignore


logger = logging.getLogger("synthetic_feeder.serve")


# ---------------------------------------------------------------------
# HTTP handler

class FeederHandler(BaseHTTPRequestHandler):
    """Serves the current fleet snapshot at /data/aircraft.json
    (and /aircraft.json as a convenience alias). Anything else
    returns 404."""

    # Set by the server constructor to share the live fleet across
    # all request threads. Class-level so per-request handler instances
    # see the same fleet object.
    fleet: Fleet | None = None

    def do_GET(self):  # noqa: N802 — required name from BaseHTTPRequestHandler
        if self.path in ("/data/aircraft.json", "/aircraft.json"):
            self._send_snapshot()
        elif self.path == "/healthz":
            # Convenience for "is the feeder up" probes
            self._send_text(200, "ok\n")
        else:
            self._send_text(404, "not found\n")

    def _send_snapshot(self) -> None:
        snap = type(self).fleet.snapshot_json() if type(self).fleet else {}
        body = json.dumps(snap).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Match dump1090's caching headers — disable caching aggressively
        # so a poll always gets the freshest data.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Quiet the default per-request stderr log. Aerodrome polls
        # every few seconds; a request log line per poll is just noise.
        # Errors still surface via log_error which we leave at default.
        return


# ---------------------------------------------------------------------
# Background ticker

def _ticker_loop(fleet: Fleet, interval_s: float, stop_event: threading.Event) -> None:
    """Advance the fleet at fixed interval until stop_event is set.

    Runs independently of HTTP request rate. Real receivers update
    aircraft state continuously regardless of who's polling them; we
    mirror that so a low poll rate doesn't yield stale snapshots.
    """
    while not stop_event.wait(interval_s):
        fleet.tick(interval_s)


# ---------------------------------------------------------------------
# Entry point

def main() -> int:
    p = argparse.ArgumentParser(
        description="Synthetic ADS-B feeder HTTP server (Mode A)."
    )
    p.add_argument("--host", default="0.0.0.0",
                   help="Bind host (default: 0.0.0.0 — accept LAN connections)")
    p.add_argument("--port", type=int, default=8080,
                   help="Bind port (default: 8080)")
    p.add_argument("--visible", type=int, default=100,
                   help="Aircraft simultaneously visible (default: 100)")
    p.add_argument("--home-lat", type=float, default=40.0,
                   help="Receiver latitude (default: 40.0)")
    p.add_argument("--home-lon", type=float, default=-75.0,
                   help="Receiver longitude (default: -75.0)")
    p.add_argument("--range", dest="range_km", type=float, default=250.0,
                   help="Receiver coverage radius in km (default: 250)")
    p.add_argument("--military-fraction", type=float, default=0.05,
                   help="Fraction of aircraft in US military hex range "
                        "(default: 0.05 — ~5%%)")
    p.add_argument("--tick-interval", type=float, default=1.0,
                   help="Seconds between fleet position updates "
                        "(default: 1.0)")
    p.add_argument("--seed", type=int, default=None,
                   help="Random seed for reproducible runs (default: random)")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    fleet = Fleet(
        size=args.visible,
        home_lat=args.home_lat,
        home_lon=args.home_lon,
        max_range_km=args.range_km,
        military_fraction=args.military_fraction,
        seed=args.seed,
    )
    FeederHandler.fleet = fleet

    stop_event = threading.Event()
    ticker = threading.Thread(
        target=_ticker_loop,
        args=(fleet, args.tick_interval, stop_event),
        daemon=True,
        name="feeder-ticker",
    )
    ticker.start()

    server = ThreadingHTTPServer((args.host, args.port), FeederHandler)
    logger.info(
        "Synthetic feeder on http://%s:%d/data/aircraft.json — "
        "%d aircraft, home (%.4f, %.4f), %.0f km range, tick %.1fs",
        args.host, args.port, args.visible,
        args.home_lat, args.home_lon, args.range_km, args.tick_interval,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    finally:
        stop_event.set()
        server.server_close()
        ticker.join(timeout=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
