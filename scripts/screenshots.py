"""
Render all documentation screenshots for the Aerodrome README.

Runs Playwright against the actual HTML templates using synthetic mock data,
so no live server or real aircraft data is needed. Output goes to docs/ in
the repo root and overwrites the existing PNGs.

Usage (from the repo root):
    pip install playwright
    playwright install chromium
    python3 scripts/screenshots.py

The mock data is deliberately generic — generic Bay Area coordinates,
reserved-for-documentation receiver IP 192.0.2.10, and fictional callsigns.
No real receiver, home location, or personal watchlist is ever referenced.

To add a new screenshot:
  1. Add a new `screenshot_foo()` function below
  2. Append it to the list at the bottom of main()
  3. Add a corresponding entry to README.md's Screenshots section
"""
import asyncio
import json
import random
import time
from pathlib import Path

from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

REPO_ROOT    = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / 'templates'
OUT_DIR      = REPO_ROOT / 'docs'
TMP_DIR      = Path('/tmp')  # where rendered-template scratch files go
VERSION      = (REPO_ROOT / 'VERSION').read_text().strip()  # for screenshots

OUT_DIR.mkdir(exist_ok=True)

NOW       = int(time.time())
DAY_START = NOW - 8 * 3600

# Deterministic seed so chart bars etc. look the same across runs. This is
# cosmetic only — no real data is derived from it.
random.seed(42)

# ---------------------------------------------------------------------------
# Synthetic data
# ---------------------------------------------------------------------------

UI_CFG = {
    "distance_enabled": True,
    "distance_unit": "mi",
    "track_link_provider": "airplanes_live",
    # v2.50.24: the harness was missing these — without them the UI's
    # track_link rendering falls through to "—" because it can't look up
    # the provider's URL template. Mirrors the canonical registry in
    # collector.py:TRACK_LINK_PROVIDERS, kept in sync manually (the list
    # changes rarely, and adding a runtime import here would tangle the
    # harness with collector module loading).
    "track_link_providers": {
        "airplanes_live":  {"label": "airplanes.live",  "url": "https://globe.airplanes.live/?icao={HEX_LOWER}",          "reg_required": False},
        "flightaware":     {"label": "FlightAware",     "url": "https://flightaware.com/live/modes/{HEX_UPPER}/redirect", "reg_required": False},
        "flightradar24":   {"label": "Flightradar24",   "url": "https://www.flightradar24.com/data/aircraft/{REG_LOWER}", "reg_required": True},
        "airnavradar":     {"label": "AirNavRadar",     "url": "https://www.airnavradar.com/data/registration/{REG_UPPER}", "reg_required": True},
        "planefinder":     {"label": "PlaneFinder",     "url": "https://planefinder.net/data/aircraft/{REG_UPPER}",      "reg_required": True},
    },
    "track_link_fallback": "airplanes_live",
    "default_military_color": "#ef4444",
    "watchlist_alerts": {
        "enabled": True, "trigger": "live", "effect": "pulse_dot",
        "color": "#f59e0b",
    },
    "stats": {
        "enabled": True, "refresh_interval": 300,
        "timezone": "America/Los_Angeles",
        "track_gap_minutes": 5,
        "cards": {}, "groups": {},
        "new_record_alerts": {
            "enabled": True, "color": "#22c55e",
            "dismiss_after_seconds": 30,
        },
    },
}

# --- Live tab: mix of commercial, GA, military -----------------------------
LIVE_AIRCRAFT = [
    {"icao":"A00001","callsign":"UAL1234","speed":452,"lat":37.41,"lon":-122.07,"altitude":35000,"aircraft_type":"B737","type_desc":"Boeing 737-800","distance":18.2,"is_military":False,"seen_at":NOW},
    {"icao":"A12345","callsign":"DAL512","speed":438,"lat":37.55,"lon":-122.18,"altitude":32000,"aircraft_type":"A321","type_desc":"Airbus A321","distance":24.5,"is_military":False,"seen_at":NOW},
    {"icao":"AB0777","callsign":"UAL901","speed":490,"lat":37.62,"lon":-121.89,"altitude":41000,"aircraft_type":"B777","type_desc":"Boeing 777-300ER","distance":36.7,"is_military":False,"seen_at":NOW},
    {"icao":"A55599","callsign":"SWA2024","speed":412,"lat":37.34,"lon":-122.31,"altitude":28500,"aircraft_type":"B738","type_desc":"Boeing 737-800","distance":14.1,"is_military":False,"seen_at":NOW},
    {"icao":"AE01CE","callsign":"RCH42","speed":365,"lat":37.51,"lon":-121.95,"altitude":26000,"aircraft_type":"C17","type_desc":"Boeing C-17A Globemaster III","distance":33.4,"is_military":True,"mil_color":"#ef4444","mil_label":"MIL","seen_at":NOW},
    {"icao":"AE093F","callsign":"GRZLY39","speed":310,"lat":37.67,"lon":-122.41,"altitude":18000,"aircraft_type":"C56X","type_desc":"Cessna 560XL Citation","distance":19.8,"is_military":True,"mil_color":"#ef4444","mil_label":"MIL","seen_at":NOW},
    {"icao":"A134D2","callsign":"N177SV","speed":95,"lat":37.45,"lon":-122.04,"altitude":3200,"aircraft_type":"C177","type_desc":"Cessna 177 Cardinal","distance":8.1,"is_military":False,"seen_at":NOW},
    {"icao":"AAAD47","callsign":"N78729","speed":80,"lat":37.43,"lon":-122.02,"altitude":1500,"aircraft_type":"C172","type_desc":"Cessna 172","distance":6.4,"is_military":False,"seen_at":NOW},
    {"icao":"AD7E6F","callsign":"JBU934","speed":497,"lat":37.78,"lon":-122.25,"altitude":39000,"aircraft_type":"A321","type_desc":"Airbus A321","distance":28.6,"is_military":False,"seen_at":NOW},
    {"icao":"C2B369","callsign":"CFC3136","speed":285,"lat":37.60,"lon":-121.78,"altitude":24000,"aircraft_type":"CL60","type_desc":"Bombardier CL-600","distance":42.3,"is_military":True,"mil_color":"#ef4444","mil_label":"MIL","seen_at":NOW},
    {"icao":"C2B2DD","callsign":"CFC555","speed":305,"lat":37.49,"lon":-122.33,"altitude":27000,"aircraft_type":"DH8D","type_desc":"De Havilland Dash 8-400","distance":15.2,"is_military":True,"mil_color":"#ef4444","mil_label":"MIL","seen_at":NOW},
    {"icao":"480C41","callsign":"MMF50","speed":220,"lat":37.55,"lon":-122.19,"altitude":15000,"aircraft_type":"A332","type_desc":"Airbus A330-200","distance":24.0,"is_military":True,"mil_color":"#ef4444","mil_label":"MIL","seen_at":NOW},
    {"icao":"A67890","callsign":"ASQ567","speed":421,"lat":37.28,"lon":-122.42,"altitude":31000,"aircraft_type":"E175","type_desc":"Embraer E175","distance":16.9,"is_military":False,"seen_at":NOW},
]

# --- Watchlist tab (grouped {latest, sightings}) ---------------------------
WATCHLIST_GROUPS = [
    {"latest":{"icao":"AAAD47","callsign":"N78729","aircraft_type":"C172","type_desc":"Cessna 172","altitude":1500,"speed":80,"lat":37.43,"lon":-122.02,"distance":6.4,"seen_at":NOW,"watchlist_label":"Friend's Cessna","is_military":False},"sightings":[]},
    {"latest":{"icao":"A67890","callsign":"ASQ567","aircraft_type":"E175","type_desc":"Embraer E175","altitude":31000,"speed":421,"lat":37.28,"lon":-122.42,"distance":16.9,"seen_at":NOW-180,"watchlist_label":"Brother's airline","is_military":False},"sightings":[]},
]

