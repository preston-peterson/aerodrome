#!/usr/bin/env python3
"""Aircraft type-shape coverage audit.

Answers one question at bump time: which aircraft types does Aerodrome
NOT draw a silhouette for, and how much live traffic do they account
for? Types with a shape render the parametric silhouette; everything
else falls back to the generic chevron (by design — a wrong shape is
worse than a generic one). Helicopters and tiltrotors are excluded
entirely: the radar draws them a heading-agnostic rotor disc, so a
missing rotorcraft designator is never a gap.

Two information sources, in priority order:

  1. The repo's own ``seen_aircraft`` table (default: ./aircraft_history.db
     or $AERODROME_DB_PATH, overridable with --db). The box's install
     keeps this; a dev tree may have an empty/stale one — that's fine,
     the audit degrades to DOC 8643 alone. Traffic rank = rows per
     typecode (one row per airframe ever seen), which is a coarse proxy
     for how often you'll see a missing type on your own radar.

  2. ICAO DOC 8643 aircraft type designators (the authoritative list of
     codes transponders can actually emit), fetched from the
     ColtJD45/icao-aircraft-designator-list CSV mirror and cached under
     the module directory (``doc8643.csv`` — refresh with --update-cache
     if the cache is older than ~a year). This source catches codes you
     haven't seen locally yet but that traffic CAN carry, and labels
     every code with its manufacturer/model/engine data — enough to
     know whether a one-line SOURCES entry in gen_type_shapes.py is
     warranted.

The DOC 8643 fetch is network-best-effort: no network / fetch failure /
unreadable cache = the audit runs on the local table alone and says so.

Exit codes (this is a bump gate, advisory by default):
  0  everything checked out, or the audit could not run at all
  1  (--strict / AERODROME_SHAPE_GATE=strict) AND the local table shows
     a type WITHOUT a shape accounting for >= --min-share (default 5%)
     of local traffic. A release-blocking signal: you are staring at
     chevrons for a big chunk of your own sky.

Usage:
  python3 scripts/shape_coverage.py                 # human report
  python3 scripts/shape_coverage.py --json          # machine-readable
  python3 scripts/shape_coverage.py --top 25        # bigger list
  python3 scripts/shape_coverage.py --update-cache  # refresh DOC 8643
  AERODROME_SHAPE_GATE=strict bump-version.sh ...   # gate mode
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DOC8643_URL = ("https://raw.githubusercontent.com/ColtJD45/"
               "icao-aircraft-designator-list/main/icao_aircraft_data.csv")
DOC8643_CACHE = os.path.join(HERE, "doc8643.csv")
TOP_DEFAULT = 15
DEFAULT_DB = ("aircraft_history.db", os.environ.get("AERODROME_DB_PATH", ""))


def load_shapes():
    """Codes the frontend can draw, straight from the shipped data file
    (never from SOURCES — the audit must reflect what ships, not what's
    configured)."""
    js = os.path.join(REPO, "static", "shapes-data.js")
    with open(js, encoding="utf-8") as f:
        return set(re.findall(r'"([A-Z0-9]{4})":', f.read()))


def load_doc8643(quiet):
    """designator -> {desc, rotor}, one entry per code. The CSV lists one
    row per manufacturer/model variant, so 'rotor' is the OR across all
    rows for a code. DOC 8643 classifies rotorcraft in the `description`
    column ("Helicopter" / "Tiltrotor"). Cache-first, network
    best-effort. Returns {} when neither source is available."""
    import csv
    import io

    def parse(text):
        out = {}
        for r in csv.DictReader(io.StringIO(text)):
            d = (r.get("type_designator") or "").strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{4}", d):
                continue
            entry = out.setdefault(d, {"desc": "", "rotor": False})
            if not entry["desc"]:  # first variant wins; one line per code
                mfr = (r.get("manufacturer") or "").strip()
                model = (r.get("model") or "").strip()
                eng = (r.get("engine_type") or "").split("/")[0]
                n = (r.get("engine_count") or "").strip()
                entry["desc"] = f"{mfr} {model} ({eng}/{n})".strip()
            if (r.get("description") or "").strip() in ("Helicopter",
                                                        "Tiltrotor"):
                entry["rotor"] = True
        return out

    try:
        with open(DOC8643_CACHE, encoding="utf-8") as f:
            cached = parse(f.read())
        if cached:
            return cached
    except OSError:
        pass
    try:
        with urllib.request.urlopen(DOC8643_URL, timeout=15) as resp:
            text = resp.read().decode("utf-8", "replace")
        with open(DOC8643_CACHE, "w", encoding="utf-8") as f:
            f.write(text)
        return parse(text)
    except Exception as exc:  # noqa: BLE001 - offline is normal here
        if not quiet:
            print(f"  (DOC 8643 fetch failed: {exc}; local table alone)")
        return {}


def load_traffic(db_path):
    """typecode -> #airframes seen. Missing/empty table is normal on a
    dev tree: ({}, False, note)."""
    for cand in (db_path, *DEFAULT_DB):
        if cand and os.path.isfile(cand):
            try:
                con = sqlite3.connect(f"file:{cand}?mode=ro", uri=True)
                rows = con.execute(
                    "select aircraft_type, count(*) from seen_aircraft "
                    "where aircraft_type not in ('','UNKNOWN') "
                    "group by 1").fetchall()
                con.close()
                return {t: n for t, n in rows}, True, cand
            except sqlite3.Error:
                continue
    return {}, False, "(no readable aircraft_history.db)"


def _table_rotorcraft(db_path):
    """Typecodes the local table itself marks as rotorcraft (the
    collector's category column — what the ADS-B emitter said). Used so
    the rotor exclusion survives an offline DOC 8643 fetch."""
    for cand in (db_path, *DEFAULT_DB):
        if cand and os.path.isfile(cand):
            try:
                con = sqlite3.connect(f"file:{cand}?mode=ro", uri=True)
                rows = con.execute(
                    "select distinct aircraft_type from seen_aircraft "
                    "where lower(category) in ('helicopter','rotorcraft')"
                ).fetchall()
                con.close()
                return {t for (t,) in rows if t}
            except sqlite3.Error:
                continue
    return set()


def audit(db_path=None, doc=True):
    shapes = load_shapes()
    traffic, have_db, db_note = load_traffic(db_path)
    names = load_doc8643(quiet=not doc) if doc else {}

    total = sum(traffic.values())
    # "Missing" excludes rotorcraft — they're covered by the rotor disc
    # glyph, never a shape gap. Coverage is measured over the non-rotor
    # sky so the denominator doesn't flatter itself.
    helis = {d for d, e in names.items() if e["rotor"]}
    # Local tables carry the ADS-B emitter *category* ("rotorcraft" —
    # what the transponder said), so we can exclude helis from the gap
    # list even when the DOC 8643 fetch is unavailable and its rotor
    # column can't be read.
    helis |= _table_rotorcraft(db_path)
    plane_traffic = {t: n for t, n in traffic.items() if t not in helis}
    total = sum(plane_traffic.values())
    covered_traffic = sum(n for t, n in plane_traffic.items() if t in shapes)
    missing = {t: n for t, n in plane_traffic.items() if t not in shapes}
    missing_traffic = sum(missing.values())
    ranked = sorted(missing.items(), key=lambda kv: -kv[1])
    doc_missing = sorted(
        (d, e["desc"]) for d, e in names.items()
        if d not in shapes and not e["rotor"])

    return {
        "shapes": len(shapes),
        "db": db_note,
        "db_available": have_db,
        "total_airframes": total,
        "coverage_pct": round(100.0 * covered_traffic / total, 1) if total else None,
        "missing_ranked": [
            {"code": t, "airframes": n,
             "share_pct": round(100.0 * n / total, 1) if total else 0.0,
             "name": names.get(t, {}).get("desc", "")}
            for t, n in ranked],
        "missing_traffic_pct":
            round(100.0 * missing_traffic / total, 1) if total else None,
        "doc8643": {"available": bool(names),
                    "codes_without_shape": len(doc_missing),
                    "rotor_codes": len(helis)},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="path to aircraft_history.db")
    ap.add_argument("--top", type=int, default=TOP_DEFAULT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-doc", action="store_true",
                    help="skip the DOC 8643 fetch/cache entirely (offline)")
    ap.add_argument("--min-share", type=float, default=5.0,
                    help="strict mode fails when one missing type exceeds "
                         "this %% of local traffic")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 on a significant local gap")
    ap.add_argument("--update-cache", action="store_true",
                    help="force a fresh DOC 8643 fetch (refreshes "
                         "scripts/doc8643.csv)")
    args = ap.parse_args()
    strict = args.strict or os.environ.get("AERODROME_SHAPE_GATE") == "strict"

    if args.update_cache and os.path.exists(DOC8643_CACHE):
        os.remove(DOC8643_CACHE)
    r = audit(db_path=args.db, doc=not args.no_doc)

    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print(f"Aircraft type-shape coverage ({r['shapes']} shapes drawn,"
              f" db: {r['db']})")
        if r["coverage_pct"] is not None:
            print(f"  fixed-wing traffic covered by a silhouette:"
                  f" {r['coverage_pct']}%"
                  f" ({r['total_airframes']} airframes)")
        else:
            print("  (no local traffic data — DOC 8643 names only)")
        for m in r["missing_ranked"][:args.top]:
            print(f"    {m['code']:6s} {m['airframes']:5d} airframes"
                  f" ({m['share_pct']:4.1f}%)  {m['name'] or '?'}")
        if r["missing_traffic_pct"]:
            print(f"  total missing share: {r['missing_traffic_pct']}%"
                  f" of local traffic")
        d = r["doc8643"]
        if d["available"]:
            print(f"  DOC 8643: {d['codes_without_shape']} designatable"
                  f" codes have no shape ({d['rotor_codes']} rotorcraft"
                  f" excluded — the rotor disc covers them);"
                  f" a few hundred plane gaps is expected —"
                  f" chevrons are correct for rarities)")
        else:
            print("  DOC 8643: unavailable (offline or --no-doc)")

    worst = max((m["share_pct"] for m in r["missing_ranked"]), default=0.0)
    if strict and r["db_available"] and worst >= args.min_share:
        print(f"  ✗ strict gate: {worst}% of local fixed-wing traffic draws"
              f" a chevron (>= {args.min_share}% on one type). Add SOURCES"
              f" entries in scripts/gen_type_shapes.py, re-run it, re-bump.")
        sys.exit(1)


if __name__ == "__main__":
    main()
