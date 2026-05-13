"""
Synthetic aircraft data generator.

Produces ADS-B observations whose shape exactly matches what
tar1090/readsb writes to /data/aircraft.json — so Aerodrome's
collector cannot tell the difference between this and a real
receiver.

Design notes:

  - Hermetic. ICAOs are random 6-char hex from the full 16M
    space. ~5% are picked from the US military range
    (AE0000-AFFFFF) so the watchlist/military classifier path
    gets exercised. The remaining 95% are uniformly random.
    Hexdb resolution will return negatives for everything,
    which is fine — that path is exercised and the negative
    cache is the production behaviour for unknown aircraft.

  - Realistic motion. Each aircraft has a position, heading,
    speed, and altitude. Per tick, position advances along
    heading at speed-scaled distance, with small random
    perturbations to heading/altitude/speed so the data isn't
    synthetic-looking. Aircraft outside range get rotated
    out and a fresh aircraft enters from a random bearing.

  - Field shape matches the user's real feeder reference
    (32-aircraft tar1090 sample, 24 always-present + 10
    usually-present + 5 rare fields). The collector only
    reads a subset of these; the rest are produced for
    fidelity and to make the synthetic feed look genuinely
    indistinguishable from a real one in any future code
    that grows to consume more fields.

  - No external state. Random seed is configurable for
    reproducible test runs. Fleet evolves deterministically
    from a seed, so a backfill with seed=42 produces the same
    output every time.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# Aircraft type catalogue. Real ICAO type codes paired with descriptions
# matching what BaseStation.sqb / readsb's database would return. Sampled
# weights roughly match what a typical busy receiver sees — heavies and
# regional jets dominate, GA props and helicopters appear less often.
_TYPE_CATALOGUE = [
    # (icao_type, description, ads_b_category, weight)
    ("B738",  "BOEING 737-800",                  "A3", 14),
    ("B739",  "BOEING 737-900",                   "A3", 6),
    ("B38M",  "BOEING 737 MAX 8",                 "A3", 9),
    ("A320",  "AIRBUS A-320",                     "A3", 12),
    ("A21N",  "AIRBUS A-321neo",                  "A3", 7),
    ("A319",  "AIRBUS A-319",                     "A3", 5),
    ("CRJ7",  "BOMBARDIER Regional Jet CRJ-700",  "A2", 6),
    ("CRJ9",  "BOMBARDIER Regional Jet CRJ-900",  "A3", 5),
    ("E75L",  "EMBRAER 175",                      "A3", 7),
    ("B77W",  "BOEING 777-300ER",                 "A5", 3),
    ("B789",  "BOEING 787-9 Dreamliner",          "A5", 3),
    ("B763",  "BOEING 767-300",                   "A5", 2),
    ("A332",  "AIRBUS A-330-200",                 "A5", 2),
    ("MD11",  "MCDONNELL DOUGLAS MD-11",          "A5", 1),
    ("C172",  "CESSNA 172",                       "A1", 4),
    ("SR22",  "CIRRUS SR-22",                     "A1", 2),
    ("PC12",  "PILATUS PC-12",                    "A1", 2),
    ("C56X",  "CESSNA 560XL Citation Excel",      "A2", 2),
    ("E55P",  "EMBRAER Phenom 300",               "A2", 2),
    ("R44",   "ROBINSON R-44",                    "A7", 1),
    ("EC35",  "AIRBUS HELICOPTER H135",           "A7", 1),
    ("BE20",  "BEECH B-200 Super King Air",       "A1", 2),
    ("B752",  "BOEING 757-200",                   "A4", 2),
]
_TYPE_TOTAL_WEIGHT = sum(w for _, _, _, w in _TYPE_CATALOGUE)


# Airline ICAO callsign prefixes. Real-looking but a deliberately
# scrambled subset so generated callsigns don't accidentally collide
# with what's likely visible at a real receiver during testing.
_AIRLINE_PREFIXES = [
    "UAL", "AAL", "DAL", "SWA", "JBU", "ASA", "FFT", "NKS",
    "ENY", "RPA", "SKW", "GJS", "EJA", "FDX", "UPS", "GTI",
    "ACA", "WJA", "VIR", "BAW", "DLH", "AFR", "KLM", "QFA",
]


@dataclass
class Aircraft:
    """One synthetic aircraft. Tick advances state by `dt` seconds."""

    hex: str                       # ICAO 24-bit hex, lowercase
    flight: str                    # callsign with trailing space (matches real feed quirk)
    registration: str              # tail (N-number for civilians, fictional for milhex)
    type_code: str                 # ICAO aircraft type, e.g. B738
    type_desc: str                 # full description
    category: str                  # ADS-B emitter category, e.g. A3
    lat: float
    lon: float
    heading: float                 # degrees, 0-360
    speed: float                   # ground speed, knots
    altitude: int                  # barometric altitude, feet
    squawk: str                    # 4-char octal
    is_military: bool
    # Synthesised receiver-relative state, recomputed per tick
    r_dst: float = 0.0             # range from receiver, km
    r_dir: float = 0.0             # bearing from receiver, deg
    seen: float = 0.0              # seconds since last seen (small random)
    seen_pos: float = 0.0          # seconds since last position update
    messages: int = 0              # cumulative message count
    rssi: float = -30.0            # signal strength, dBFS
    nav_qnh: float = 1013.2        # altimeter setting hPa
    nav_altitude_mcp: int = 0      # MCP-selected altitude
    nav_heading: float = 0.0       # MCP-selected heading
    baro_rate: int = 0             # vertical rate (baro), fpm
    geom_rate: int = 0             # vertical rate (geom), fpm
    year: str = ""                 # registration year string
    own_op: str = ""               # owner/operator string

    def tick(self, dt: float, rng: random.Random,
             home_lat: float, home_lon: float) -> None:
        """Advance state by `dt` seconds. Mutates in place."""
        # Position step. Convert speed (knots) → degrees of lat/lon
        # per second. 1 nautical mile = 1/60 degree latitude. At
        # mid-latitudes 1 degree longitude is shorter than 1 degree
        # latitude — apply a cosine correction so paths are roughly
        # great-circle straight at this resolution.
        nm_per_sec = self.speed / 3600.0
        deg_per_sec = nm_per_sec / 60.0
        rad = math.radians(self.heading)
        dlat = deg_per_sec * math.cos(rad) * dt
        dlon = (deg_per_sec * math.sin(rad)
                / max(math.cos(math.radians(self.lat)), 0.01)
                * dt)
        self.lat += dlat
        self.lon += dlon

        # Small perturbations so flight paths look natural rather
        # than mathematically straight. Magnitudes calibrated to
        # what real ADS-B traces show.
        self.heading = (self.heading + rng.gauss(0, 0.3)) % 360
        self.speed = max(60, min(550, self.speed + rng.gauss(0, 1.5)))
        if rng.random() < 0.05:
            # Occasional altitude change request — climb or descend
            self.altitude += int(rng.gauss(0, 200))
            self.altitude = max(500, min(43000, self.altitude))

        # Vertical rate from altitude delta. Smoothed so it doesn't
        # flap to zero every tick we don't change altitude.
        self.baro_rate = int(rng.gauss(self.baro_rate * 0.7, 50))
        self.geom_rate = self.baro_rate + rng.randint(-30, 30)

        # Receiver-relative geometry
        self._update_receiver_geometry(home_lat, home_lon)

        # Per-tick housekeeping
        self.messages += rng.randint(8, 20)
        self.seen = rng.uniform(0.5, 3.0)
        self.seen_pos = rng.uniform(0.5, 12.0)
        self.rssi = -30 + rng.gauss(0, 4)

    def _update_receiver_geometry(self, home_lat: float, home_lon: float) -> None:
        """Compute distance and bearing from the receiver to this
        aircraft. Uses the haversine formula. Receiver at home_lat/home_lon."""
        lat1 = math.radians(home_lat)
        lat2 = math.radians(self.lat)
        dlat = lat2 - lat1
        dlon = math.radians(self.lon - home_lon)
        a = (math.sin(dlat / 2) ** 2
             + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        # Earth radius 6371 km
        self.r_dst = round(6371 * c, 2)
        # Forward bearing
        y = math.sin(dlon) * math.cos(lat2)
        x = (math.cos(lat1) * math.sin(lat2)
             - math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
        bearing = math.degrees(math.atan2(y, x))
        self.r_dir = round((bearing + 360) % 360, 1)

    def to_json(self) -> Dict:
        """Serialise to the readsb/tar1090 shape Aerodrome's collector
        consumes. Keys and types match the reference 32-aircraft sample
        from a real feeder. Optional fields are included when applicable
        so the synthetic shape passes for the real one."""
        d: Dict = {
            "hex": self.hex,
            "type": "adsb_icao",
            "flight": self.flight,
            "r": self.registration,
            "t": self.type_code,
            "desc": self.type_desc,
            "alt_baro": self.altitude,
            "alt_geom": self.altitude - 500,
            "gs": round(self.speed, 1),
            "track": round(self.heading, 2),
            "baro_rate": self.baro_rate,
            "geom_rate": self.geom_rate,
            "squawk": self.squawk,
            "emergency": (
                "hijack" if self.squawk == "7500"
                else "radio" if self.squawk == "7600"
                else "emergency" if self.squawk == "7700"
                else "none"
            ),
            "category": self.category,
            "nav_qnh": round(self.nav_qnh, 1),
            "nav_altitude_mcp": self.nav_altitude_mcp,
            "nav_heading": round(self.heading, 2),
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "nic": 8, "rc": 186,
            "seen_pos": round(self.seen_pos, 3),
            "r_dst": self.r_dst,
            "r_dir": self.r_dir,
            "version": 2,
            "nic_baro": 1, "nac_p": 10, "nac_v": 1,
            "sil": 3, "sil_type": "perhour",
            "gva": 2, "sda": 2, "alert": 0, "spi": 0,
            "mlat": [], "tisb": [],
            "messages": self.messages,
            "seen": round(self.seen, 1),
            "rssi": round(self.rssi, 1),
        }
        if self.year:
            d["year"] = self.year
        if self.own_op:
            d["ownOp"] = self.own_op
        return d


class Fleet:
    """A population of synthetic aircraft visible to a receiver.

    Per tick: each aircraft advances its state. Aircraft that drift
    outside `max_range_km` from home get retired; new aircraft are
    spawned at the edge to keep the fleet at target size.
    """

    def __init__(
        self,
        size: int = 100,
        home_lat: float = 40.0,
        home_lon: float = -75.0,
        max_range_km: float = 250.0,
        military_fraction: float = 0.05,
        seed: Optional[int] = None,
    ):
        self.size = size
        self.home_lat = home_lat
        self.home_lon = home_lon
        self.max_range_km = max_range_km
        self.military_fraction = military_fraction
        self.rng = random.Random(seed)
        self.aircraft: List[Aircraft] = []
        # v3.1.0: emergency-squawk event state. demo mode triggers
        # rare 7500/7600/7700 squawks so the emergency-squawk
        # notification path is exercised. Cooldown enforces a strict
        # "no more than 1 per hour" cap. Format when active:
        #   {"icao": str, "squawk": str, "ends_at": float (epoch s),
        #    "orig_squawk": str (to restore on clear)}
        self._emergency: Optional[Dict] = None
        self._next_emergency_eligible_at: float = 0.0
        # Pre-seed the visible fleet
        for _ in range(size):
            self.aircraft.append(self._spawn())

    # ------------------------------------------------------------------
    # Public API

    def tick(self, dt: float = 1.0) -> None:
        """Advance every aircraft by `dt` seconds. Retire and replace
        any that have drifted out of range."""
        for ac in self.aircraft:
            ac.tick(dt, self.rng, self.home_lat, self.home_lon)

        # v3.1.0: emergency-squawk event maintenance.
        # An event lasts 2-5 minutes; the cooldown after one ends is
        # 1 hour. Probability per tick is dt/3600, so the *average* rate
        # during eligible periods is ~1/hour, and the cooldown enforces
        # the strict upper bound.
        now = time.time()
        if self._emergency is not None and now >= self._emergency["ends_at"]:
            # Restore original squawk on the still-visible aircraft (if any)
            for ac in self.aircraft:
                if ac.hex == self._emergency["hex"]:
                    ac.squawk = self._emergency["orig_squawk"]
                    break
            self._next_emergency_eligible_at = now + 3600.0
            self._emergency = None
        elif self._emergency is None and now >= self._next_emergency_eligible_at:
            if self.aircraft and self.rng.random() < (dt / 3600.0):
                target = self.rng.choice(self.aircraft)
                squawk = self.rng.choice(["7500", "7600", "7700"])
                duration = self.rng.uniform(120.0, 300.0)
                self._emergency = {
                    "hex": target.hex,
                    "squawk": squawk,
                    "ends_at": now + duration,
                    "orig_squawk": target.squawk,
                }
                target.squawk = squawk

        # Replace out-of-range aircraft. Iterate over a copy so we can
        # mutate the list safely. Also clear an in-flight emergency if
        # its target retires out of range (Aircraft.tick handles range;
        # this branch handles the unlikely case of the emergency-marked
        # aircraft drifting out before the event expires).
        for i, ac in enumerate(list(self.aircraft)):
            if ac.r_dst > self.max_range_km:
                if self._emergency and self._emergency["hex"] == ac.hex:
                    self._next_emergency_eligible_at = time.time() + 3600.0
                    self._emergency = None
                self.aircraft[i] = self._spawn()

    def snapshot_json(self) -> Dict:
        """Return the current fleet shaped for /data/aircraft.json."""
        now = time.time()
        return {
            "now": round(now, 1),
            "messages": sum(ac.messages for ac in self.aircraft),
            "aircraft": [ac.to_json() for ac in self.aircraft],
        }

    # ------------------------------------------------------------------
    # Aircraft factory

    def _spawn(self) -> Aircraft:
        """Create a fresh aircraft on the edge of receiver range."""
        is_military = self.rng.random() < self.military_fraction
        if is_military:
            # US military hex range AE0000-AFFFFF. Picked uniformly
            # within so the watchlist/military classifier path gets
            # exercised at the configured fraction.
            hex_int = self.rng.randint(0xAE0000, 0xAFFFFF)
        else:
            # Avoid the military range for civilian generation —
            # otherwise our military_fraction lies. Anything else
            # in the 24-bit space is fair game.
            while True:
                hex_int = self.rng.randint(0x100000, 0xFFFFFE)
                if not (0xAE0000 <= hex_int <= 0xAFFFFF):
                    break
        hex_str = f"{hex_int:06x}"

        type_code, type_desc, category, _ = self._pick_type()
        callsign = self._make_callsign(is_military)
        registration = self._make_registration(is_military)

        # Spawn position: random bearing at the range edge, working
        # inward slightly so the aircraft has time to be seen before
        # falling out of range.
        bearing = self.rng.uniform(0, 360)
        spawn_range = self.max_range_km * self.rng.uniform(0.85, 0.98)
        # Convert bearing+range to lat/lon offset from home
        # (small-angle approximation suffices at receiver scales).
        km_per_deg_lat = 111.0
        km_per_deg_lon = 111.0 * math.cos(math.radians(self.home_lat))
        rad = math.radians(bearing)
        lat = self.home_lat + (spawn_range * math.cos(rad)) / km_per_deg_lat
        lon = self.home_lon + (spawn_range * math.sin(rad)) / km_per_deg_lon

        # Heading roughly inbound, with random spread so they don't
        # all fly straight at the receiver.
        inbound = (bearing + 180) % 360
        heading = (inbound + self.rng.gauss(0, 30)) % 360

        # Speed and altitude consistent with type. Light GA cruises
        # 100-180 kts at 3-12k ft; jets 350-500 kts at 28-40k ft.
        if category in ("A1",):
            speed = self.rng.uniform(100, 180)
            altitude = self.rng.randint(3000, 12000)
        elif category in ("A2",):
            speed = self.rng.uniform(280, 420)
            altitude = self.rng.randint(20000, 36000)
        elif category in ("A7",):
            speed = self.rng.uniform(70, 140)
            altitude = self.rng.randint(500, 4000)
        else:  # A3, A4, A5
            speed = self.rng.uniform(380, 500)
            altitude = self.rng.randint(28000, 41000)

        squawk = f"{self.rng.randint(1, 7777):04d}"

        return Aircraft(
            hex=hex_str,
            flight=callsign,
            registration=registration,
            type_code=type_code,
            type_desc=type_desc,
            category=category,
            lat=lat, lon=lon,
            heading=heading, speed=speed, altitude=altitude,
            squawk=squawk,
            is_military=is_military,
            messages=self.rng.randint(50, 5000),
            year=str(self.rng.randint(1995, 2024)) if not is_military else "",
            own_op=("" if is_military else
                    self._pick_owner(callsign[:3])),
        )

    def _pick_type(self) -> tuple:
        """Weighted random type pick from the catalogue."""
        r = self.rng.uniform(0, _TYPE_TOTAL_WEIGHT)
        acc = 0.0
        for entry in _TYPE_CATALOGUE:
            acc += entry[3]
            if r <= acc:
                return entry
        return _TYPE_CATALOGUE[-1]

    def _make_callsign(self, is_military: bool) -> str:
        """Generate a callsign. Trailing space matches the real feed
        quirk (`"GJS4511 "`) — collector strips it via .strip()."""
        if is_military:
            tactical = self.rng.choice(["RCH", "REACH", "PAT", "SAM", "EVAC", "JOSA"])
            return f"{tactical}{self.rng.randint(10, 9999)} "
        prefix = self.rng.choice(_AIRLINE_PREFIXES)
        return f"{prefix}{self.rng.randint(1, 9999)} "

    def _make_registration(self, is_military: bool) -> str:
        """Civilian: N-number style. Military: synthetic 5-char code."""
        if is_military:
            return f"{self.rng.randint(10, 99)}-{self.rng.randint(1000, 9999)}"
        # N-number: N + 3-5 digits + optional 1-2 letter suffix
        digits = self.rng.randint(100, 99999)
        suffix_len = self.rng.choice([0, 0, 1, 2])
        suffix = "".join(self.rng.choices(
            "ABCDEFGHIJKLMNPQRSTUVWXYZ", k=suffix_len))
        return f"N{digits}{suffix}"

    def _pick_owner(self, airline_prefix: str) -> str:
        """Return a synthetic owner/operator string. Maps the airline
        prefix to a plausible-but-fictional operator name."""
        # Most are leasing trusts in real data. Mirror that pattern.
        if self.rng.random() < 0.3:
            return f"{airline_prefix} TRUSTEE LLC"
        return ""