# --- Military tab (grouped) ------------------------------------------------
_MIL_FLAT = [
    {"icao":"AE01CE","callsign":"SHINR40","speed":365,"lat":37.51,"lon":-121.95,"altitude":26000,"aircraft_type":"R135","type_desc":"Boeing RC-135V Rivet Joint","distance":33.4,"is_military":True,"mil_color":"#ef4444","mil_label":"MIL","seen_at":NOW-120,"special_label":""},
    {"icao":"AE093F","callsign":"GRZLY39","speed":310,"lat":37.67,"lon":-122.41,"altitude":18000,"aircraft_type":"C56X","type_desc":"Cessna 560XL Citation","distance":19.8,"is_military":True,"mil_color":"#ef4444","mil_label":"MIL","seen_at":NOW-180,"special_label":""},
    {"icao":"C2B369","callsign":"CFC3136","speed":285,"lat":37.60,"lon":-121.78,"altitude":24000,"aircraft_type":"CL60","type_desc":"Bombardier CL-600","distance":42.3,"is_military":True,"mil_color":"#ef4444","mil_label":"MIL","seen_at":NOW-60,"special_label":""},
    {"icao":"ADFEEF","callsign":"ADFFBF","speed":0,"lat":None,"lon":None,"altitude":None,"aircraft_type":"T38","type_desc":"Northrop T-38 Talon","distance":None,"is_military":True,"mil_color":"#ef4444","mil_label":"MIL","seen_at":NOW-300,"special_label":""},
    {"icao":"C2B2DD","callsign":"CFC555","speed":305,"lat":37.49,"lon":-122.33,"altitude":27000,"aircraft_type":"DH8D","type_desc":"De Havilland Dash 8-400","distance":15.2,"is_military":True,"mil_color":"#ef4444","mil_label":"MIL","seen_at":NOW-240,"special_label":""},
    {"icao":"480C41","callsign":"MMF50","speed":220,"lat":37.55,"lon":-122.19,"altitude":15000,"aircraft_type":"A332","type_desc":"Airbus A330-200","distance":24.0,"is_military":True,"mil_color":"#ef4444","mil_label":"MIL","seen_at":NOW-420,"special_label":""},
]
MILITARY_AIRCRAFT = [{"latest": ac, "sightings": []} for ac in _MIL_FLAT]

# v2.67.0: the All-tab data fixture was removed alongside the All tab itself.
# Search is the canonical browse-every-aircraft surface now and has its own
# screenshot path (screenshot_search) backed by the SEARCH_RESPONSE fixture
# below.

# --- Search tab response (v2.77.0 — adds screenshot_search) ----------------
# Mock /api/search response. The frontend's runSearch() passes ?q=today and
# we synthesize a believable mixed-fleet result set. The row shape mirrors
# search.py's execute_search() return — search.py:898 lists every key the
# frontend's renderer expects. Distance is in km (last_distance_km); the
# server normally converts to user-units at response time, but for the
# screenshot we just supply km values that look reasonable when rendered.
#
# Why query="today": demonstrates the v2.65.0 relative-date token (a
# recent flagship feature). The parsed_filters[] entry here represents
# what parse_query() emits for the "today" token — a time_range filter
# that resolves at parse time. The frontend renders this as a chip in
# the Search tab's chip strip, which is a nice visual to feature in
# the README.
_SEARCH_T0 = NOW
SEARCH_ROWS = [
    {"icao":"A12345","registration":"N512DL","last_callsign":"DAL512","callsign":"DAL512",
     "aircraft_type":"A321","aircraft_type_desc":"Airbus A321","operator":"Delta Air Lines",
     "country":"United States","last_lat":37.55,"last_lon":-122.18,
     "last_seen_at":_SEARCH_T0-90,"sighting_count":47,"first_seen_at":_SEARCH_T0-9000,
     "last_speed":438,"last_altitude":32000,"last_squawk":"3401",
     "last_distance_km":39.4,"distance":24.5,"score":1.0},
    {"icao":"AB0777","registration":"N777UA","last_callsign":"UAL901","callsign":"UAL901",
     "aircraft_type":"B777","aircraft_type_desc":"Boeing 777-300ER","operator":"United Airlines",
     "country":"United States","last_lat":37.62,"last_lon":-121.89,
     "last_seen_at":_SEARCH_T0-150,"sighting_count":12,"first_seen_at":_SEARCH_T0-7200,
     "last_speed":490,"last_altitude":41000,"last_squawk":"2654",
     "last_distance_km":59.0,"distance":36.7,"score":0.95},
    {"icao":"A00001","registration":"N1234U","last_callsign":"UAL1234","callsign":"UAL1234",
     "aircraft_type":"B737","aircraft_type_desc":"Boeing 737-800","operator":"United Airlines",
     "country":"United States","last_lat":37.41,"last_lon":-122.07,
     "last_seen_at":_SEARCH_T0-300,"sighting_count":89,"first_seen_at":_SEARCH_T0-21600,
     "last_speed":452,"last_altitude":35000,"last_squawk":"3215",
     "last_distance_km":29.3,"distance":18.2,"score":0.92},
    {"icao":"AD7E6F","registration":"N934JB","last_callsign":"JBU934","callsign":"JBU934",
     "aircraft_type":"A321","aircraft_type_desc":"Airbus A321","operator":"JetBlue",
     "country":"United States","last_lat":37.78,"last_lon":-122.25,
     "last_seen_at":_SEARCH_T0-420,"sighting_count":23,"first_seen_at":_SEARCH_T0-43200,
     "last_speed":497,"last_altitude":39000,"last_squawk":"4711",
     "last_distance_km":46.0,"distance":28.6,"score":0.88},
    {"icao":"A55599","registration":"N555SW","last_callsign":"SWA2024","callsign":"SWA2024",
     "aircraft_type":"B738","aircraft_type_desc":"Boeing 737-800","operator":"Southwest",
     "country":"United States","last_lat":37.34,"last_lon":-122.31,
     "last_seen_at":_SEARCH_T0-600,"sighting_count":156,"first_seen_at":_SEARCH_T0-86400*3,
     "last_speed":412,"last_altitude":28500,"last_squawk":"1247",
     "last_distance_km":22.7,"distance":14.1,"score":0.84},
    {"icao":"AE01CE","registration":None,"last_callsign":"RCH42","callsign":"RCH42",
     "aircraft_type":"C17","aircraft_type_desc":"Boeing C-17A Globemaster III",
     "operator":"USAF","country":"United States","last_lat":37.51,"last_lon":-121.95,
     "last_seen_at":_SEARCH_T0-900,"sighting_count":5,"first_seen_at":_SEARCH_T0-28800,
     "last_speed":365,"last_altitude":26000,"last_squawk":"1200",
     "last_distance_km":53.7,"distance":33.4,"score":0.81,
     "is_military":True,"mil_color":"#ef4444","mil_label":"MIL"},
    {"icao":"AAAD47","registration":"N78729","last_callsign":"N78729","callsign":"N78729",
     "aircraft_type":"C172","aircraft_type_desc":"Cessna 172",
     "operator":None,"registered_owner":"BAY AREA FLYING CLUB INC","manufacturer":"Cessna",
     "country":"United States","last_lat":37.43,"last_lon":-122.02,
     "last_seen_at":_SEARCH_T0-1200,"sighting_count":3,"first_seen_at":_SEARCH_T0-1200,
     "last_speed":80,"last_altitude":1500,"last_squawk":"1200",
     "last_distance_km":10.3,"distance":6.4,"score":0.78,
     "is_watchlist":True,"watchlist_label":"Friend's Cessna"},
    {"icao":"C2B369","registration":"C-FCFC","last_callsign":"CFC3136","callsign":"CFC3136",
     "aircraft_type":"CL60","aircraft_type_desc":"Bombardier CL-600","operator":"RCAF",
     "country":"Canada","last_lat":37.60,"last_lon":-121.78,
     "last_seen_at":_SEARCH_T0-1800,"sighting_count":2,"first_seen_at":_SEARCH_T0-1800,
     "last_speed":285,"last_altitude":24000,"last_squawk":"1200",
     "last_distance_km":68.0,"distance":42.3,"score":0.75,
     "is_military":True,"mil_color":"#ef4444","mil_label":"MIL"},
]
SEARCH_RESPONSE = {
    "ok": True,
    "query": "today",
    # The frontend's chip-strip renderer reads parsed_filters and
    # renders a single chip for each entry. "today" parses to a
    # time_range filter; the frontend special-cases it as a "Today"
    # chip.
    "parsed_filters": [
        {"field": "time_range", "match": "today",
         "value": [_SEARCH_T0 - (_SEARCH_T0 % 86400), _SEARCH_T0 + 86400]},
    ],
    "free_text": [],
    "time_range": [_SEARCH_T0 - (_SEARCH_T0 % 86400), _SEARCH_T0 + 86400],
    "total_count": len(SEARCH_ROWS),
    "rows": SEARCH_ROWS,
    "execution_ms": 14.2,
    "error": None,
}

