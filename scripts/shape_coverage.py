#!/usr/bin/env python3
"""Aircraft type-shape coverage audit.

Answers one question at bump time: which aircraft designators can a
transponder actually emit that Aerodrome does NOT draw a silhouette for?
Types with a shape render the parametric silhouette; everything else
falls back to the generic chevron (by design — a wrong shape is worse
than a generic one). Helicopters and tiltrotors are excluded entirely:
one without a silhouette of its own still gets the radar's rotor disc,
so a missing rotorcraft designator is never a gap.

One information source:

  ICAO DOC 8643 aircraft type designators (the authoritative list of
  codes transponders can actually emit), fetched from the ColtJD45/icao-
  aircraft-designator-list CSV mirror and cached under the module
  directory (``doc8643.csv`` — refresh with --update-cache if the cache
  is older than ~a year). This catches codes no receiver has reported
  yet but that traffic CAN carry, and labels every code with its
  manufacturer/model/engine data — enough to know whether a one-line
  SOURCES entry in gen_type_shapes.py is warranted.

The fetch is network-best-effort: no network / fetch failure /
unreadable cache = the audit prints the shipped shape count alone and
says DOC 8643 was unavailable. --cache-only reads the cached CSV when it
exists and never fetches or writes it (what `bump-version.sh check` and
package-release.sh use, so a gates-only run makes no network call).

Exit codes (bump-version.sh gates on any non-zero):
  0  report printed. An unavailable DOC 8643 source is NOT a failure —
     the report just says what it lacked.
  2  the shipped static/shapes-data.js is unreadable or yields no shape
     codes — the radar would draw no silhouettes at all.

Usage:
  python3 scripts/shape_coverage.py                 # human report
  python3 scripts/shape_coverage.py --json          # machine-readable
  python3 scripts/shape_coverage.py --update-cache  # refresh DOC 8643
  python3 scripts/shape_coverage.py --cache-only    # never fetch / write
"""

import argparse
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DOC8643_URL = ("https://raw.githubusercontent.com/ColtJD45/"
               "icao-aircraft-designator-list/main/icao_aircraft_data.csv")
DOC8643_CACHE = os.path.join(HERE, "doc8643.csv")


def load_shapes():
    """Codes the frontend can draw, straight from the shipped data file
    (never from SOURCES — the audit must reflect what ships, not what's
    configured)."""
    js = os.path.join(REPO, "static", "shapes-data.js")
    try:
        with open(js, encoding="utf-8") as f:
            codes = set(re.findall(r'^\s*"([A-Z0-9]{2,4})":', f.read(), re.M))
    except (OSError, UnicodeDecodeError) as exc:
        print(f"  ✗ cannot read static/shapes-data.js: {exc}")
        sys.exit(2)
    if not codes:
        print("  ✗ static/shapes-data.js has no shape codes — corrupt or"
              " truncated; regenerate with scripts/gen_type_shapes.py")
        sys.exit(2)
    return codes


def load_doc8643(quiet, cache_only=False):
    """designator -> {desc, rotor}, one entry per code. The CSV lists one
    row per manufacturer/model variant, so 'rotor' is the OR across all
    rows for a code. DOC 8643 classifies rotorcraft in the `description`
    column ("Helicopter" / "Tiltrotor"). Cache-first, network
    best-effort (cache_only: never fetch, never write the cache).
    Returns {} when neither source is available."""
    import csv
    import io

    def parse(text):
        out = {}
        for r in csv.DictReader(io.StringIO(text)):
            d = (r.get("type_designator") or "").strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{2,4}", d):
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
    if cache_only:
        return {}
    try:
        with urllib.request.urlopen(DOC8643_URL, timeout=15) as resp:
            text = resp.read().decode("utf-8", "replace")
        with open(DOC8643_CACHE, "w", encoding="utf-8") as f:
            f.write(text)
        return parse(text)
    except Exception as exc:  # noqa: BLE001 - offline is normal here
        if not quiet:
            print(f"  (DOC 8643 fetch failed: {exc}; shipped shapes only)")
        return {}


def audit(doc=True, cache_only=False):
    shapes = load_shapes()
    names = load_doc8643(quiet=not doc, cache_only=cache_only) if doc else {}

    # Rotorcraft are never a gap — they're covered by the rotor disc
    # glyph, so they stay out of both the gap count and its total.
    helis = {d for d, e in names.items() if e["rotor"]}
    doc_missing = sorted(
        (d, e["desc"]) for d, e in names.items()
        if d not in shapes and not e["rotor"])

    return {
        "shapes": len(shapes),
        "doc8643": {"available": bool(names),
                    "codes_without_shape": len(doc_missing),
                    "rotor_codes": len(helis)},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-doc", action="store_true",
                    help="skip the DOC 8643 fetch/cache entirely (offline)")
    ap.add_argument("--cache-only", action="store_true",
                    help="read scripts/doc8643.csv if present; never fetch "
                         "or write it (gates-only runs)")
    ap.add_argument("--update-cache", action="store_true",
                    help="force a fresh DOC 8643 fetch (refreshes "
                         "scripts/doc8643.csv)")
    args = ap.parse_args()

    if args.update_cache and os.path.exists(DOC8643_CACHE):
        os.remove(DOC8643_CACHE)
    r = audit(doc=not args.no_doc, cache_only=args.cache_only)

    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print(f"Aircraft type-shape coverage ({r['shapes']} shapes drawn)")
        d = r["doc8643"]
        if d["available"]:
            print(f"  DOC 8643: {d['codes_without_shape']} designatable"
                  f" codes have no shape ({d['rotor_codes']} rotorcraft"
                  f" excluded — the rotor disc covers any without a shape)."
                  f" Most are rarities, and a chevron is correct for them.")
        else:
            print("  DOC 8643: unavailable (offline, --no-doc, or --cache-only"
                  " with no cached scripts/doc8643.csv)")


if __name__ == "__main__":
    main()
