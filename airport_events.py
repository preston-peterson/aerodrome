"""Takeoffs and landings the receiver sees (3.4.184).

The collector passes every position it stores through a Tracker:

  takeoff       first seen low near an airport, lined up with one of its
                runways, then climbing away;
  landing       seen descending to a runway, low and lined up, and then gone
                (no position for GAP seconds) or reporting itself on the ground;
  touch_and_go  a dip to a runway in the middle of a flight: down to it, then
                up again (practice circuits; a low pass or a go-around looks
                the same and counts too).

The thresholds are the ones measured on a real receiver's 208 days of stored
sightings before this was built: of the aircraft seen landing at an airport and
next seen taking off, 99.5 % took off from the same one. Only what the antenna
sees low near an airport can count, so an airport below the receiver's horizon
shows few or none: that is coverage, not a quiet airport.

The same Tracker fills in the history from the sightings already stored, once,
in the background: backfill_step() reads them in the order they were saved, a
slice at a time (BACKFILL_BUDGET_SEC per collector loop), so a Pi stays
responsive. It stops where live detection began (the horizon), and remembers
how far it got, so a restart carries on from there.

Standard library + airports.py only. Positions are (ts, lat, lon, altitude ft).
"""
import json
import logging
import math
import sqlite3
import time
from collections import defaultdict, deque

import airports as airports_data

logger = logging.getLogger("adsb.airport_events")

GAP = 600            # s without a position ends a visit
LOW_AGL = 2000       # ft above the field: low enough to be taking off or landing
CLIMB = 600          # ft gained within WIN points after the low point = climbing away
DESC = 400           # ft lost within WIN points before the low point = descending in
WIN = 5              # points (about 5 minutes at the default 60 s poll)
MIN_SPACING = 45     # s: faster polls are thinned to about one point a minute, as measured
NEAR_NM = 14         # an airport further than this from the point is not considered
PARALLEL_NM = 0.3    # parallel runways closer than this to the track can't be told apart
CAT_BONUS = {"major": 0.3, "regional": 0.2, "small": 0.0}
KINDS = ("takeoff", "landing", "touch_and_go")
# The same event found twice (live and by the fill-in, or across a restart) is
# kept once: same aircraft, kind and airport within this many seconds.
DEDUPE_SEC = {"takeoff": 300, "landing": 300, "touch_and_go": 90}
SESSION_GAP = 1800   # touch-and-goes this close together at one airport are one session
META_KEY = "airport_events_backfill"
BACKFILL_BUDGET_SEC = 1.5
BACKFILL_BATCH = 5000


def nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3440.065 * math.asin(min(1.0, math.sqrt(a)))


def _xy(lat0, lon0, lat, lon):
    """NM east and north of (lat0, lon0): flat earth, fine within ~20 NM."""
    return (lon - lon0) * 60 * math.cos(math.radians(lat0)), (lat - lat0) * 60


def _bearing(p0, p1):
    if (p0[1], p0[2]) == (p1[1], p1[2]):
        return None
    x, y = _xy(p0[1], p0[2], p1[1], p1[2])
    return math.degrees(math.atan2(x, y)) % 360


def _angdiff(a, b, mod=360.0):
    d = abs(a - b) % mod
    return min(d, mod - d)


class _Runway:
    __slots__ = ("la1", "lo1", "length", "ux", "uy", "axis", "name1", "name2")

    def __init__(self, la1, lo1, la2, lo2, name1="", name2=""):
        x2, y2 = _xy(la1, lo1, la2, lo2)
        self.length = math.hypot(x2, y2)
        self.la1, self.lo1 = la1, lo1
        self.ux, self.uy = (x2 / self.length, y2 / self.length) if self.length else (0.0, 0.0)
        self.axis = math.degrees(math.atan2(self.ux, self.uy)) % 360     # end 1 → end 2
        self.name1, self.name2 = name1 or "", name2 or ""

    def name_for(self, track):
        """The runway's name in the direction of travel (13L moving end 1 → end 2)."""
        if track is None:
            return ""
        return self.name1 if _angdiff(track, self.axis) < 90 else self.name2


