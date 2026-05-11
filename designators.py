"""
ICAO code lookup tables.

Aerodrome receives callsigns and aircraft-type strings in ICAO's 3-letter
(airline) and 3-to-4-character (type) designator format. Most people outside
aviation don't recognize "DAL" as Delta or "A321" as an Airbus A321 on sight.
These tables provide friendly display names for the codes that appear in
real-world ADS-B traffic — common commercial airlines, business/cargo
operators, and the aircraft types that actually fly.

Scope is deliberately pragmatic, not exhaustive. There are ~1,700 assigned
ICAO airline designators and several hundred aircraft type codes; shipping
complete tables from some authoritative source would bloat the package for
very little real benefit. Instead we cover the codes you'll actually see —
around 150 airlines (the major commercial carriers, popular cargo haulers,
and business-jet charter operators for large hubs) and around 90 aircraft
types (everything from a Cessna 172 to a Boeing 747).

If a code isn't in the table, the UI falls back to showing just the raw
code, which is the current behavior for everyone. Adding new entries is a
one-line PR.

Sources for initial population:
  - Airlines: ICAO Doc 8585 (current assignments as of this writing).
  - Aircraft types: ICAO Doc 8643 (type designators), cross-referenced
    with the FAA's aircraft-type designator list.

Note that we use the ICAO 3-letter airline code (e.g. DAL, UAL, SWA), NOT
the 2-letter IATA code (DL, UA, WN). ADS-B callsigns carry the ICAO form —
a flight from Delta appears as "DAL512", never "DL512". Some aircraft type
codes here overlap with common variants (B738 for 737-800, B39M for
737 MAX 9, etc.); the mapping aims for what an average reader would want
to see, not the last bit of sub-variant detail.
"""

# ---------------------------------------------------------------------------
# Airlines — ICAO 3-letter designators to common display names.
# ---------------------------------------------------------------------------
# Covering: the major North American carriers, major international carriers
# that operate long-haul service into US/Canadian airspace, the biggest
# cargo operators, and large business-jet charter companies (which appear
# frequently near Class B airports). Sorted alphabetically by code.
AIRLINES = {
    # North American majors
    "AAL": "American Airlines",
    "ACA": "Air Canada",
    "AAY": "Allegiant Air",
    "ASA": "Alaska Airlines",
    "DAL": "Delta Air Lines",
    "FFT": "Frontier Airlines",
    "HAL": "Hawaiian Airlines",
    "JBU": "JetBlue",
    "NKS": "Spirit Airlines",
    "SKW": "SkyWest",
    "SWA": "Southwest Airlines",
    "UAL": "United Airlines",
    "WJA": "WestJet",

    # North American regionals (heavily present at hub airports)
    "ASH": "Mesa Airlines",
    "AWI": "Air Wisconsin",
    "EDV": "Endeavor Air",
    "ENY": "Envoy Air",
    "GJS": "GoJet Airlines",
    "JIA": "PSA Airlines",
    "PDT": "Piedmont Airlines",
    "QXE": "Horizon Air",
    "RPA": "Republic Airways",
    "RUM": "Air Rum",
    "SCX": "Sun Country",
    "TCF": "Shuttle America",
    "WEN": "Encore Air",

    # European carriers (transatlantic + major European flags)
    "AEA": "Air Europa",
    "AFL": "Aeroflot",
    "AFR": "Air France",
    "AUA": "Austrian Airlines",
    "AZA": "ITA Airways",
    "BAW": "British Airways",
    "BEL": "Brussels Airlines",
    "DLH": "Lufthansa",
    "EIN": "Aer Lingus",
    "EZY": "easyJet",
    "FIN": "Finnair",
    "IBE": "Iberia",
    "ICE": "Icelandair",
    "KLM": "KLM",
    "NAX": "Norwegian",
    "RYR": "Ryanair",
    "SAS": "Scandinavian Airlines",
    "SWR": "Swiss",
    "TAP": "TAP Air Portugal",
    "THY": "Turkish Airlines",
    "VIR": "Virgin Atlantic",
    "WZZ": "Wizz Air",

    # Middle East
    "ETD": "Etihad Airways",
    "QTR": "Qatar Airways",
    "UAE": "Emirates",

    # Asia-Pacific
    "AAR": "Asiana Airlines",
    "ANA": "All Nippon Airways",
    "CCA": "Air China",
    "CES": "China Eastern",
    "CPA": "Cathay Pacific",
    "CSN": "China Southern",
    "JAL": "Japan Airlines",
    "KAL": "Korean Air",
    "PAL": "Philippine Airlines",
    "QFA": "Qantas",
    "SIA": "Singapore Airlines",
    "THA": "Thai Airways",
    "VJC": "VietJet",

    # Latin America
    "AMX": "Aeromexico",
    "ARG": "Aerolineas Argentinas",
    "AVA": "Avianca",
    "LAN": "LATAM Chile",
    "TAM": "LATAM Brasil",
    "VOI": "Volaris",

    # Cargo — these show up a LOT near hub airports
    "ABX": "ABX Air",
    "ATN": "Atlas Air",
    "CLX": "Cargolux",
    "FDX": "FedEx",
    "GTI": "Atlas Air Cargo",
    "NCA": "Nippon Cargo",
    "PAC": "Polar Air Cargo",
    "UPS": "UPS",
    "WGN": "Western Global",

    # Charter, fractional, and business-jet operators (large fleets, common
    # in reports from major hubs)
    "EJA": "NetJets",
    "EJM": "Executive Jet Management",
    "FLG": "Flight Options",
    "JTL": "Jet Linx",
    "LXJ": "Flexjet",
    "TAV": "TAG Aviation",
    "TVF": "Trans Travel",
    "VJT": "VistaJet",
    "XOJ": "JetSuiteX",

    # Government / military callsigns commonly seen in civilian airspace
    "RCH": "USAF Reach",           # USAF logistics callsign
    "CNV": "USN Convoy",
    "BOXR": "US Marines Boxer",
    "HOMR": "USAF Homer",
}