# --- Stats payload (8 sections worth of cards) -----------------------------
STATS = {
    "enabled": True, "day_start_ts": DAY_START, "now_ts": NOW,
    "timezone": "America/Los_Angeles",
    "groups": [
        {"id":"today","label":"Today","cards":["unique_today","peak_simultaneous","average_concurrent","military_today","watchlist_hits","first_last_contact"]},
        {"id":"extremes","label":"Today's extremes","cards":["furthest","highest_altitude","lowest_altitude","fastest","slowest","longest_track"]},
        {"id":"composition","label":"Composition","cards":["top_aircraft","top_types","top_operators","military_branches","category_mix","top_countries"]},
        {"id":"patterns","label":"Patterns","cards":["hourly_histogram"]},
        {"id":"history","label":"History","cards":["first_time_seen","daily_counts_7d","watchlist_frequency"]},
        {"id":"records","label":"All-time records","cards":["all_time_records"]},
        {"id":"coverage","label":"Coverage","cards":["range_rose","distance_histogram"]},
    ],
    "cards": {
        "unique_today": 242,
        "peak_simultaneous": 57,
        "average_concurrent": 44.4,
        "military_today": 6,
        "watchlist_hits": 24,
        "first_last_contact": {
            "first": {"icao":"A00001","callsign":"UAL1234","seen_at":DAY_START+300},
            "last":  {"icao":"A67890","callsign":"ASQ567", "seen_at":NOW-180},
        },
        "furthest":         {"icao":"AE01CE","callsign":"RCH42","distance":420,"unit":"mi","bearing":220,"aircraft_type":"C17"},
        "highest_altitude": {"icao":"AB0777","callsign":"UAL901","altitude":43000,"aircraft_type":"B777"},
        "lowest_altitude":  {"icao":"A134D2","callsign":"N177SV","altitude":1000,"aircraft_type":"C177"},
        "fastest":          {"icao":"AD7E6F","callsign":"JBU934","speed":597,"aircraft_type":"A321"},
        "slowest":          {"icao":"AAAD47","callsign":"N78729","speed":84,"aircraft_type":"C172"},
        "longest_track":    {"icao":"AAA86C","callsign":"ENY3987","duration_seconds":4230},
        "top_aircraft": [
            {"icao":"A9F31C","last_callsign":"UPS2877","aircraft_type":"B763","registration":"N363UP","operator":"UPS","n":2841},
            {"icao":"AB22E8","last_callsign":"SKW5193","aircraft_type":"CRJ7","registration":"N728SK","operator":"SKW","n":2104},
            {"icao":"AC8E40","last_callsign":"FFT1612","aircraft_type":"A321","registration":"N701FR","operator":"FFT","n":1873},
            {"icao":"A4D2B1","last_callsign":"N511MK","aircraft_type":"BE20","registration":"N511MK","operator":"","n":1652},
            {"icao":"ADE419","last_callsign":"AAL1148","aircraft_type":"B738","registration":"N916NN","operator":"AAL","n":1438},
        ],
        "top_types": [
            {"aircraft_type":"B738","n":90,"name":"Boeing 737-800"},
            {"aircraft_type":"A320","n":77,"name":"Airbus A320"},
            {"aircraft_type":"E75L","n":51,"name":"Embraer 175 (long wing)"},
            {"aircraft_type":"B737","n":31,"name":"Boeing 737-700"},
            {"aircraft_type":"CRJ7","n":29,"name":"Bombardier CRJ-700"},
        ],
        "top_operators": [
            {"operator":"UAL","n":88,"name":"United Airlines"},
            {"operator":"SWA","n":74,"name":"Southwest Airlines"},
            {"operator":"DAL","n":55,"name":"Delta Air Lines"},
            {"operator":"AAL","n":40,"name":"American Airlines"},
            {"operator":"SKW","n":28,"name":"SkyWest"},
        ],
        "military_branches": [{"branch":"Other","n":4}, {"branch":"Air Force","n":2}],
        "category_mix": [
            {"category":"Commercial","n":503},
            {"category":"General Aviation","n":223},
            {"category":"Military","n":6},
        ],
        # v2.50.27: countries card mock — values picked to be plausible
        # for a Bay Area receiver where US dominates with a long tail of
        # international aircraft seen at altitude.
        "top_countries": [
            {"country":"United States","n":698},
            {"country":"Canada","n":18},
            {"country":"Mexico","n":7},
            {"country":"United Kingdom","n":4},
            {"country":"Japan","n":3},
        ],
        "hourly_histogram": [
            {"hour": h, "n": max(0, 20 + random.randint(-15, 45) if 6 <= h <= 22
                                    else random.randint(0, 8))}
            for h in range(24)
        ],
        "first_time_seen": {"total":12,"list":[
            {"icao":"A12AB1","first_callsign":"N12345","first_aircraft_type":"SR22","first_seen_at":DAY_START+1200},
            {"icao":"A54321","first_callsign":"N99887","first_aircraft_type":"C172","first_seen_at":DAY_START+3400},
            {"icao":"AE2345","first_callsign":"RCH789","first_aircraft_type":"C130","first_seen_at":DAY_START+5200},
        ]},
        "daily_counts_7d": [
            {"date":"2026-04-11","label":"Sat","n":195},
            {"date":"2026-04-12","label":"Sun","n":178},
            {"date":"2026-04-13","label":"Mon","n":242},
            {"date":"2026-04-14","label":"Tue","n":225},
            {"date":"2026-04-15","label":"Wed","n":267},
            {"date":"2026-04-16","label":"Thu","n":234},
            {"date":"2026-04-17","label":"Fri","n":242},
        ],
        "watchlist_frequency": [
            {"watchlist_label":"Friend's Cessna","total_hits":87,"unique_aircraft":3},
            {"watchlist_label":"Brother's airline","total_hits":34,"unique_aircraft":1},
        ],
        "all_time_records": [
            {"record_type":"fastest_ever","value":614,"icao":"A45678","callsign":"UAL801","aircraft_type":"B77W","set_at":NOW-86400*3,"extra":""},
            {"record_type":"highest_altitude_ever","value":51000,"icao":"A99999","callsign":"N7LX","aircraft_type":"GLF6","set_at":NOW-86400*12,"extra":""},
            {"record_type":"lowest_altitude_ever","value":200,"icao":"A33333","callsign":"N54321","aircraft_type":"C172","set_at":NOW-86400*8,"extra":""},
            {"record_type":"furthest_ever","value":478.6,"icao":"A22222","callsign":"AAL123","aircraft_type":"B789","set_at":NOW-86400*20,"extra":"mi"},
            {"record_type":"peak_simultaneous_ever","value":89,"icao":"","callsign":"","aircraft_type":"","set_at":NOW-86400*5,"extra":""},
            {"record_type":"longest_track_ever","value":18000,"icao":"AAA123","callsign":"LONGHAUL","aircraft_type":"B747","set_at":NOW-86400*30,"extra":""},
        ],
        "range_rose": {
            "window":"30d","unit":"mi","total_positions":6501,
            "directions":["N","NNE","NE","ENE","E","ESE","SE","SSE",
                          "S","SSW","SW","WSW","W","WNW","NW","NNW"],
            "bucket_labels":["<50","50-100","100-150","150-200","200-250","250+"],
            "grid":[[random.randint(3, 35) for _ in range(6)] for _ in range(16)],
        },
        "distance_histogram": {
            "window":"30d","unit":"mi","total_positions":6501,
            "buckets":["<50","50-100","100-150","150-200","200-250","250+"],
            "counts":[1371, 1446, 1860, 1233, 530, 61],
        },
    }
}

# --- Status page payload ---------------------------------------------------
STATUS = {
    "overall_ok": True,
    "version": VERSION,
    "timestamp": NOW,
    "components": {
        "receiver": {"ok":True,"url":"http://192.0.2.10:8080/data/aircraft.json","response_ms":45,"error":None},
        "database": {"ok":True,"path":"/opt/aerodrome/aircraft_history.db","size_mb":45.0,
                     "stats":{"all":{"total":185234,"unique":6501},
                              "military":{"total":1452,"unique":89},
                              "watchlist":{"total":234,"unique":12}},
                     "capacity": {
                         "ok": True,
                         "db_size_mb": 45.0,
                         "rows_per_day": 18500,
                         "mb_per_day": 3.0,
                         "bytes_per_row": 170.4,
                         "days_of_data": 10.2,
                         "data_source": "measured",
                         "retention_days": 30,
                         "projected_settled_mb": 90.0,
                         "disk_free_mb": 12400.0,
                         "disk_total_mb": 40960.0,
                         "headroom_ratio": 138.3,
                         "what_if": [
                             {"days": 7,   "projected_mb": 21.0,  "headroom_ratio": 593.0},
                             {"days": 14,  "projected_mb": 42.0,  "headroom_ratio": 296.4},
                             {"days": 30,  "projected_mb": 90.0,  "headroom_ratio": 138.3},
                             {"days": 60,  "projected_mb": 180.0, "headroom_ratio": 69.1},
                             {"days": 90,  "projected_mb": 270.0, "headroom_ratio": 46.1},
                             {"days": 180, "projected_mb": 540.0, "headroom_ratio": 23.0},
                         ],
                         "error": None,
                     },
                     "error":None},
        "collector":      {"ok":True,"last_write_seconds_ago":28,"records_per_sec_5m":4.2,"records_sample_count_5m":1260,"error":None},
        "webserver":      {"ok":True,"host":"0.0.0.0","port":8000,"error":None},
        "hexdb_resolver": {"ok":True,"response_ms":112,"error":None},
    },
    "system": {
        "ok": True, "error": None,
        "uptime_seconds": 186400, "started_at": NOW - 186400,
        "cpu_percent": 12.4,
        "process_cpu_percent": 3.8,
        "cpu_cores": 4,
        "memory": {"used_mb":512, "total_mb":2048, "percent":25.0},
        "disk":   {"used_gb":14.2, "total_gb":40.0, "percent":35.5},
    },
}