def _digits(name):
    return "".join(ch for ch in name if ch.isdigit())


class AirportIndex:
    """The airports near the receiver, bucketed by half a degree."""

    def __init__(self, airports):
        self.by_code = {}
        self._cells = defaultdict(list)
        for a in airports:
            rws = []
            for rw in a.get("runways") or []:
                if len(rw) >= 4 and all(isinstance(v, (int, float)) for v in rw[:4]):
                    r = _Runway(*rw[:4], *(list(rw[4:6]) + ["", ""])[:2])
                    if r.length >= 0.05:
                        rws.append(r)
            pts = [(a["lat"], a["lon"])]
            for rw in a.get("runways") or []:
                if len(rw) >= 4:
                    pts += [(rw[0], rw[1]), (rw[2], rw[3])]
            entry = {"code": a["code"], "cat": a.get("cat") or "small", "lat": a["lat"], "lon": a["lon"],
                     "elev": float(a.get("elev") or 0), "elev_ft": a.get("elev"), "name": a.get("name") or "",
                     "runways": rws, "pts": pts}
            self.by_code[a["code"]] = entry
            self._cells[(math.floor(a["lat"] * 2), math.floor(a["lon"] * 2))].append(entry)

    def _near(self, lat, lon):
        ci, cj = math.floor(lat * 2), math.floor(lon * 2)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                yield from self._cells.get((ci + di, cj + dj), ())

    @staticmethod
    def _runway_fit(a, lat, lon, track, kind):
        """(score, beyond NM, runway name) for the runway this point lines up with, or None."""
        lim_cross, lim_beyond, lim_ang = (1.5, 12.0, 30.0) if kind == "arr" else (2.5, 8.0, 50.0)
        fits = []
        for rw in a["runways"]:
            px, py = _xy(rw.la1, rw.lo1, lat, lon)
            along = px * rw.ux + py * rw.uy
            cross = abs(px * rw.uy - py * rw.ux)
            beyond = -along if along < 0 else (along - rw.length if along > rw.length else 0.0)
            ang = _angdiff(track, rw.axis, 180) if track is not None else 0.0
            if cross <= lim_cross and beyond <= lim_beyond and ang <= lim_ang:
                fits.append((cross / lim_cross + ang / lim_ang, cross, beyond, ang, rw))
        if not fits:
            return None
        fits.sort(key=lambda f: f[0])
        best = fits[0]
        name = best[4].name_for(track)
        # A parallel runway about as close can't be told apart at one position a
        # minute: say only the number they share (13 for 13L/13R), or nothing.
        for f in fits[1:]:
            if abs(f[1] - best[1]) < PARALLEL_NM and _angdiff(f[4].axis, best[4].axis, 180) < 10:
                other = f[4].name_for(track)
                name = _digits(name) if _digits(name) and _digits(name) == _digits(other) else ""
                break
        return best[0], best[2], name

    def match(self, lat, lon, alt, track, kind):
        """The airport this low point belongs to, as (score, airport, runway name), or None.
        kind "dep" (taking off) or "arr" (coming down to a runway)."""
        best = None
        for a in self._near(lat, lon):
            agl = alt - a["elev"]
            if agl > LOW_AGL or agl < -400:
                continue
            d = min(nm(lat, lon, p[0], p[1]) for p in a["pts"])
            if d > NEAR_NM:
                continue
            if a["runways"]:
                fit = self._runway_fit(a, lat, lon, track, kind)
                if not fit:
                    continue
                # height must suit the distance: ~318 ft/NM on a 3° glide, generous both ways
                if kind == "arr" and agl > 600 + 450 * fit[1]:
                    continue
                score, rwy = fit[0] - CAT_BONUS.get(a["cat"], 0.0), fit[2]
            else:
                if d > 2.0:
                    continue
                score, rwy = 1.5 + d, ""
            if best is None or score < best[0]:
                best = (score, a, rwy)
        return best


