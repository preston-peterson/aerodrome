"""
Aerodrome synthetic feeder (v2.85.0).

Two modes:

  serve.py    — HTTP server that mimics dump1090-fa/tar1090's
                /data/aircraft.json endpoint. Point Aerodrome's
                config receiver.{ip,port,path} at this and it
                will run as if a real ADS-B receiver were attached.
                Used for live-flow testing (collector behaviour,
                Live tab, watchlist alerts, classifier paths).

  backfill.py — Bulk-inserts synthetic historical sightings
                directly into all_sightings + sightings_hourly.
                Used for query-side testing — get the test bench
                to a target data scale (e.g. matching a user's
                production install) in minutes rather than waiting
                weeks of real-time accumulation.

Both modes share generator.py for the underlying data shape.
The generator is hermetic: random valid hex ICAOs that won't
collide with real registrations, configurable home location,
no outbound network calls.

This is a maintainer tool. Lives in tools/ and is excluded from
the release zip via the bump-version.sh staging logic.
"""