# --- Watchlist chip entries -----------------------------------------------
WL_ENTRIES = [
    {"icao":"AAAD47","tail":"N78729","label":"Friend's Cessna"},
    {"icao":"A67890","tail":"","callsign":"ASQ","label":"Brother's airline"},
    {"icao":"","tail":"","model":"G650","label":"Any Gulfstream G650"},
]

# --- First-seen timestamps for inline "first seen X ago" chips ------------
# Covers ICAOs that appear on the Watchlist and Military tabs. Ages
# chosen to exercise all three formatter branches:
#   < 1 day  → "first seen today" / "first seen Xh ago"
#   < 30 day → "first seen Xd ago"
#   ≥ 30 day → "first seen Mon D, YYYY"
# Without this the chips wouldn't show up in the regenerated screenshots
# since the harness's default fetch stub returns {} for unknown routes.
FIRST_SEEN = {
    # Watchlist ICAOs
    "AAAD47": NOW - 47 * 86400,           # ~7 weeks ago → absolute date
    "A67890": NOW - 12 * 86400,           # 12d ago → relative
    # Military ICAOs (sampled, varied ages)
    "AE01CE": NOW - 3 * 86400,            # 3d ago
    "AE093F": NOW - 120 * 86400,          # ~4 months → absolute
    "C2B369": NOW - 1 * 86400,            # 1d ago
    "ADFEEF": NOW - 8 * 3600,             # 8h ago → "Xh ago"
    "C2B2DD": NOW - 18 * 86400,           # 18d ago
    "480C41": NOW - 5 * 86400,            # 5d ago
    # Live / All-tab ICAOs
    "A00001": NOW - 60 * 86400,           # 2 months → absolute
    "A12345": NOW - 2 * 86400,            # 2d ago
    "AB0777": NOW - 210 * 86400,          # ~7 months → absolute
    "A55599": NOW - 4 * 86400,            # 4d ago
    "A134D2": NOW - 22 * 86400,           # 22d ago
    "AD7E6F": NOW - 6 * 86400,            # 6d ago
}

# --- Full config used by the config.html screenshot -----------------------
FULL_CONFIG = {
    "receiver":{"ip":"192.0.2.10","port":8080,"path":"/data/aircraft.json","latitude":37.5,"longitude":-122.1,"poll_interval":60,"distance_unit":"mi","track_link_provider":"airplanes_live"},
    "web":{"host":"0.0.0.0","port":8000},
    "retention":{"all_days":30,"military_days":30,"watchlist_days":30},
    "data":{"db_file":"aircraft_history.db"},
    "logging":{"dir":"logs","level":"INFO"},
    "military":{"enabled":True,"default_color":"#ef4444","special_aircraft":{}},
    "watchlist_alerts":{"enabled":True,"trigger":"live","effect":"pulse_dot","color":"#f59e0b"},
    "stats": UI_CFG['stats'],
}

# --- Markdown samples for the docs.html screenshot ------------------------
# Short excerpts representative of each doc — enough to fill the viewer with
# realistic-looking rendered content without copying the full docs into this
# script (which would be a maintenance headache when the real docs change).
DOC_MARKDOWN = {
    "readme": """# Aerodrome

A clean, modern ADS-B aircraft tracker for your home receiver.

Aerodrome turns your local ADS-B receiver (readsb, dump1090, tar1090, etc.)
into a polished web dashboard with five views: live aircraft, your personal
watchlist, auto-detected military aircraft, a statistics dashboard, and a
searchable historical archive.

## Features

- **Live tab** — every aircraft your receiver can see right now, refreshed every 60 seconds
- **Watchlist tab** — track specific aircraft by tail number, ICAO hex, callsign prefix, or aircraft model
- **Military tab** — auto-detected by callsign prefixes (RCH, NAVY, ARMY...)
- **Stats tab** — today's activity in 8 collapsible sections with 18 drillable cards
- **Search tab** — full-text search across every aircraft your receiver has ever seen
- **Status page** — live health dashboard for every component
- **Configuration page** — edit every setting through the web UI
- **Logs page** — view `tracker.log` with filtering and download
- **Documentation page** — this page

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/aerodrome.git
cd aerodrome
./install.sh
```

See the full README for detailed setup instructions.
""",
    "changelog": """# Changelog

All notable changes to Aerodrome are documented here.

## [2.34.0] — 2026-04-18

### Added
- In-app Logs page and Documentation page, both linked from the gear menu.
  The logs page supports tail sizes (100/500/2000/full), client-side search
  filtering, and download. The documentation page renders every project doc
  file as markdown inside the app.

## [2.33.0] — 2026-04-18

### Added
- Documentation workflow improvements for maintainers: a new
  `scripts/screenshots.py` harness, CONTRIBUTING.md documentation policy,
  and a pre-bump docs checklist in `bump-version.sh`.

## [2.32.0] — 2026-04-18

### Added
- New-record alerts on the Stats tab. When an all-time record is broken,
  the Stats tab flashes a colored pulsing dot and the relevant cards
  get a matching highlight.
""",
    "contributing": """# Contributing to Aerodrome

Thanks for considering a contribution! Aerodrome is a personal hobby
project, but pull requests, bug reports, and feature suggestions are all
welcome.

## Reporting bugs

Open an issue with:

- What you tried to do
- What happened instead
- Your Aerodrome version (`cat VERSION`)
- Your receiver type
- Any relevant log output

## Documentation

Docs are considered part of every change, not an afterthought. See the full
CONTRIBUTING.md for the per-bump-type rules.

## Code style

- Python: PEP 8 reasonably
- HTML/CSS/JS: flat structure, minimal dependencies
- Comments where intent isn't obvious
""",
    "scripts_readme": """# scripts/

Helper scripts that don't run as part of the Aerodrome service. Everything
here is developer / maintainer tooling, safe to ignore at runtime.

## `screenshots.py`

Regenerates every PNG in `docs/` from the current HTML templates using
Playwright and synthetic mock data. No live receiver, no real aircraft
data, no PII.

**How to run** (from the repo root):

```bash
pip install playwright
playwright install chromium
python3 scripts/screenshots.py
```

Output goes to `docs/screenshot-*.png`, overwriting the existing files.
""",
    "update_readme": """# Update staging folder

This folder is used by Aerodrome's local update feature.

## How to stage an update

1. On your workstation, build or download a new Aerodrome release.
2. Copy its contents into this folder.
3. Open the Aerodrome web UI → gear icon → **Check for updates**.
4. If a newer version is detected, click **Apply & restart**.

The apply step will back up, copy, install deps, and restart the service
automatically.
""",
    "license": """MIT License

Copyright (c) 2026 Aerodrome Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
""",
}

# --- Log sample for the logs.html screenshot ------------------------------
# Synthesize a realistic-looking log with a mix of DEBUG (most common),
# INFO (periodic), WARNING (occasional), and ERROR (rare) lines so the
# screenshot shows all the severity colors without leaking anything real.
def _build_log_sample(n_lines=220):
    lines = []
    for i in range(n_lines):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - (n_lines - i) * 45))
        stamp = f"{ts},{i % 1000:03d}"
        if i % 60 == 0:
            lines.append(f"{stamp} [adsb.collector] ERROR: Failed to connect to receiver at http://192.0.2.10:8080 after 3 retries: timeout")
        elif i % 35 == 0:
            lines.append(f"{stamp} [adsb.hexdb] WARNING: hexdb.io rate limit hit, backing off 30s")
        elif i % 8 == 0:
            lines.append(f"{stamp} [adsb.main] INFO: Collector iteration {i+12400}, wrote {random.randint(8, 24)} aircraft to db")
        elif i % 15 == 0:
            lines.append(f"{stamp} [adsb.server] INFO: 127.0.0.1 - GET /api/live HTTP/1.1 - 200 OK ({random.randint(4, 18)} ms)")
        else:
            lines.append(f"{stamp} [adsb.collector] DEBUG: Polling http://192.0.2.10:8080/data/aircraft.json -> 200 OK, {random.randint(6, 18)} aircraft")
    return "\n".join(lines) + "\n"

LOG_SAMPLE = _build_log_sample()
LOG_INFO = {
    "path": "/opt/aerodrome/logs/tracker.log",
    "size_bytes": 2_413_056,
    "line_count": 28_340,
    "mtime": NOW,
    "error": None,
}