class _Seg:
    """One aircraft's current visit (or the part since it was last on the ground)."""
    __slots__ = ("last", "head", "tail", "dep_done", "dep_ts", "dip_ts", "callsign")

    def __init__(self, ts, head_known=True, callsign=""):
        self.last = ts
        self.head = []                          # the first airborne points, until the takeoff check is done
        self.tail = deque(maxlen=2 * WIN + 1)   # the latest airborne points
        self.dep_done = not head_known
        self.dep_ts = None
        self.dip_ts = 0
        self.callsign = callsign


class Tracker:
    """Feeds positions in time order and returns the events they complete.

    head_unknown: aircraft already in view when tracking began. Their visit
    began before the first position fed here, so its first positions aren't a
    takeoff; only a later one (after a landing or a gap) can be."""

    def __init__(self, index, head_unknown=()):
        self.index = index
        self._segs = {}
        self._head_unknown = set(head_unknown)

    def __len__(self):
        return len(self._segs)

    def _event(self, icao, seg, kind, m, ts):
        return {"icao": icao, "kind": kind, "airport": m[1]["code"], "runway": m[2], "at": int(ts),
                "callsign": seg.callsign}

    def feed(self, icao, ts, callsign, lat, lon, alt):
        events = []
        seg = self._segs.get(icao)
        if seg is not None:
            if ts <= seg.last:
                return events                    # out of order or a repeat
            if ts - seg.last > GAP:
                events += self._close(icao, seg)
                seg = None
        if seg is None:
            known = icao not in self._head_unknown
            self._head_unknown.discard(icao)
            seg = self._segs[icao] = _Seg(ts, known)
        seg.last = ts
        cs = (callsign or "").strip()
        if cs:
            seg.callsign = cs
        if lat is None or lon is None:
            return events                        # no position: only keeps the visit going
        if alt is None:
            # A position without an altitude is a surface report: it's on the ground.
            if seg.tail:
                events += self._arrival(icao, seg)
                self._segs[icao] = _Seg(ts, True, seg.callsign)
            return events
        if seg.tail and ts - seg.tail[-1][0] < MIN_SPACING:
            return events
        p = (ts, lat, lon, alt)
        seg.tail.append(p)
        if not seg.dep_done:
            seg.head.append(p)
            events += self._departure(icao, seg)
        events += self._dip(icao, seg)
        return events

    def _departure(self, icao, seg):
        air = seg.head
        h = air[:WIN + 1]
        lo = min(range(len(h)), key=lambda i: h[i][3])
        after = air[lo:lo + WIN + 1]
        if len(after) >= 3 and max(p[3] for p in after) - after[0][3] >= CLIMB:
            seg.dep_done, seg.head = True, []
            p0 = after[0]
            if lo and max(p[3] for p in h[:lo]) - p0[3] >= DESC:
                # It came down to this point before climbing: first seen on an
                # approach, so a touch and go (or a go-around), not a takeoff.
                m = self.index.match(p0[1], p0[2], p0[3], _bearing(h[lo - 1], p0), "arr")
                if m:
                    seg.dip_ts = p0[0]
                    return [self._event(icao, seg, "touch_and_go", m, p0[0])]
                return []
            m = self.index.match(p0[1], p0[2], p0[3], _bearing(p0, after[1]), "dep")
            if m:
                seg.dep_ts = p0[0]
                return [self._event(icao, seg, "takeoff", m, p0[0])]
            return []
        if len(air) >= 2 * WIN + 1:
            seg.dep_done, seg.head = True, []
        return []

    def _arrival(self, icao, seg):
        air = list(seg.tail)
        if len(air) < 3:
            return []
        t = air[-(WIN + 1):]
        lo = min(range(len(t)), key=lambda i: t[i][3])
        k = len(air) - len(t) + lo
        before = air[max(0, k - WIN):k + 1]
        if len(before) >= 3 and before[0][3] - before[-1][3] >= DESC:
            p1 = before[-1]
            if p1[0] in (seg.dip_ts, seg.dep_ts):
                return []
            m = self.index.match(p1[1], p1[2], p1[3], _bearing(before[-2], p1), "arr")
            if m:
                return [self._event(icao, seg, "landing", m, p1[0])]
        return []

    def _dip(self, icao, seg):
        w = seg.tail
        if len(w) < 2 * WIN + 1:
            return []
        c = w[WIN]
        if c[0] == seg.dep_ts or c[0] <= seg.dip_ts:
            return []
        alts = [p[3] for p in w]
        if c[3] > min(alts) or max(alts[:WIN]) - c[3] < DESC or max(alts[WIN + 1:]) - c[3] < CLIMB:
            return []
        m = self.index.match(c[1], c[2], c[3], _bearing(w[WIN - 1], c), "arr")
        if not m:
            return []
        seg.dip_ts = c[0]
        return [self._event(icao, seg, "touch_and_go", m, c[0])]

    def _close(self, icao, seg):
        self._segs.pop(icao, None)
        return self._arrival(icao, seg) if seg.tail else []

    def close_stale(self, now):
        """End the visits with no position for GAP seconds; returns their landings."""
        events = []
        for icao, seg in list(self._segs.items()):
            if now - seg.last > GAP:
                events += self._close(icao, seg)
        return events

    def drop_open(self):
        """Forget the visits still open (the fill-in's end: live detection has them)."""
        self._segs.clear()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def record(conn, events):
    """Store events, skipping ones already stored (DEDUPE_SEC). Returns rows added.
    The caller commits."""
    added = 0
    for e in events:
        w = DEDUPE_SEC.get(e["kind"], 300)
        cur = conn.execute(
            "INSERT INTO airport_events (icao, callsign, kind, airport, runway, at) "
            "SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM airport_events "
            "WHERE icao = ? AND kind = ? AND airport = ? AND at BETWEEN ? AND ?)",
            (e["icao"], e.get("callsign") or "", e["kind"], e["airport"], e.get("runway") or "", e["at"],
             e["icao"], e["kind"], e["airport"], e["at"] - w, e["at"] + w))
        added += max(cur.rowcount, 0)
    return added


