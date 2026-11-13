#!/usr/bin/env python3
"""Rebuild static/airports.json — the airports the maps can show — from OurAirports.

OurAirports (https://ourairports.com) publishes its data into the public domain
(The Unlicense). This keeps only what the map's airport layer draws:

  l  large_airport                         ("Major" on the map)
  m  medium_airport                        ("Regional")
  s  small_airport with an open, paved runway of 3,000 ft or more ("Small fields")

Heliports, seaplane bases, closed airports, grass strips and airports with only
a made-up OurAirports ident (US-1234, X-ABCD) are left out. Each airport keeps
its code (ICAO when it has one, else its GPS/local code), position and open
runways' end points. Names are not kept: the map shows codes only.

Usage:
    python3 scripts/build_airports.py                 # download both CSVs
    python3 scripts/build_airports.py --airports airports.csv --runways runways.csv

Output is deterministic (sorted, rounded to 5 decimals, about a metre) and has
one airport per line, so a refresh reads as a normal diff.
"""
import argparse
import csv
import io
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

BASE_URL = "https://davidmegginson.github.io/ourairports-data/"
OUT = Path(__file__).resolve().parent.parent / "static" / "airports.json"
CATEGORY = {"large_airport": "l", "medium_airport": "m", "small_airport": "s"}
PAVED = ("ASP", "CON", "PEM", "BIT", "PAV")
SMALL_MIN_FT = 3000
MADE_UP = re.compile(r"-")        # OurAirports placeholders (US-1234, X-ABCD); real codes have no hyphen


def _read(path_or_name):
    if path_or_name and Path(path_or_name).is_file():
        return Path(path_or_name).read_text(encoding="utf-8")
    url = BASE_URL + path_or_name
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build(airports_csv, runways_csv):
    runways = {}
    paved = set()
    for r in csv.DictReader(io.StringIO(runways_csv)):
        if r.get("closed") == "1":
            continue
        ident = r.get("airport_ident") or ""
        length = _num(r.get("length_ft")) or 0
        if (r.get("surface") or "").upper().startswith(PAVED) and length >= SMALL_MIN_FT:
            paved.add(ident)
        ends = [_num(r.get(k)) for k in ("le_latitude_deg", "le_longitude_deg",
                                         "he_latitude_deg", "he_longitude_deg")]
        if all(v is not None for v in ends):
            runways.setdefault(ident, []).append([round(v, 5) for v in ends])
    rows = []
    for r in csv.DictReader(io.StringIO(airports_csv)):
        cat = CATEGORY.get(r.get("type"))
        ident = r.get("ident") or ""
        if not cat or (cat == "s" and ident not in paved):
            continue
        lat, lon = _num(r.get("latitude_deg")), _num(r.get("longitude_deg"))
        code = (r.get("icao_code") or r.get("gps_code") or r.get("local_code") or ident).strip().upper()
        if lat is None or lon is None or not code or MADE_UP.search(code):
            continue
        rows.append([code, cat, round(lat, 5), round(lon, 5), sorted(runways.get(ident, []))])
    rows.sort(key=lambda a: (a[0], a[1], a[2], a[3]))
    return rows


def write(rows, out=OUT, built=None):
    built = built or date.today().isoformat()
    head = json.dumps({"source": "OurAirports (ourairports.com), public domain (The Unlicense)",
                       "built": built, "count": len(rows)})[:-1]
    body = ",\n".join(json.dumps(a, separators=(",", ":")) for a in rows)
    out.write_text(head + ', "airports": [\n' + body + "\n]}\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--airports", default="airports.csv", help="OurAirports airports.csv (path; downloaded when absent)")
    ap.add_argument("--runways", default="runways.csv", help="OurAirports runways.csv (path; downloaded when absent)")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--built", default=None, help="date to record (default today)")
    a = ap.parse_args(argv)
    rows = build(_read(a.airports), _read(a.runways))
    write(rows, Path(a.out), a.built)
    counts = {c: sum(1 for r in rows if r[1] == c) for c in "lms"}
    print(f"wrote {a.out}: {len(rows)} airports (major {counts['l']}, regional {counts['m']}, small {counts['s']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