# --- Performance diagnostic payload ----------------------------------------
# Designed to look like a moderately busy Pi 4 / USB-SSD install — not
# catastrophic (would look alarming and unrepresentative), not tiny (wouldn't
# showcase the page). 30 days of retention at ~300-400 unique aircraft/day.
# Query timings show a healthy system post-v2.41.9's window-CTE fix (all under
# 150 ms). The diagnostic page looks interesting, not scary.
PERF_DIAG = {
    "ok": True,
    "generated_at": NOW,
    "aerodrome_version": VERSION,
    "sqlite_version": "3.46.0",
    "python_version": "3.12.3",
    "platform": {"system": "Linux", "machine": "aarch64", "processor": "",
                 "release": "6.6.28+rpt-rpi-v8"},
    "storage": {
        "db_file": "/opt/aerodrome/aircraft_history.db",
        "size_bytes": 285 * 1024 * 1024,
        "size_human": "285.0 MB",
        "wal_bytes": 4 * 1024 * 1024,
    },
    "pragmas": {
        "journal_mode": "wal", "page_size": 4096, "page_count": 73_000,
        "cache_size": -2000, "wal_autocheckpoint": 1000,
        "synchronous": 1, "temp_store": 0, "auto_vacuum": 0, "mmap_size": 0,
    },
    "tables": [
        {"name": "all_sightings",       "rows": 1_841_302, "count_ms": 72.4,
         "oldest_ts": NOW - 30 * 86400, "newest_ts": NOW, "span_days": 30.0},
        {"name": "military_sightings",  "rows": 8_214,     "count_ms": 2.1,
         "oldest_ts": NOW - 30 * 86400, "newest_ts": NOW - 1800, "span_days": 30.0},
        {"name": "watchlist_sightings", "rows": 412,       "count_ms": 0.8,
         "oldest_ts": NOW - 25 * 86400, "newest_ts": NOW - 8400, "span_days": 25.0},
        {"name": "seen_aircraft",       "rows": 11_205,    "count_ms": 3.2,
         "oldest_ts": NOW - 210 * 86400, "newest_ts": NOW - 180, "span_days": 210.0},
        {"name": "stats_records",       "rows": 8,         "count_ms": 0.3},
    ],
    "indexes": [
        {"table": "all_sightings",       "name": "idx_all_seen"},
        {"table": "all_sightings",       "name": "idx_all_icao"},
        {"table": "all_sightings",       "name": "idx_all_seen_icao"},
        {"table": "military_sightings",  "name": "idx_mil_seen"},
        {"table": "watchlist_sightings", "name": "idx_watch_seen"},
        {"table": "seen_aircraft",       "name": "idx_seen_first"},
    ],
    "queries": [
        {"label": "live_aircraft (all_sightings seen in last 5 min)",
         "ok": True, "ms": 4.2, "plan": [
             {"detail": "SEARCH all_sightings USING COVERING INDEX idx_all_seen_icao (seen_at>?)"},
         ]},
        {"label": "all_tab_count (DISTINCT icao over last 30d)",
         "ok": True, "ms": 38.7, "plan": [
             {"detail": "USE TEMP B-TREE FOR count(DISTINCT)"},
             {"detail": "SEARCH all_sightings USING COVERING INDEX idx_all_seen_icao (seen_at>? AND seen_at<?)"},
         ]},
        {"label": "military_count (over last 30d)",
         "ok": True, "ms": 1.4, "plan": [
             {"detail": "USE TEMP B-TREE FOR count(DISTINCT)"},
             {"detail": "SEARCH military_sightings USING INDEX idx_mil_seen (seen_at>? AND seen_at<?)"},
         ]},
        {"label": "watchlist_count (over last 30d)",
         "ok": True, "ms": 0.9, "plan": [
             {"detail": "USE TEMP B-TREE FOR count(DISTINCT)"},
             {"detail": "SEARCH watchlist_sightings USING INDEX idx_watch_seen (seen_at>? AND seen_at<?)"},
         ]},
        {"label": "all_tab_page (window CTE, full 30d window)",
         "ok": True, "ms": 142.8, "plan": [
             {"detail": "CO-ROUTINE filtered"},
             {"detail": "  SEARCH all_sightings USING COVERING INDEX idx_all_seen_icao"},
             {"detail": "CO-ROUTINE ranked"},
             {"detail": "  USE TEMP B-TREE FOR ORDER BY (PARTITION BY icao ORDER BY seen_at DESC)"},
             {"detail": "SCAN ranked"},
             {"detail": "USE TEMP B-TREE FOR ORDER BY"},
         ]},
        {"label": "seen_aircraft_total (all-time unique ICAOs)",
         "ok": True, "ms": 0.7, "plan": [
             {"detail": "SCAN seen_aircraft USING COVERING INDEX idx_seen_first"},
         ]},
    ],
    "io_baseline": {
        "bytes_read": 1024 * 1024,
        "elapsed_ms": 12.4,
        "throughput_mb_s": 82.3,
    },
    "hints": [],  # healthy system, no warnings
}

# ---------------------------------------------------------------------------
# Aircraft detail + positions (v3.4.33: for the aircraft-details screenshot).
# Synthetic track exercises every visual feature of the v3.4.31 polyline
# rendering: climb → mid cruise → high cruise → COVERAGE GAP → high cruise
# → descent → landing. Altitude spans every bin so the line shows the full
# six-color palette in transitions; the gap sits in the middle so a
# polyline break is clearly visible; hysteresis is naturally exercised
# because the steady-cruise segments stay at one altitude rather than
# wobbling across a boundary.
#
# Bay Area coordinates so the receiver marker + map view make sense visually.
# Receiver lives near San Mateo (37.5, -122.1); the synthetic flight traces
# an arc from northwest to southeast through nearby airspace.
# ---------------------------------------------------------------------------
_AC_NOW = NOW
_AC_TRACK_BASE = _AC_NOW - 7200  # 2h ago, in seconds since epoch

# 28 sample points at ~30s cadence (=14 minutes of "real time" worth of
# track), with a 4-minute gap between idx=14 and idx=15 to demonstrate
# the gap-break. Format: [seen_at_seconds, lat, lon, altitude_ft].
_AC_POSITIONS = []
for i, (lat, lon, alt) in enumerate([
    (38.10, -122.85,    600),   # 0 — taxi/climb
    (38.06, -122.78,   2400),   # 1
    (38.02, -122.72,   5200),   # 2 — bin transition green→yellow-green
    (37.99, -122.66,   8400),   # 3
    (37.95, -122.59,  12500),   # 4 — yellow
    (37.92, -122.52,  16800),   # 5
    (37.89, -122.45,  20900),   # 6 — orange
    (37.86, -122.38,  24500),   # 7
    (37.84, -122.31,  27800),   # 8
    (37.81, -122.24,  30200),   # 9 — red
    (37.79, -122.16,  32100),   # 10
    (37.77, -122.08,  33800),   # 11
    (37.75, -122.00,  35100),   # 12 — dark red
    (37.73, -121.92,  35400),   # 13
    (37.71, -121.84,  35200),   # 14 — last point before gap
    # === COVERAGE GAP ~4 minutes ===
    (37.65, -121.62,  35100),   # 15 — resumes (4-minute time jump below)
    (37.62, -121.55,  34800),   # 16
    (37.59, -121.48,  32400),   # 17 — descent begins
    (37.56, -121.41,  28100),   # 18
    (37.53, -121.34,  23200),   # 19 — orange
    (37.50, -121.27,  17800),   # 20
    (37.48, -121.20,  12900),   # 21
    (37.45, -121.14,   8700),   # 22 — yellow
    (37.43, -121.08,   5300),   # 23
    (37.41, -121.02,   3100),   # 24 — green
    (37.39, -120.96,   1900),   # 25
    (37.37, -120.91,    900),   # 26
    (37.35, -120.88,    200),   # 27 — landing
]):
    # 30s cadence, except after idx=14 (the gap) where the next sample
    # appears 4 minutes (240s) later — well past the 120s gap-break
    # threshold so the rendering shows a clean line-break.
    if i == 0:
        ts = _AC_TRACK_BASE
    elif i == 15:
        ts = _AC_POSITIONS[14][0] + 240   # 4-minute gap
    else:
        ts = _AC_POSITIONS[i - 1][0] + 30
    _AC_POSITIONS.append([ts, lat, lon, alt])

AIRCRAFT_POSITIONS = {
    "ok": True,
    "icao": "ABCDEF",
    "window": "24h",
    "count": len(_AC_POSITIONS),
    "positions": _AC_POSITIONS,
    "receiver": {"lat": 37.5, "lon": -122.1},
    "truncated": False,
}