def _receiver(config):
    r = (config or {}).get("receiver") or {}
    try:
        return float(r.get("latitude")), float(r.get("longitude"))
    except (TypeError, ValueError):
        return None


def index_for(config):
    """The AirportIndex for the configured receiver position, or None without one."""
    pos = _receiver(config)
    if pos is None:
        return None
    key = (round(pos[0], 4), round(pos[1], 4))
    if _index_cache.get("key") != key:
        _index_cache.update(key=key, index=AirportIndex(airports_data.near(*pos)))
    return _index_cache["index"]


_index_cache = {"key": None, "index": None}


def _meta(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS _aerodrome_meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM _aerodrome_meta WHERE key = ?", (META_KEY,)).fetchone()
    try:
        return json.loads(row[0]) if row and row[0] else None
    except ValueError:
        return None


def _save_meta(conn, state):
    conn.execute("INSERT OR REPLACE INTO _aerodrome_meta (key, value) VALUES (?, ?)",
                 (META_KEY, json.dumps(state, separators=(",", ":"))))


def _resume(conn, index, from_ts):
    """A Tracker that starts at from_ts: aircraft seen in the GAP before it are mid-visit."""
    rows = conn.execute("SELECT DISTINCT icao FROM all_sightings WHERE seen_at >= ? AND seen_at < ?",
                        (from_ts - GAP, from_ts)).fetchall()
    return Tracker(index, {r[0] for r in rows})


# ---------------------------------------------------------------------------
# Live: called by the collector once per poll, inside its transaction
# ---------------------------------------------------------------------------

_live = {"tracker": None, "index": None, "failed": False}


def reset():
    """Forget the in-memory state (tests; a receiver position change does it itself)."""
    _live.update(tracker=None, index=None, failed=False)
    _backfill.update(tracker=None, index=None, rowid=None)
    _index_cache.update(key=None, index=None)


def on_poll(conn, config, points, now):
    """points: [(icao, callsign, lat, lon, altitude)] stored by this poll at `now`.
    Stores the takeoffs, landings and touch-and-goes they complete. Never raises."""
    try:
        index = index_for(config)
        if index is None:
            return 0
        events = []
        if _live["tracker"] is None or _live["index"] is not index:
            events += _start_live(conn, index, now)
        tr = _live["tracker"]
        for icao, cs, lat, lon, alt in points:
            events += tr.feed(icao, now, cs, lat, lon, alt)
        events += tr.close_stale(now)
        added = record(conn, events) if events else 0
        _live["failed"] = False
        return added
    except Exception as e:      # never let this stop a poll from being stored
        if not _live["failed"]:
            logger.warning(f"Takeoff/landing detection skipped a poll: {e}", exc_info=True)
        _live.update(failed=True, tracker=None)
        return 0


def _start_live(conn, index, now):
    """Begin live tracking: pick up the aircraft in view from the last few minutes
    of stored positions, and on the first start ever, set where the fill-in stops."""
    from_ts = now - 2 * GAP
    tr = _resume(conn, index, from_ts)
    events = []
    for icao, cs, lat, lon, alt, ts in conn.execute(
            "SELECT icao, callsign, lat, lon, altitude, seen_at FROM all_sightings "
            "WHERE seen_at >= ? AND seen_at < ? ORDER BY seen_at, rowid", (from_ts, now)):
        events += tr.feed(icao, ts, cs, lat, lon, alt)
    _live.update(tracker=tr, index=index)
    if _meta(conn) is None:
        lo = conn.execute("SELECT MIN(rowid), MAX(rowid) FROM all_sightings").fetchone()
        _save_meta(conn, {"horizon_ts": int(now), "first_rowid": lo[0] or 0, "last_rowid": lo[1] or 0,
                          "cursor": 0, "cursor_ts": 0, "done": not lo[0], "started_at": int(now), "found": 0})
    return events


# ---------------------------------------------------------------------------
# Fill-in from the stored sightings (the collector loop calls this)
# ---------------------------------------------------------------------------

_backfill = {"tracker": None, "index": None, "rowid": None}


def backfill_step(db_path, config, budget=BACKFILL_BUDGET_SEC, open_conn=None):
    """Work through the stored sightings for up to `budget` seconds. Returns True
    when there is nothing (left) to do. Never raises."""
    conn = None
    try:
        index = index_for(config)
        if index is None:
            return False
        conn = open_conn(db_path) if open_conn else sqlite3.connect(db_path, timeout=30)
        state = _meta(conn)
        if state is None:
            return False            # live tracking hasn't started yet; it sets the horizon
        if state.get("done"):
            return True
        if _backfill["tracker"] is None or _backfill["index"] is not index:
            _start_backfill(conn, index, state)
        tr = _backfill["tracker"]
        horizon = state["horizon_ts"]
        deadline = time.monotonic() + budget
        finished = False
        while not finished:
            rows = conn.execute("SELECT rowid, icao, callsign, lat, lon, altitude, seen_at FROM all_sightings "
                                "WHERE rowid > ? ORDER BY rowid LIMIT ?",
                                (_backfill["rowid"], BACKFILL_BATCH)).fetchall()
            events = []
            last = None
            for rowid, icao, cs, lat, lon, alt, ts in rows:
                if ts >= horizon:
                    finished = True
                    break
                if last is None or ts - last >= 60:
                    events += tr.close_stale(ts)
                    last = ts
                events += tr.feed(icao, ts, cs, lat, lon, alt)
                _backfill["rowid"] = rowid
                if ts > state.get("cursor_ts", 0):
                    state["cursor_ts"] = ts
            if not rows or len(rows) < BACKFILL_BATCH:
                finished = True
            if finished:
                events += tr.close_stale(horizon)
                tr.drop_open()
            state["cursor"] = _backfill["rowid"]
            if events:
                state["found"] = state.get("found", 0) + record(conn, events)
            if finished:
                state.update(done=True, finished_at=int(time.time()))
                logger.info(f"Takeoff/landing history filled in from the stored sightings: "
                            f"{state['found']} found")
            _save_meta(conn, state)
            conn.commit()
            if time.monotonic() >= deadline:
                break
        return finished
    except Exception as e:
        logger.warning(f"Takeoff/landing fill-in paused: {e}")
        _backfill.update(tracker=None)
        return False
    finally:
        if conn is not None:
            conn.close()


def _start_backfill(conn, index, state):
    if state.get("cursor"):
        # Carry on from where it stopped, a few minutes back so the aircraft in
        # view then are picked up again (anything found twice is stored once).
        from_ts = max(0, state["cursor_ts"] - 2 * GAP)
        row = conn.execute("SELECT rowid FROM all_sightings WHERE seen_at >= ? ORDER BY seen_at LIMIT 1",
                           (from_ts,)).fetchone()
        start = min(state["cursor"], row[0] - 1) if row else state["cursor"]
        tr = _resume(conn, index, from_ts)
    else:
        start, tr = 0, Tracker(index)
    _backfill.update(tracker=tr, index=index, rowid=start)


def backfill_status(conn):
    """{"state": "waiting" | "running" | "done", "pct": 0-100} for the pages."""
    try:
        state = _meta(conn)
    except sqlite3.Error:
        state = None
    if state is None:
        return {"state": "waiting", "pct": 0}
    if state.get("done"):
        return {"state": "done", "pct": 100}
    lo, hi, cur = state.get("first_rowid") or 0, state.get("last_rowid") or 0, state.get("cursor") or 0
    pct = int(100 * (cur - lo) / (hi - lo)) if hi > lo and cur > lo else 0
    return {"state": "running", "pct": max(0, min(99, pct))}


# ---------------------------------------------------------------------------
# Reading, for the aircraft page, the Live map panel and Stats
# ---------------------------------------------------------------------------

def _names(config):
    index = index_for(config)
    return {c: a["name"] for c, a in index.by_code.items()} if index else {}


def _group(rows):
    """Newest-first rows (kind, airport, runway, at, callsign[, icao]) → display items,
    one aircraft's back-to-back touch-and-goes at one airport folded into one session:
    {"kind", "airport", "runway", "at", "until", "count", "callsign"[, "icao"]}."""
    items = []
    for row in rows:
        kind, airport, runway, at, callsign = row[:5]
        icao = row[5] if len(row) > 5 else None
        last = items[-1] if items else None
        if (kind == "touch_and_go" and last and last["kind"] == "touch_and_go"
                and last["airport"] == airport and last.get("icao") == icao and last["at"] - at <= SESSION_GAP):
            last["count"] += 1
            last["at"] = at                       # the session's first, `until` its last
            if runway and not last["runway"]:
                last["runway"] = runway
            continue
        item = {"kind": kind, "airport": airport, "runway": runway or "", "at": at, "until": at,
                "count": 1, "callsign": callsign or ""}
        if icao is not None:
            item["icao"] = icao
        items.append(item)
    return items


def aircraft_history(conn, config, icao, limit=8):
    """One aircraft's takeoffs and landings: per-airport totals, the newest events
    (touch-and-go sessions folded) and `recent`, its latest flight's events."""
    rows = conn.execute("SELECT kind, airport, runway, at, callsign FROM airport_events "
                        "WHERE icao = ? ORDER BY at DESC", (icao,)).fetchall()
    names = _names(config)
    items = _group(rows)
    per = {}
    for kind, airport, *_ in rows:
        c = per.setdefault(airport, {"code": airport, "name": names.get(airport, ""),
                                     "landings": 0, "takeoffs": 0, "touch_and_go": 0})
        c[{"takeoff": "takeoffs", "landing": "landings"}.get(kind, "touch_and_go")] += 1
    for it in items:
        it["name"] = names.get(it["airport"], "")
    recent = []
    if items:
        recent = [items[0]]
        if (items[0]["kind"] == "landing" and len(items) > 1 and items[1]["kind"] == "takeoff"
                and items[0]["at"] - items[1]["until"] <= 12 * 3600):
            recent = [items[1], items[0]]
    first = conn.execute("SELECT MIN(at) FROM airport_events").fetchone()[0]
    return {"ok": True, "since": first, "total": len(items),
            "airports": sorted(per.values(), key=lambda c: -(c["landings"] + c["takeoffs"] + c["touch_and_go"])),
            "events": items[:max(0, int(limit))], "recent": recent, "fill_in": backfill_status(conn)}


def _moves(kind):
    """(landed, took off) a stored event adds to an airport's counts."""
    return {"takeoff": (0, 1), "landing": (1, 0)}.get(kind, (1, 1))


def stats_cards(conn, config, start_ts, end_ts, tz, want):
    """The Stats "Airports" cards named in `want`, from today's events (start_ts..end_ts,
    local midnight to now in tz) and the last 30 days."""
    from datetime import datetime
    names = _names(config)
    out = {}
    today = conn.execute("SELECT kind, airport, at FROM airport_events WHERE at >= ? AND at <= ?",
                         (start_ts, end_ts)).fetchall()
    fill = backfill_status(conn)
    if "airport_moves_today" in want:
        out["airport_moves_today"] = {"total": sum(sum(_moves(k)) for k, _, _ in today),
                                      "airports": len({a for _, a, _ in today}), "fill_in": fill}
    if "airports_today" in want:
        rows = {}
        now_hour = datetime.fromtimestamp(end_ts, tz).hour
        for kind, airport, at in today:
            r = rows.setdefault(airport, {"code": airport, "name": names.get(airport, ""), "landed": 0,
                                          "took_off": 0, "last_hour": 0, "hours": [0] * 24})
            landed, took = _moves(kind)
            r["landed"] += landed
            r["took_off"] += took
            r["hours"][datetime.fromtimestamp(at, tz).hour] += landed + took
            if at > end_ts - 3600:
                r["last_hour"] += landed + took
        out["airports_today"] = {"now_hour": now_hour, "fill_in": fill,
                                 "rows": sorted(rows.values(), key=lambda r: (-(r["landed"] + r["took_off"]), r["code"]))[:12]}
    if "airports_30d" in want:
        since = end_ts - 30 * 86400
        first = conn.execute("SELECT MIN(at) FROM airport_events").fetchone()[0]
        days = max(1.0, min(30.0, (end_ts - (first or end_ts)) / 86400.0))
        totals = defaultdict(int)
        for kind, airport in conn.execute("SELECT kind, airport FROM airport_events WHERE at >= ?", (since,)):
            totals[airport] += sum(_moves(kind))
        top = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        # v3.4.185: with the fill-in status, so the card says "filling in" instead
        # of averaging a month it hasn't reached yet (it goes oldest first).
        out["airports_30d"] = {"rows": [{"code": c, "name": names.get(c, ""), "per_day": round(n / days)}
                                        for c, n in top], "fill_in": fill}
    return out


_COMPASS = ("north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest")
_COMPASS_SHORT = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _flow(runway):
    """The direction traffic is moving for a runway name: 35C → north, 13 → southeast."""
    d = _digits(runway or "")
    if not d:
        return ""
    return _COMPASS[int(((int(d) * 10) % 360 + 22.5) // 45) % 8]


def airport_summary(conn, config, code, now, day_start):
    """The Live map's airport panel: the airport, today's counts, the runways in use
    over the last hour, its latest takeoffs and landings, today's visitors and how
    many it sees on a typical day. None for an airport not near the receiver."""
    from distance import haversine
    index = index_for(config)
    a = index.by_code.get(code) if index else None
    if a is None:
        return None
    unit = (((config or {}).get("receiver") or {}).get("distance_unit") or "mi").lower()
    rx = _receiver(config)
    dist = direction = None
    if rx:
        dist = haversine(rx[0], rx[1], a["lat"], a["lon"], unit=unit if unit in ("mi", "km", "nm") else "mi")
        x, y = _xy(rx[0], rx[1], a["lat"], a["lon"])
        direction = _COMPASS_SHORT[int((math.degrees(math.atan2(x, y)) % 360 + 22.5) // 45) % 8]
    today = conn.execute("SELECT kind, icao, runway, at FROM airport_events WHERE airport = ? AND at >= ? AND at <= ?",
                         (code, day_start, now)).fetchall()
    counts = {"landed": 0, "took_off": 0, "touch_and_go": 0, "last_hour": 0}
    land_rw, dep_rw = defaultdict(int), defaultdict(int)
    for kind, _icao, runway, at in today:
        landed, took = _moves(kind)
        counts["landed"] += landed
        counts["took_off"] += took
        counts["touch_and_go"] += kind == "touch_and_go"
        if at > now - 3600:
            counts["last_hour"] += landed + took
            if runway and landed:
                land_rw[runway] += 1
            if runway and took:
                dep_rw[runway] += 1
    top = lambda d: [r for r, _ in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    lead = top(land_rw)[:1] or top(dep_rw)[:1]
    rows = conn.execute("SELECT kind, airport, runway, at, callsign, icao FROM airport_events "
                        "WHERE airport = ? ORDER BY at DESC LIMIT 60", (code,)).fetchall()
    latest = _group(rows)[:6]
    icaos = sorted({r[1] for r in today})
    types, cats = defaultdict(int), defaultdict(int)
    kinds = {}
    for i in range(0, len(icaos), 500):
        chunk = icaos[i:i + 500]
        q = ",".join("?" * len(chunk))
        for icao, typ, cat in conn.execute(f"SELECT icao, aircraft_type, category FROM seen_aircraft WHERE icao IN ({q})", chunk):
            kinds[icao] = (typ or "", cat or "unknown")
    for icao in icaos:
        typ, cat = kinds.get(icao, ("", "unknown"))
        if typ:
            types[typ] += 1
        cats[cat] += 1
    for it in latest:
        it["type"] = kinds.get(it["icao"], ("", ""))[0] or _type_of(conn, it["icao"])
    watch = 0
    if icaos:
        q = ",".join("?" * len(icaos))
        try:
            watch = conn.execute(f"SELECT COUNT(DISTINCT icao) FROM watchlist_hourly WHERE hour_bucket >= ? "
                                 f"AND icao IN ({q})", [day_start - day_start % 3600, *icaos]).fetchone()[0]
        except sqlite3.OperationalError:
            watch = 0
    first = conn.execute("SELECT MIN(at) FROM airport_events").fetchone()[0]
    days = max(1.0, min(30.0, (now - (first or now)) / 86400.0))
    month = conn.execute("SELECT kind FROM airport_events WHERE airport = ? AND at >= ?", (code, now - 30 * 86400)).fetchall()
    per_day = round(sum(sum(_moves(k)) for (k,) in month) / days)
    return {"ok": True, "code": a["code"], "name": a["name"], "cat": a["cat"],
            "elev": None if a["elev_ft"] is None else int(a["elev_ft"]), "runways": len(a["runways"]),
            "distance": None if dist is None else round(dist), "distance_unit": unit, "direction": direction,
            "today": counts,
            "in_use": {"landing": top(land_rw), "taking_off": top(dep_rw), "flow": _flow(lead[0]) if lead else ""},
            "latest": latest,
            "visitors": {"aircraft": len(icaos),
                         "types": [{"type": t, "n": n} for t, n in sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))[:4]],
                         "mix": {c: n for c, n in cats.items()},
                         "military": cats.get("military", 0), "watchlist": watch},
            "per_day": per_day, "fill_in": backfill_status(conn)}


def _type_of(conn, icao):
    row = conn.execute("SELECT aircraft_type FROM seen_aircraft WHERE icao = ?", (icao,)).fetchone()
    return (row[0] or "") if row else ""
