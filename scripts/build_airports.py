#!/usr/bin/env python3
"""Rebuild static/airports.json — the airports the maps can show — from OurAirports.

OurAirports (https://ourairports.com) publishes its data into the public domain
(The Unlicense). This keeps only what the map's airport layer draws:

  l  large_airport                         ("Major" on the map)
  m  medium_airport                        ("Regional")
  s  small_airport with an open, paved runway of 3,000 ft or more ("Small fields")

Heliports, seaplane bases, closed airports, grass strips and airports with only
a made-up OurAirports ident (US-1234, X-ABCD) are left out. Each airport keeps
its code (ICAO when it has one, else its GPS/local code), position, open runways
(both ends' positions and names, e.g. 13L/31R), field elevation in feet, name
(without a trailing " Airport"), IATA code (DFW; "" when it has none) and city.
The map draws codes and runway lines; the takeoff and landing detector
(airport_events.py) uses the elevation and runways; the pages that list takeoffs
and landings show the names; the airport page and Search use the IATA code and city.

Row: [code, category, lat, lon, runways, elevation_ft or null, name, iata, city]
Runway: [end1_lat, end1_lon, end2_lat, end2_lon, end1_name, end2_name]

It also writes static/airport-info.json, what the airport page shows about each
of those airports beyond the map: its runways (length, width, surface, lights),
radio frequencies, website and Wikipedia links (all from OurAirports), and
whether the FAA publishes plates for it (from the FAA's d-TPP index for the
current 28-day cycle, US government data). Only the airport page reads it, one
airport at a time.

Info row: [code, faa_ident, faa_plates (0/1), website, wikipedia, runways, radio]
  faa_ident: the FAA's own code for a US airport when it differs from code
             (DFW for KDFW, 9S7 for K9S7), else ""; the FAA and SkyVector links use it.
  runways:   [[name, length_ft or null, width_ft or null, surface, lit (0/1)], ...], longest first
  radio:     [[label, "118.300, 120.750"], ...], frequencies in the airband only

Usage:
    python3 scripts/build_airports.py                 # download the CSVs and the FAA index
    python3 scripts/build_airports.py --airports airports.csv --runways runways.csv \
        --frequencies airport-frequencies.csv --dtpp d-tpp_Metafile.xml

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
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://davidmegginson.github.io/ourairports-data/"
OUT = Path(__file__).resolve().parent.parent / "static" / "airports.json"
INFO_OUT = OUT.with_name("airport-info.json")
DTPP_URL = "https://aeronav.faa.gov/d-tpp/{cycle}/xml_data/d-tpp_Metafile.xml"
AIRAC_EPOCH = datetime(2026, 1, 22, 9, 1, tzinfo=timezone.utc)   # cycle 2601; one every 28 days
FAA_COUNTRIES = {"US", "PR", "GU", "VI", "AS", "MP", "UM"}       # where the FAA's codes apply
CATEGORY = {"large_airport": "l", "medium_airport": "m", "small_airport": "s"}
PAVED = ("ASP", "CON", "PEM", "BIT", "PAV")
SMALL_MIN_FT = 3000
MADE_UP = re.compile(r"-")        # OurAirports placeholders (US-1234, X-ABCD); real codes have no hyphen


def _read(path_or_name):
    if path_or_name and Path(path_or_name).is_file():
        return Path(path_or_name).read_text(encoding="utf-8")
    url = path_or_name if "://" in path_or_name else BASE_URL + path_or_name
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _name(v):
    name = " ".join((v or "").split())
    return name[:-len(" Airport")] if name.endswith(" Airport") and len(name) > len(" Airport") else name


def _paved(runways_csv):
    """OurAirports idents with an open, paved runway of SMALL_MIN_FT or more."""
    paved = set()
    for r in csv.DictReader(io.StringIO(runways_csv)):
        if r.get("closed") != "1" and (r.get("surface") or "").upper().startswith(PAVED) \
                and (_num(r.get("length_ft")) or 0) >= SMALL_MIN_FT:
            paved.add(r.get("airport_ident") or "")
    return paved


def _kept(airports_csv, paved):
    """(code, category, lat, lon, csv row) for each airport the list keeps."""
    for r in csv.DictReader(io.StringIO(airports_csv)):
        cat = CATEGORY.get(r.get("type"))
        ident = r.get("ident") or ""
        if not cat or (cat == "s" and ident not in paved):
            continue
        lat, lon = _num(r.get("latitude_deg")), _num(r.get("longitude_deg"))
        code = (r.get("icao_code") or r.get("gps_code") or r.get("local_code") or ident).strip().upper()
        if lat is None or lon is None or not code or MADE_UP.search(code):
            continue
        yield code, cat, lat, lon, r


def build(airports_csv, runways_csv):
    runways = {}
    paved = _paved(runways_csv)
    for r in csv.DictReader(io.StringIO(runways_csv)):
        if r.get("closed") == "1":
            continue
        ident = r.get("airport_ident") or ""
        ends = [_num(r.get(k)) for k in ("le_latitude_deg", "le_longitude_deg",
                                         "he_latitude_deg", "he_longitude_deg")]
        if all(v is not None for v in ends):
            names = [(r.get(k) or "").strip().upper()[:4] for k in ("le_ident", "he_ident")]
            runways.setdefault(ident, []).append([round(v, 5) for v in ends] + names)
    rows = []
    for code, cat, lat, lon, r in _kept(airports_csv, paved):
        elev = _num(r.get("elevation_ft"))
        iata = (r.get("iata_code") or "").strip().upper()
        rows.append([code, cat, round(lat, 5), round(lon, 5), sorted(runways.get(r.get("ident") or "", [])),
                     None if elev is None else int(round(elev)), _name(r.get("name")),
                     iata if re.fullmatch(r"[A-Z0-9]{3}", iata) else "", " ".join((r.get("municipality") or "").split())])
    rows.sort(key=lambda a: (a[0], a[1], a[2], a[3]))
    return rows


def write(rows, out=OUT, built=None):
    built = built or date.today().isoformat()
    head = json.dumps({"source": "OurAirports (ourairports.com), public domain (The Unlicense)",
                       "built": built, "count": len(rows)})[:-1]
    body = ",\n".join(json.dumps(a, separators=(",", ":")) for a in rows)
    out.write_text(head + ', "airports": [\n' + body + "\n]}\n", encoding="utf-8")


# Runway surfaces: OurAirports' free text (ASPH-G, CONC, TURF-F, ASPHALT/TURF ...) in
# plain words. The condition suffix (-G good, -F fair, -P poor, -E excellent) is dropped.
SURFACES = [("ASP", "Asphalt"), ("BIT", "Asphalt"), ("MAC", "Asphalt"), ("CON", "Concrete"),
            ("PEM", "Paved"), ("PAV", "Paved"), ("COP", "Paved"), ("COM", "Paved"),
            ("GRS", "Grass"), ("GRASS", "Grass"), ("TURF", "Grass"), ("GRAVEL", "Gravel"), ("GRE", "Gravel"), ("GRV", "Gravel"),
            ("GVL", "Gravel"), ("DIRT", "Dirt"), ("CLA", "Clay"), ("SAN", "Sand"), ("WATER", "Water")]
CONDITION = {"G", "F", "P", "E", "TRTD"}
# Radio: OurAirports' frequency types as labels, in the order the page lists them.
RADIO = [("ATIS", "ATIS"), ("AWOS", "AWOS"), ("ASOS", "ASOS"), ("CLD", "Clearance"), ("GND", "Ground"),
         ("TWR", "Tower"), ("TOWER", "Tower"), ("CTAF", "CTAF"), ("UNIC", "Unicom"), ("A/D", "Approach / Departure"),
         ("APP/DEP", "Approach / Departure"), ("APP", "Approach"), ("ARR", "Approach"), ("ACP", "Approach"),
         ("DEP", "Departure"), ("CNTR", "Center"), ("CTR", "Center"), ("ARTC", "Center"), ("ACC", "Center"),
         ("AFIS", "AFIS"), ("FSS", "Flight service"), ("RDO", "Radio"), ("RADIO", "Radio"), ("A/G", "Radio"),
         ("RCO", "Radio"), ("INFO", "Information"), ("FIS", "Information"), ("OPS", "Operations"), ("RMP", "Ramp"),
         ("RDR", "Radar"), ("RADAR", "Radar"), ("EMR", "Emergency"), ("MISC", "Other")]
RADIO_LABEL = dict(RADIO)
RADIO_ORDER = {label: i for i, (_, label) in reversed(list(enumerate(RADIO)))}


def _surface(text):
    words = [w for w in re.split(r"[-/ ,]+", (text or "").upper()) if w and w not in CONDITION]
    named = []
    for w in words:
        label = next((lbl for pre, lbl in SURFACES if w.startswith(pre)), None)
        if label is None:
            return " ".join((text or "").split()).capitalize() if words != ["UNK"] else ""
        if label not in named:
            named.append(label)
    return " / ".join(named[:1] + [n.lower() for n in named[1:]])


def _mhz(v):
    """The frequency as 118.300, or None outside the airband (VHF 108-137, military UHF
    225-400): OurAirports has a few truncated or mistyped ones (28.465 for 128.465)."""
    f = _num(v)
    if f is None or not (108 <= f < 137 or 225 <= f <= 400):
        return None
    return f"{f:.3f}"


def _link(v, host=None):
    v = (v or "").strip()
    if not re.match(r"https?://[^\s\"<>]+$", v) or (host and host not in v.split("/")[2]):
        return ""
    return v


def airac_cycle(when=None):
    """The FAA's d-TPP cycle in effect at `when` (default now), as "2611": the year's
    two digits and the cycle's number in that year (AIRAC: every 28 days, at 0901Z)."""
    when = when or datetime.now(timezone.utc)
    step = timedelta(days=28)
    n = (when - AIRAC_EPOCH) // step
    year = (AIRAC_EPOCH + n * step).year
    first = -((AIRAC_EPOCH - datetime(year, 1, 1, tzinfo=timezone.utc)) // step)
    return f"{year % 100:02d}{n - first + 1:02d}"


def faa_idents(dtpp_xml):
    """The FAA codes (apt_ident DFW and icao_ident KDFW) of every airport with at least
    one plate in a d-TPP metafile."""
    out = set()
    for apt in ET.fromstring(dtpp_xml).iter("airport_name"):
        if apt.find(".//record") is not None:
            out |= {apt.get("apt_ident") or "", apt.get("icao_ident") or ""}
    out.discard("")
    return out


def build_info(airports_csv, runways_csv, freqs_csv, faa):
    """Info rows (see the docstring) for the airports build() keeps, by the same codes."""
    details, radio = {}, {}
    for r in csv.DictReader(io.StringIO(runways_csv)):
        names = [(r.get(k) or "").strip().upper() for k in ("le_ident", "he_ident")]
        if r.get("closed") == "1" or not names[0] or (names[0].startswith("H") and not names[1]):
            continue                                  # closed, unnamed, or a helipad
        length, width = _num(r.get("length_ft")), _num(r.get("width_ft"))
        details.setdefault(r.get("airport_ident") or "", []).append(
            ["/".join(n for n in names if n), int(length) if length else None, int(width) if width else None,
             _surface(r.get("surface")), 1 if r.get("lighted") == "1" else 0])
    for r in csv.DictReader(io.StringIO(freqs_csv)):
        kind = (r.get("type") or "").strip().upper()
        mhz = _mhz(r.get("frequency_mhz"))
        if mhz and kind:
            label = RADIO_LABEL.get(kind, kind)
            radio.setdefault(r.get("airport_ident") or "", {}).setdefault(label, set()).add(mhz)
    rows = {}
    for code, cat, _lat, _lon, r in _kept(airports_csv, _paved(runways_csv)):
        if code in rows and "lms".index(rows[code][0]) <= "lms".index(cat):
            continue                                  # a code two airports share: the bigger one's
        ident = r.get("ident") or ""
        lid = ""
        if (r.get("iso_country") or "") in FAA_COUNTRIES:
            local = (r.get("local_code") or "").strip().upper()
            lid = local if local and local != code else ""
        plates = 1 if (lid or code) in faa or code in faa else 0
        rws = sorted(details.get(ident, []), key=lambda w: (-(w[1] or 0), w[0]))
        rad = sorted(([label, ", ".join(sorted(f, key=float))] for label, f in radio.get(ident, {}).items()),
                     key=lambda x: (RADIO_ORDER.get(x[0], len(RADIO)), x[0]))
        web, wiki = _link(r.get("home_link")), _link(r.get("wikipedia_link"), "wikipedia.org")
        rows[code] = (cat, [code, lid, plates, web, wiki, rws, rad])
    return [row for _, (_, row) in sorted(rows.items()) if any(row[1:])]


def write_info(rows, out=INFO_OUT, built=None, cycle=""):
    built = built or date.today().isoformat()
    source = "OurAirports (ourairports.com), public domain (The Unlicense)" + (
        f"; which airports have plates: FAA d-TPP cycle {cycle} (US government)" if cycle else "")
    head = json.dumps({"source": source, "built": built, "count": len(rows)})[:-1]
    body = ",\n".join(json.dumps(a, separators=(",", ":"), ensure_ascii=False) for a in rows)
    out.write_text(head + ', "airports": [\n' + body + "\n]}\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--airports", default="airports.csv", help="OurAirports airports.csv (path; downloaded when absent)")
    ap.add_argument("--runways", default="runways.csv", help="OurAirports runways.csv (path; downloaded when absent)")
    ap.add_argument("--frequencies", default="airport-frequencies.csv",
                    help="OurAirports airport-frequencies.csv (path; downloaded when absent)")
    ap.add_argument("--dtpp", default=None, help="FAA d-TPP metafile (path; the current cycle's is downloaded when absent)")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--info-out", default=str(INFO_OUT))
    ap.add_argument("--built", default=None, help="date to record (default today)")
    a = ap.parse_args(argv)
    airports_csv, runways_csv = _read(a.airports), _read(a.runways)
    rows = build(airports_csv, runways_csv)
    write(rows, Path(a.out), a.built)
    counts = {c: sum(1 for r in rows if r[1] == c) for c in "lms"}
    print(f"wrote {a.out}: {len(rows)} airports (major {counts['l']}, regional {counts['m']}, small {counts['s']})")
    cycle = airac_cycle()
    faa = faa_idents(_read(a.dtpp or DTPP_URL.format(cycle=cycle)))
    info = build_info(airports_csv, runways_csv, _read(a.frequencies), faa)
    write_info(info, Path(a.info_out), a.built, cycle)
    print(f"wrote {a.info_out}: {len(info)} airports, {sum(1 for r in info if r[2])} with FAA plates (cycle {cycle})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