# ---------------------------------------------------------------------------
# Aircraft types — ICAO type designators to common display names.
# ---------------------------------------------------------------------------
# These are the codes you actually see in ADS-B type fields. Covers major
# commercial airliners, common general aviation, helicopters, and the
# business-jet fleet. Sorted by family.
AIRCRAFT_TYPES = {
    # Airbus narrowbody (A320 family)
    "A318": "Airbus A318",
    "A319": "Airbus A319",
    "A320": "Airbus A320",
    "A321": "Airbus A321",
    "A20N": "Airbus A320neo",
    "A21N": "Airbus A321neo",
    "A19N": "Airbus A319neo",

    # Airbus widebody
    "A306": "Airbus A300",
    "A310": "Airbus A310",
    "A332": "Airbus A330-200",
    "A333": "Airbus A330-300",
    "A339": "Airbus A330-900neo",
    "A338": "Airbus A330-800neo",
    "A342": "Airbus A340-200",
    "A343": "Airbus A340-300",
    "A346": "Airbus A340-600",
    "A359": "Airbus A350-900",
    "A35K": "Airbus A350-1000",
    "A388": "Airbus A380-800",

    # Boeing 737
    "B731": "Boeing 737-100",
    "B732": "Boeing 737-200",
    "B733": "Boeing 737-300",
    "B734": "Boeing 737-400",
    "B735": "Boeing 737-500",
    "B736": "Boeing 737-600",
    "B737": "Boeing 737-700",
    "B738": "Boeing 737-800",
    "B739": "Boeing 737-900",
    "B37M": "Boeing 737 MAX 7",
    "B38M": "Boeing 737 MAX 8",
    "B39M": "Boeing 737 MAX 9",
    "B3XM": "Boeing 737 MAX 10",

    # Boeing widebody
    "B742": "Boeing 747-200",
    "B744": "Boeing 747-400",
    "B748": "Boeing 747-8",
    "B752": "Boeing 757-200",
    "B753": "Boeing 757-300",
    "B762": "Boeing 767-200",
    "B763": "Boeing 767-300",
    "B764": "Boeing 767-400",
    "B772": "Boeing 777-200",
    "B77L": "Boeing 777-200LR",
    "B773": "Boeing 777-300",
    "B77W": "Boeing 777-300ER",
    "B778": "Boeing 777-8",
    "B779": "Boeing 777-9",
    "B788": "Boeing 787-8",
    "B789": "Boeing 787-9",
    "B78X": "Boeing 787-10",

    # Embraer regionals (very common in North America)
    "E135": "Embraer ERJ-135",
    "E145": "Embraer ERJ-145",
    "E170": "Embraer 170",
    "E75L": "Embraer 175 (long wing)",
    "E75S": "Embraer 175 (short wing)",
    "E190": "Embraer 190",
    "E195": "Embraer 195",
    "E290": "Embraer E190-E2",
    "E295": "Embraer E195-E2",

    # Bombardier regionals
    "CRJ1": "Bombardier CRJ-100",
    "CRJ2": "Bombardier CRJ-200",
    "CRJ7": "Bombardier CRJ-700",
    "CRJ9": "Bombardier CRJ-900",
    "CRJX": "Bombardier CRJ-1000",

    # De Havilland / Bombardier turboprops
    "DH8A": "Dash 8-100",
    "DH8B": "Dash 8-200",
    "DH8C": "Dash 8-300",
    "DH8D": "Dash 8-400",
    "AT72": "ATR 72",
    "AT75": "ATR 72-500",
    "AT76": "ATR 72-600",
    "AT43": "ATR 42",

    # Business jets (common at urban receivers)
    "C25A": "Cessna Citation CJ2",
    "C25B": "Cessna Citation CJ3",
    "C25C": "Cessna Citation CJ4",
    "C560": "Cessna Citation V",
    "C56X": "Cessna Citation Excel",
    "C680": "Cessna Citation Sovereign",
    "C68A": "Cessna Citation Latitude",
    "C700": "Cessna Citation Longitude",
    "C750": "Cessna Citation X",
    "GLF4": "Gulfstream IV",
    "GLF5": "Gulfstream V",
    "GLF6": "Gulfstream G650",
    "G280": "Gulfstream G280",
    "CL30": "Challenger 300",
    "CL35": "Challenger 350",
    "CL60": "Challenger 600",
    "GL5T": "Global 5000",
    "GLEX": "Global Express",
    "LJ35": "Learjet 35",
    "LJ45": "Learjet 45",
    "LJ60": "Learjet 60",
    "LJ75": "Learjet 75",
    "E55P": "Embraer Phenom 300",
    "E50P": "Embraer Phenom 100",
    "HDJT": "HondaJet",

    # Common single-engine piston (general aviation)
    "C152": "Cessna 152",
    "C162": "Cessna Skycatcher",
    "C172": "Cessna 172 Skyhawk",
    "C177": "Cessna Cardinal",
    "C182": "Cessna 182 Skylane",
    "C206": "Cessna 206",
    "C208": "Cessna Caravan",
    "C210": "Cessna Centurion",
    "P28A": "Piper Cherokee",
    "P28R": "Piper Arrow",
    "PA44": "Piper Seminole",
    "PA46": "Piper Malibu",
    "SR20": "Cirrus SR20",
    "SR22": "Cirrus SR22",
    "M20P": "Mooney M20",
    "DA40": "Diamond DA40",
    "DA42": "Diamond DA42",
    "BE36": "Beechcraft Bonanza",
    "BE58": "Beechcraft Baron",

    # Turboprops (common cargo + GA)
    "BE20": "Beechcraft King Air 200",
    "BE30": "Beechcraft King Air 350",
    "BE40": "Beechjet 400",
    "PC12": "Pilatus PC-12",
    "TBM9": "TBM 900",
    "TBM8": "TBM 850",

    # Helicopters (seen in dense urban environments)
    "B06": "Bell 206",
    "B407": "Bell 407",
    "B429": "Bell 429",
    "B505": "Bell 505",
    "EC20": "Eurocopter EC120",
    "EC30": "Eurocopter EC130",
    "EC35": "Eurocopter EC135",
    "EC45": "Eurocopter EC145",
    "H60": "Sikorsky Black Hawk",
    "R22": "Robinson R22",
    "R44": "Robinson R44",
    "R66": "Robinson R66",

    # Military aircraft frequently seen over civilian airspace
    "C17": "Boeing C-17 Globemaster",
    "C130": "Lockheed C-130 Hercules",
    "C5M": "Lockheed C-5M Galaxy",
    "KC46": "Boeing KC-46 Pegasus",
    "KC10": "McDonnell Douglas KC-10",
    "KC35": "Boeing KC-135 Stratotanker",
    "E3CF": "Boeing E-3 Sentry (AWACS)",
    "E6": "Boeing E-6 Mercury",
    "P8": "Boeing P-8 Poseidon",
    "V22": "Bell-Boeing V-22 Osprey",
    "F16": "General Dynamics F-16",
    "F18": "McDonnell Douglas F/A-18",
    "F35": "Lockheed Martin F-35",
    "A10": "Fairchild A-10 Thunderbolt",
    "B52": "Boeing B-52 Stratofortress",
}