AIRCRAFT_DETAIL = {
    "icao": "ABCDEF",
    "registration": "N737TS",
    "aircraft_type": "B738",
    "aircraft_type_desc": "Boeing 737-800",
    "operator": "Example Airlines",
    "country": "United States",
    "last_callsign": "EXA1234",
    "first_callsign": "EXA1234",
    "callsign": "EXA1234",
    "sighting_count": 142,
    "mode": "full",
    "first_seen": _AC_NOW - 86400 * 38,
    "last_seen": _AC_NOW - 1800,
    "is_military": False,
    "is_watchlist": False,
    "recent_sightings": [],
    # The detail page reads stat-card values from `ranges`, hour_of_day,
    # day_of_week, and chips — discovered via grep on the render code.
    # All values picked to look plausible for a regional 737 making
    # roughly 4-5 trips per day past the receiver.
    "ranges": {
        "days_active": 38,
        "max_altitude_ft": 35400,
        "min_altitude_ft": 200,
        "max_speed_kt": 482,
        "sightings_per_day_min":    1,
        "sightings_per_day_max":    8,
        "sightings_per_day_median": 4,
    },
    "hour_of_day":  [2, 1, 0, 0, 0, 1, 4, 7, 9, 11, 12, 10, 9, 8, 7, 9, 11, 12, 10, 8, 6, 4, 3, 2],
    "day_of_week":  [18, 22, 24, 21, 20, 19, 18],
    "chips":        [
        {"label": "Cruise altitude", "value": "FL352"},
        {"label": "Typical route",   "value": "NW → SE corridor"},
    ],
    "primary_callsigns":   [{"callsign": "EXA1234", "count": 142}],
}

# ---------------------------------------------------------------------------
# Playwright harness
# ---------------------------------------------------------------------------

def _build_fetch_stub() -> str:
    """Return a <script> block that overrides window.fetch so every
    /api/* call the templates make returns our synthetic data."""
    payloads = {
        'ui_config':     UI_CFG,
        'stats':         STATS,
        'live':          {"aircraft": LIVE_AIRCRAFT, "last_updated": NOW},
        'military':      {"aircraft": MILITARY_AIRCRAFT, "last_updated": NOW, "retention_days": 30},
        'watchlist':     {"aircraft": WATCHLIST_GROUPS, "last_updated": NOW, "retention_days": 30},
        'wl_entries':    {"entries": WL_ENTRIES},
        'status':        STATUS,
        'config':        FULL_CONFIG,
        'perf':          PERF_DIAG,
        'search':        SEARCH_RESPONSE,
    }
    return f"""<script>
window.fetch = async (url) => {{
    const j = (o) => new Response(JSON.stringify(o), {{status: 200}});
    const t = (s) => new Response(s, {{status: 200, headers: {{'Content-Type':'text/plain'}}}});
    // IMPORTANT: more-specific routes come first (e.g. /api/watchlist/entries
    // before /api/watchlist) so the correct stub matches.
    if (url.includes('/api/ui-config'))             return j({json.dumps(payloads['ui_config'])});
    if (url.includes('/api/stats/drill')) {{
        const u = new URL(url, 'http://x');
        return j({{card: u.searchParams.get('card'), rows: [], count: 0}});
    }}
    if (url.includes('/api/stats'))                 return j({json.dumps(payloads['stats'])});
    if (url.includes('/api/live'))                  return j({json.dumps(payloads['live'])});
    if (url.includes('/api/military'))              return j({json.dumps(payloads['military'])});
    if (url.includes('/api/watchlist/entries'))     return j({json.dumps(payloads['wl_entries'])});
    if (url.includes('/api/watchlist'))             return j({json.dumps(payloads['watchlist'])});
    if (url.includes('/api/first-seen')) {{
        // Return only the ICAOs actually requested. The frontend handles
        // the "no entry = no chip" case gracefully, so we don't have to
        // worry about absent keys.
        const u = new URL(url, 'http://x');
        const requested = (u.searchParams.get('icaos') || '').split(',')
            .map(s => s.trim().toUpperCase()).filter(Boolean);
        const all = {json.dumps(FIRST_SEEN)};
        const out = {{}};
        for (const hex of requested) if (all[hex] != null) out[hex] = all[hex];
        return j({{first_seen: out}});
    }}
    if (url.includes('/api/perf/diagnostics'))     return j({json.dumps(payloads['perf'])});
    if (url.includes('/api/status'))                return j({json.dumps(payloads['status'])});
    // v2.77.0: search routes. Order matters — suggestions and per-icao
    // drill are more specific than the bare /api/search prefix, so
    // they come first. The bare prefix services the runSearch() flow.
    if (url.includes('/api/search/suggestions'))    return j({{ok: true, suggestions: []}});
    if (url.includes('/api/search/aircraft/'))      return j({{ok: true, sightings: [], total: 0}});
    if (url.includes('/api/search'))                return j({json.dumps(payloads['search'])});
    if (url.includes('/api/capacity'))              return j({{
        ok: true,
        capacity: {{
            ok: true,
            db_size_mb: 45.0,
            rows_per_day: 18500,
            mb_per_day: 3.0,
            bytes_per_row: 170.4,
            days_of_data: 10.2,
            data_source: "measured",
            retention_days: 30,
            projected_settled_mb: 90.0,
            disk_free_mb: 12400.0,
            disk_total_mb: 40960.0,
            headroom_ratio: 138.3,
            what_if: [
                {{days: 7,   projected_mb: 21.0,  headroom_ratio: 593.0}},
                {{days: 14,  projected_mb: 42.0,  headroom_ratio: 296.4}},
                {{days: 30,  projected_mb: 90.0,  headroom_ratio: 138.3}},
                {{days: 60,  projected_mb: 180.0, headroom_ratio: 69.1}},
                {{days: 90,  projected_mb: 270.0, headroom_ratio: 46.1}},
                {{days: 180, projected_mb: 540.0, headroom_ratio: 23.0}},
            ],
            error: null,
        }},
    }});
    if (url.includes('/api/timezones'))             return j({{timezones: []}});
    if (url.includes('/api/config/db-tuning'))      return j({{
        ok: true, auto_resolves_to: 'balanced', system_memory_gb: 4.0,
        profiles: {{
            'default':      {{cache_mib: 2,   mmap_mib: 0,   temp_store: 0}},
            'conservative': {{cache_mib: 8,   mmap_mib: 32,  temp_store: 2}},
            'balanced':     {{cache_mib: 32,  mmap_mib: 128, temp_store: 2}},
            'aggressive':   {{cache_mib: 64,  mmap_mib: 256, temp_store: 2}},
            'high_memory':  {{cache_mib: 128, mmap_mib: 512, temp_store: 2}},
        }}
    }});
    if (url.includes('/api/config/backups'))        return j({{backups: []}});
    if (url.includes('/api/backup/pre-restore'))    return j({{snapshots: []}});
    if (url.includes('/api/backup/preview'))        return j({{ok: true, files: []}});
    if (url.includes('/api/config/validate'))       return j({{errors: []}});
    if (url.includes('/api/config'))                return j({json.dumps(payloads['config'])});
    // v3.4.33: aircraft-detail page. Two endpoints: the detail object
    // (full aircraft metadata for the hero/cards/sections) and the
    // positions array used by the track-rendering map.
    if (url.match(/\/api\/aircraft\/[0-9A-Fa-f]+\/positions/)) {{
        return j({json.dumps(AIRCRAFT_POSITIONS)});
    }}
    if (url.match(/\/api\/aircraft\/[0-9A-Fa-f]+$/)) {{
        return j({json.dumps(AIRCRAFT_DETAIL)});
    }}
    // --- Docs viewer ---
    if (url.includes('/api/docs/')) {{
        const slug = url.split('/api/docs/')[1].split(/[?#]/)[0];
        const docs = {json.dumps(DOC_MARKDOWN)};
        return t(docs[slug] || '# ' + slug + '\\n\\nNot available in screenshots harness.');
    }}
    // --- Logs viewer ---
    if (url.includes('/api/logs/info'))             return j({json.dumps(LOG_INFO)});
    if (url.includes('/api/logs/tail'))             return t({json.dumps(LOG_SAMPLE)});
    return j({{}});
}};
</script>"""