def airline_name(code: str) -> str | None:
    """Look up a friendly name for an ICAO airline designator. Returns None
    when the code isn't known, so callers can choose how to display it."""
    if not code:
        return None
    return AIRLINES.get(code.strip().upper())


def operator_from_callsign(callsign: str) -> str | None:
    """v2.50.42: derive ICAO airline designator from a callsign.

    Convention: airline callsigns start with the airline's 3-letter ICAO
    code followed by a flight number (e.g. "UAL2024" → "UAL" → United
    Airlines). General aviation tail-number callsigns (e.g. "N12345",
    "G-XYZA") don't have a 3-letter prefix that maps to an airline; they
    return None.

    Implementation: take the first 3 characters; require them to be all
    letters; require them to exist in the AIRLINES table. Anything that
    doesn't pass returns None — we'd rather be silent than make up a
    fake operator from random callsign prefixes.

    Single source of truth: collector and backfill both call this so
    they derive operator the same way. Without that consistency, you'd
    get "operator: UAL" on aircraft seen since the change but
    "operator: NULL" on backfilled aircraft, even when their callsigns
    are identical.
    """
    if not callsign:
        return None
    c = callsign.strip().upper()
    if len(c) < 3:
        return None
    prefix = c[:3]
    if not prefix.isalpha():
        return None
    if prefix not in AIRLINES:
        return None
    return prefix


def fts_operator_string(code: str | None) -> str:
    """v2.50.42: build the FTS5-tokenizable string for an operator.

    Returns "{code} {name}" when both are known (e.g. "UAL United Airlines"),
    "{code}" when the code is set but unknown to AIRLINES (shouldn't happen
    if operator_from_callsign is the only writer, but defensive), and
    empty string when no code is set.

    The empty-string return is important: the FTS5 flush copies this into
    the `operator` FTS column verbatim, so an unset operator column
    becomes an empty FTS row rather than a literal "None" string that
    could match unrelated queries.
    """
    if not code:
        return ""
    name = AIRLINES.get(code.strip().upper())
    if name:
        return f"{code} {name}"
    return code


def aircraft_type_name(code: str) -> str | None:
    """Look up a friendly name for an ICAO aircraft type designator."""
    if not code:
        return None
    return AIRCRAFT_TYPES.get(code.strip().upper())