async def _render(browser, template_file: str, outfile: Path, *,
                  viewport: dict = None, ready_fn=None, clip: dict = None):
    """Load a template with the stub injected, optionally run a prep function,
    then screenshot. `ready_fn` gets the page object and can click tabs etc."""
    page = await browser.new_page(
        viewport=viewport or {'width': 1400, 'height': 900},
        # v2.50.24: force dark color scheme. The FOUC script in each
        # template reads localStorage('aerodrome-theme') (empty in the
        # harness) and falls back to 'auto', which checks
        # prefers-color-scheme. Headless chromium defaults that to
        # 'light' — but the existing screenshot convention (and the
        # default user experience on first install) is dark. Setting
        # this here keeps the rendered output consistent with what
        # users actually see.
        color_scheme='dark',
    )
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    html = (TEMPLATE_DIR / template_file).read_text()

    # v2.50.24: inline /static/* assets so they resolve under file:// origin.
    # Templates use absolute paths like /static/theme.css for production
    # serving; the harness loads templates as file:// URLs where those
    # paths can't resolve. Substituting <link>/<script> tags with inline
    # <style>/<script> blocks containing the actual file contents keeps
    # the screenshots faithfully styled without hacking on the template.
    #
    # Note: when inlining JS, any literal `</script>` string in the body
    # (typically inside a JSDoc comment or docstring) would prematurely
    # close the surrounding <script> tag and break the inlined script.
    # The standard fix is to escape it as `<\/script>` — still a valid
    # string in JS, but the HTML tokenizer doesn't recognize it as a
    # closing tag. Applied to every inlined JS body below.
    static_dir = REPO_ROOT / 'static'
    for asset in ('theme.css', 'theme.js', 'health-indicator.js'):
        path = static_dir / asset
        if not path.exists():
            continue
        body = path.read_text()
        if asset.endswith('.css'):
            tag_orig = f'<link rel="stylesheet" href="/static/{asset}">'
            tag_new  = f'<style>\n{body}\n</style>'
        else:
            body = body.replace('</script>', r'<\/script>')
            tag_orig = f'<script src="/static/{asset}"></script>'
            tag_new  = f'<script>\n{body}\n</script>'
        html = html.replace(tag_orig, tag_new)

    # v3.4.33: also inline Leaflet for templates that use the position-history
    # map (currently just aircraft.html). The map block is the focal point of
    # the aircraft-details screenshot and Leaflet has to actually load for
    # the polyline-track rendering to be visible. Leaflet CSS/JS live in
    # /static/leaflet/ and reference the same images/ subdir via relative
    # CSS url() — inlining the CSS keeps url() pointing at /static/leaflet/
    # which won't resolve under file://, but the polyline render doesn't
    # depend on the default marker images, so the missing-image warnings
    # are cosmetic for our purposes.
    leaflet_dir = static_dir / 'leaflet'
    for asset in ('leaflet.css', 'leaflet.js'):
        path = leaflet_dir / asset
        if not path.exists():
            continue
        body = path.read_text()
        if asset.endswith('.css'):
            tag_orig = f'<link rel="stylesheet" href="/static/leaflet/{asset}">'
            tag_new  = f'<style>\n{body}\n</style>'
            html = html.replace(tag_orig, tag_new)
        else:
            body = body.replace('</script>', r'<\/script>')
            for tag_orig in (
                f'<script src="/static/leaflet/{asset}" defer></script>',
                f'<script src="/static/leaflet/{asset}"></script>',
            ):
                if tag_orig in html:
                    # Leaflet must load synchronously in the harness so it's
                    # ready when the page's IIFE fires. The `defer` attribute
                    # is meaningless for inline scripts but the source tag
                    # uses defer in production — strip it on inline.
                    tag_new = f'<script>\n{body}\n</script>'
                    html = html.replace(tag_orig, tag_new)
                    break

    # v2.97.12: mirror server.py's timefmt.js injection (server.py:1142-1146).
    # The server replaces </head> at request time with a window._aerodromeTimeFormat
    # config plus a <script src="/static/timefmt.js?v=..."></script> tag that the
    # templates depend on but never reference statically. The harness loads
    # templates directly from disk, bypassing the server, so without this mirror
    # the screenshots render with "formatDateTime is not defined" errors wherever
    # formatDateTime() is called from inline page JS. Default to 'auto' (matches
    # the fresh-install default and the typical user experience).
    timefmt_path = static_dir / 'timefmt.js'
    if timefmt_path.exists():
        timefmt_body = timefmt_path.read_text().replace('</script>', r'<\/script>')
        timefmt_block = (
            '<script>window._aerodromeTimeFormat="auto";</script>\n'
            f'<script>\n{timefmt_body}\n</script>\n'
        )
        html = html.replace('</head>', timefmt_block + '</head>', 1)

    html = html.replace('</head>', _build_fetch_stub() + '</head>')
    tmp = TMP_DIR / f'__aerodrome_rendertmp_{outfile.name}.html'
    tmp.write_text(html)

    await page.goto(f'file://{tmp}')
    await page.wait_for_timeout(1500)
    if ready_fn:
        await ready_fn(page)
    if clip:
        await page.screenshot(path=str(outfile), clip=clip)
    else:
        await page.screenshot(path=str(outfile), full_page=True)
    await page.close()

    if errors:
        # Fatal JS errors usually mean mock data has drifted from the
        # template's expected shape — surface them loudly.
        print(f"  ! errors in {outfile.name}:")
        for e in errors[:3]:
            print(f"      {e}")
    return outfile


# ---------------------------------------------------------------------------
# One function per screenshot. To add a new one: copy this pattern, add the
# function name to the list at the bottom of main(), and reference the new
# image in README.md.
# ---------------------------------------------------------------------------

async def screenshot_live(browser):
    return await _render(browser, 'index.html', OUT_DIR / 'screenshot-live.png')

async def screenshot_watchlist(browser):
    async def ready(p):
        await p.evaluate("if(typeof go==='function') go('watchlist')")
        await p.wait_for_timeout(800)
    return await _render(browser, 'index.html', OUT_DIR / 'screenshot-watchlist.png',
                         ready_fn=ready)

async def screenshot_military(browser):
    async def ready(p):
        await p.evaluate("if(typeof go==='function') go('military')")
        await p.wait_for_timeout(800)
    return await _render(browser, 'index.html', OUT_DIR / 'screenshot-military.png',
                         ready_fn=ready)

# v2.77.0: Search tab. Switches to the Search tab, types "today" into
# the input, calls runSearch() to fire the synthetic /api/search fetch,
# and waits for results to render. The "today" query exercises the
# v2.65.0 relative-date token (renders as a parsed-filter chip in the
# chip strip) and demonstrates the typical browse-everything-recent
# flow that's Search's headline use case.
#
# Filed at v2.67.0 (Phase 1D) when screenshot_all was removed; closed
# at v2.77.0 alongside the Phase 3 polish work.
async def screenshot_search(browser):
    async def ready(p):
        await p.evaluate("if(typeof go==='function') go('search')")
        await p.wait_for_timeout(300)
        # Type into the search input and trigger runSearch. setting .value
        # alone doesn't fire the input event some flows depend on, so we
        # set it AND call runSearch() directly. The search input id is
        # 'searchInput' — see the Search tab markup in index.html.
        await p.evaluate("""
            const input = document.getElementById('searchInput');
            if (input) input.value = 'today';
            if (typeof runSearch === 'function') runSearch();
        """)
        # Wait for the results render. _fetchSearchPage() is async; the
        # synthesized fetch resolves immediately but the render path
        # still goes through requestAnimationFrame before the cards
        # land in the DOM. 800ms is generous.
        await p.wait_for_timeout(800)
    return await _render(browser, 'index.html', OUT_DIR / 'screenshot-search.png',
                         ready_fn=ready)

async def screenshot_stats(browser):
    """Stats tab with all 8 sections expanded — needs a tall viewport."""
    async def ready(p):
        await p.click('#tab-stats')
        await p.wait_for_timeout(1200)
    return await _render(browser, 'index.html', OUT_DIR / 'screenshot-stats.png',
                         viewport={'width': 1400, 'height': 1800}, ready_fn=ready)

async def screenshot_status(browser):
    return await _render(browser, 'status.html', OUT_DIR / 'screenshot-status.png')

async def screenshot_config(browser):
    return await _render(browser, 'config.html', OUT_DIR / 'screenshot-config.png',
                         viewport={'width': 1400, 'height': 1400})

async def screenshot_config_database(browser):
    """v2.50.24: Configuration page, Database tab. Shows the SQLite tuning
    profile dropdown (v2.50.13) and the auto-resolve status note (v2.50.14)
    that surfaces what 'Auto' picks for the running hardware. The status
    note is rendered after a /api/config/db-tuning fetch resolves, so we
    wait for the placeholder text to be replaced before snapping."""
    async def ready(p):
        await p.evaluate("switchTab('data')")
        # Auto-resolve fetch is async; the placeholder reads "Loading
        # auto-resolve info…" until it returns. Wait for that to flip
        # so the screenshot captures the populated state. The mock
        # harness intercepts /api/config/db-tuning below to provide a
        # stable response.
        await p.wait_for_function(
            "() => { const e=document.getElementById('dbTuningStatus'); "
            "return e && !e.textContent.includes('Loading'); }",
            timeout=3000,
        )
    return await _render(browser, 'config.html',
                         OUT_DIR / 'screenshot-config-database.png',
                         viewport={'width': 1400, 'height': 900},
                         ready_fn=ready)

async def screenshot_config_alerts(browser):
    """v2.50.24: Configuration page, Watchlist alerts tab. Shows the
    trigger dropdown reduced to its three distinct options after the
    v2.50.23 cleanup: continuous, continuous_dismissable, live."""
    async def ready(p):
        await p.evaluate("switchTab('alerts')")
        await p.wait_for_timeout(400)
    return await _render(browser, 'config.html',
                         OUT_DIR / 'screenshot-config-alerts.png',
                         viewport={'width': 1400, 'height': 900},
                         ready_fn=ready)

async def screenshot_config_backup(browser):
    """v2.50.24: Configuration page, Backup & Restore tab. Shows the
    pre-restore safety-snapshot section added in v2.50.6 alongside the
    config auto-backup retention controls from v2.50.9."""
    async def ready(p):
        await p.evaluate("switchTab('backup')")
        # Backup & Restore loads a few fetches (config backups, pre-restore
        # snapshots, full backup status) — give them a beat to populate.
        await p.wait_for_timeout(800)
    return await _render(browser, 'config.html',
                         OUT_DIR / 'screenshot-config-backup.png',
                         viewport={'width': 1400, 'height': 1600},
                         ready_fn=ready)

async def screenshot_config_stats(browser):
    """Configuration page with the Stats tab selected — shows the
    auto-refresh, timezone, and Continuous-track-gap controls."""
    async def ready(p):
        # Click the Stats tab. The page's switchTab() is globally defined.
        await p.evaluate("switchTab('stats')")
        await p.wait_for_timeout(400)
    return await _render(browser, 'config.html', OUT_DIR / 'screenshot-config-stats.png',
                         viewport={'width': 1400, 'height': 1800},
                         ready_fn=ready)

async def screenshot_config_notifications(browser):
    """v2.67.2: Configuration page, Notifications tab. Added to the harness
    so the public README screenshot can be regenerated with synthetic
    data — the previous hand-captured version showed the old "ADS-B
    Aerodrome" branding and a real LAN IP that should never appear in
    distribution images."""
    async def ready(p):
        await p.evaluate("switchTab('notifications')")
        # Notifications tab fetches the local-ntfy server status — give it
        # a moment so the section finishes laying out.
        await p.wait_for_timeout(800)
    return await _render(browser, 'config.html',
                         OUT_DIR / 'screenshot-config-notifications.png',
                         viewport={'width': 1400, 'height': 1800},
                         ready_fn=ready)

async def screenshot_export(browser):
    """Live tab with the Export dropdown open."""
    async def ready(p):
        await p.evaluate("""
            () => {
                const btns = Array.from(document.querySelectorAll('.export-dropdown, .export-menu-toggle'));
                if (btns.length) { btns[0].click(); return; }
                const exp = Array.from(document.querySelectorAll('button, .btn'))
                    .find(b => (b.textContent || '').toLowerCase().includes('export'));
                if (exp) exp.click();
            }
        """)
        await p.wait_for_timeout(400)
    return await _render(browser, 'index.html', OUT_DIR / 'screenshot-export.png',
                         ready_fn=ready)

async def screenshot_docs(browser):
    """Documentation viewer — lands on the README tab."""
    async def ready(p):
        # Give the markdown renderer a beat after the fetch resolves
        await p.wait_for_timeout(600)
    return await _render(browser, 'docs.html', OUT_DIR / 'screenshot-docs.png',
                         viewport={'width': 1400, 'height': 1100}, ready_fn=ready)

async def screenshot_about(browser):
    """v3.4.18: dedicated /about page. Static content (project description,
    license, OSS credits) — no fetched data to wait for beyond the standard
    header version pill, which the shared _render helper already handles."""
    return await _render(browser, 'about.html', OUT_DIR / 'screenshot-about.png',
                         viewport={'width': 1400, 'height': 700})

async def screenshot_logs(browser):
    """Log viewer with the sample tail loaded."""
    async def ready(p):
        await p.wait_for_timeout(600)
    return await _render(browser, 'logs.html', OUT_DIR / 'screenshot-logs.png',
                         viewport={'width': 1400, 'height': 1000}, ready_fn=ready)


async def screenshot_performance(browser):
    """Performance diagnostics page — renders the standard v2.41.11+ header,
    the auto-run diagnostic results, and all four sections (storage, tables,
    indexes, queries, SQLite + system). Needs a tall viewport because the
    page is long on a real install with 6 indexes + 6 query rows."""
    async def ready(p):
        # The diagnostic auto-runs on page load. Give it a beat to render.
        await p.wait_for_timeout(900)
    return await _render(browser, 'performance.html',
                         OUT_DIR / 'screenshot-performance.png',
                         viewport={'width': 1400, 'height': 1700},
                         ready_fn=ready)


async def screenshot_setup_guide(browser):
    """Notifications setup wizard modal — the four-step guide shown to new
    users. Opened by clicking the 'Setup guide' button on the Notifications
    config tab (which calls showOnboardingModal())."""
    async def ready(p):
        # Get to the Notifications tab, then fire the modal directly.
        await p.evaluate("switchTab('notifications')")
        await p.wait_for_timeout(500)
        # Call the modal opener directly — more reliable than finding the
        # button since the button may be hidden until further UI state is
        # ready. The function is globally defined in config.html.
        opened = await p.evaluate("""
            () => {
                if (typeof showOnboardingModal === 'function') {
                    showOnboardingModal();
                    return true;
                }
                return false;
            }
        """)
        if not opened:
            print("    ! showOnboardingModal() not available in scope")
        await p.wait_for_timeout(700)
    return await _render(browser, 'config.html',
                         OUT_DIR / 'screenshot-setup-guide.png',
                         viewport={'width': 1200, 'height': 1000},
                         ready_fn=ready)


async def screenshot_diagnostics_hub(browser):
    """v2.41.23: Diagnostics hub page at /diagnostics. Shows the card grid
    of available troubleshooting diagnostics (currently Performance and
    Watchlist alerts). Short page — the viewport doesn't need to be tall."""
    async def ready(p):
        # Cards render via DOMContentLoaded; give it a beat to paint.
        await p.wait_for_timeout(400)
    return await _render(browser, 'diagnostics.html',
                         OUT_DIR / 'screenshot-diagnostics.png',
                         viewport={'width': 1200, 'height': 700},
                         ready_fn=ready)


async def screenshot_diagnostics_watchlist(browser):
    """v2.41.23: Watchlist-alert diagnostic detail page. The screenshot
    captures the post-run state — interpretation cards + report text block —
    since that's the meaningful visual. Fire the diagnostic programmatically
    and wait for the report to render."""
    async def ready(p):
        # Auto-run the diagnostic so the screenshot shows the populated UI.
        await p.evaluate("runDiagnostic && runDiagnostic()")
        # Diagnostic does two fetches (/api/config/ui, /api/watchlist) then
        # populates the DOM. 1200ms is plenty on a loopback.
        await p.wait_for_timeout(1200)
    return await _render(browser, 'diagnostics-watchlist.html',
                         OUT_DIR / 'screenshot-diagnostics-watchlist.png',
                         viewport={'width': 1200, 'height': 1200},
                         ready_fn=ready)


async def screenshot_aircraft_details(browser):
    """v3.4.33: aircraft details page showing the v3.4.31 multi-color
    polyline track rendering on the position-history map. The synthetic
    track exercises every visual feature — full color palette across
    altitude bins, gap-break at a >120s coverage drop, smooth lines
    where steady cruise sits between bin thresholds (hysteresis).

    The page reads ICAO from window.location.pathname, which under the
    harness loads as file:///tmp/<tmpname>.html — pathname's last
    segment ends up being the tmp filename and fails the 6-hex-char
    validation. We monkey-patch getIcaoFromUrl after page load and
    re-trigger loadDetail() so the page renders with the intended ICAO."""
    async def ready(p):
        # Override the URL-parsing helper and re-call loadDetail so the
        # detail panel populates from the mocked /api/aircraft/ABCDEF
        # response instead of rendering the "Invalid aircraft URL" error
        # that fires from the original file:// pathname.
        await p.evaluate("""
            window.getIcaoFromUrl = () => 'ABCDEF';
            if (typeof loadDetail === 'function') {
                loadDetail();
            }
        """)
        # The detail load is async and the position map is initialized
        # lazily after the receiver-coords check. 2500ms gives Leaflet
        # time to lay out tiles, the polyline renderer time to walk the
        # binned positions and emit segments, and the bounds calculation
        # time to fit the map view. 2500ms is comfortable; a 1500ms
        # smoke-test showed occasional half-rendered tiles.
        await p.wait_for_timeout(2500)
    return await _render(browser, 'aircraft.html',
                         OUT_DIR / 'screenshot-aircraft-details.png',
                         viewport={'width': 1400, 'height': 1100},
                         ready_fn=ready)


async def main():
    print(f"Rendering screenshots to {OUT_DIR}/ ...")
    renderers = [
        screenshot_live,
        screenshot_watchlist,
        screenshot_military,
        screenshot_search,  # v2.77.0
        screenshot_stats,
        screenshot_status,
        screenshot_config,
        screenshot_config_database,
        screenshot_config_alerts,
        screenshot_config_backup,
        screenshot_config_stats,
        screenshot_config_notifications,
        screenshot_export,
        screenshot_docs,
        screenshot_about,
        screenshot_logs,
        screenshot_performance,
        screenshot_diagnostics_hub,
        screenshot_diagnostics_watchlist,
        screenshot_setup_guide,
        screenshot_aircraft_details,  # v3.4.33
    ]
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for fn in renderers:
            out = await fn(browser)
            size = out.stat().st_size
            print(f"  ✓ {out.name} ({size:,} bytes)")
        await browser.close()
    print(f"Done. {len(renderers)} screenshots written.")


if __name__ == '__main__':
    asyncio.run(main())
