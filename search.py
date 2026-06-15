"""
Aerodrome search backend (v2.51.0 Phase 2).

This module turns user-typed query strings into filtered, ranked
aircraft results from the search-feature schema introduced in Phase 1.
It owns three concerns:

  1. parse_query(q) — token classifier. Splits the query on whitespace,
     classifies each token by pattern (ICAO hex, type code, country,
     date, callsign, free text), and returns a structured filter list.
  2. execute_search(conn, parsed, limit, offset) — SQL builder + executor.
     Translates the filter list into a single-table SELECT against
     seen_aircraft + seen_aircraft_fts (no JOINs to sightings_hourly —
     the denormalization from Phase 1 makes that unnecessary). Returns
     rows + total count.
  3. detail_for_aircraft(conn, icao) — per-ICAO detail used by the
     /api/search/aircraft/{icao} endpoint.

Design notes (also captured in docs/SEARCH_DESIGN.md):

- All filters that hit indexed columns (icao, registration, last_callsign,
  aircraft_type, country) become equality predicates against B-tree
  indexes. These are sub-millisecond on any plausible install size.
- Free-text tokens go through FTS5 via `seen_aircraft_fts MATCH ?`.
  We collect them into a single FTS5 query string and emit one
  MATCH clause; FTS5 internally handles tokenization and AND/OR
  semantics across columns.
- The ranking expression is computed in SQL as part of the SELECT,
  not in Python, so ORDER BY can use indexes when possible.
- Date tokens parse to a [start_ts, end_ts) range and become a
  last_seen_at filter. We deliberately use last_seen_at rather than
  joining to sightings_hourly — the question "did I see X in March?"
  for a typical aircraft is answered correctly by checking whether
  its last sighting was in March. Edge case: an aircraft seen daily
  from January through April will have last_seen_at in April, so
  searching "March 2026" wouldn't return it. This is a known limit
  acknowledged in the design doc — the alternative (join to rollup
  for time-range filtering) would cost the per-result join we
  worked hard to eliminate. Filed for Phase 5+ if it bites.

The module is intentionally pure-Python with no FastAPI dependency.
The endpoint glue lives in server.py.
"""
import logging
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from countries import known_countries
from designators import AIRCRAFT_TYPES, AIRLINES, aircraft_type_name, airline_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token classification regexes
# ---------------------------------------------------------------------------
# Order matters in the parser: we try patterns in priority order and
# accept the first match. ICAO hex must come before type code because
# both are 6-char and 4-char strings of letters and digits — ICAO is
# checked first because it's a literal address; if a 6-char hex string
# matches a known aircraft the user almost certainly means the aircraft.
#
# All patterns are compiled at module load. Cheap, and makes the parser
# itself a small function with no regex setup overhead.

_ICAO_HEX_RE      = re.compile(r"^[0-9A-Fa-f]{6}$")
_SQUAWK_RE        = re.compile(r"^(7500|7600|7700)$")
_DATE_FULL_RE     = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")  # 2026-04-29
_DATE_MONTH_RE    = re.compile(r"^(\d{4})-(\d{2})$")          # 2026-04
_DATE_YEAR_RE     = re.compile(r"^(\d{4})$")                  # 2026 (4-digit year)
# v2.52.0: locale-specific slash-separated date formats. The numeric
# parts only — locale (MDY vs DMY) determines which positions mean what,
# resolved at runtime via _try_parse_date(tok, date_format=...). One
# regex matches both since the structure is the same:
#   M/D/YY       D/M/YY     1-2 digits, 1-2 digits, 2 digits
#   M/D/YYYY     D/M/YYYY   1-2, 1-2, 4 digits
# Year ambiguity (2-digit) resolves: 00-69 → 2000-2069, 70-99 → 1970-1999,
# matching POSIX strptime convention. We don't actually expect 1970s
# aircraft sightings; the rule mostly avoids classifying random tokens.
_DATE_SLASH_RE    = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")
# Registration: either "N" + digits + optional alphanumeric (US tails)
# or 1-2 letter prefix + dash + alphanumeric (everything else: G-XYZA,
# D-ABCD, JA-001A). The dash is required for non-US tails because
# without it the pattern matches any short word — too many false
# positives from typing "HELLO" or "Delta".
_REGISTRATION_RE  = re.compile(r"^(N[0-9][A-Z0-9]{0,4}|[A-Z]{1,2}-[A-Z0-9]+)$")
_CALLSIGN_FULL_RE = re.compile(r"^([A-Z]{3})([0-9][A-Z0-9]*)$")  # UAL2024
_CALLSIGN_PREFIX_RE = re.compile(r"^([A-Z]{3})$")               # UAL alone


# Reasonable bounds. The query parser caps tokens at this many to
# defend against pathological inputs (a user pasting a paragraph) —
# beyond these we just stop classifying and treat the rest as free text.
MAX_TOKENS = 16


# v2.60.0: allowlist of column identifiers that are valid in the
# /api/search?order= parameter. Maps the public column name (matching
# the `data-c` attribute on the Search column-header strip in
# templates/index.html) to the actual SQL ORDER BY expression. The
# expressions reference the same tables joined by the search query —
# seen_aircraft and (for last-state columns) the latest_h subquery
# alias `lh`.
#
# IMPORTANT: never interpolate user input into the SQL — the user's
# `?order=` parameter is matched against this dict's KEYS and the
# corresponding VALUE (a hardcoded SQL fragment) goes into the query.
# This makes the sort feature SQL-injection-safe by construction.
SORTABLE_COLUMNS = {
    "icao":           "seen_aircraft.icao",
    "callsign":       "seen_aircraft.last_callsign",
    "aircraft_type":  "seen_aircraft.aircraft_type",
    "type_desc":      "seen_aircraft.aircraft_type_desc",
    "operator":       "seen_aircraft.operator",
    "country":        "seen_aircraft.country",
    "speed":          "latest_h.last_speed",
    "altitude":       "latest_h.last_altitude",
    "squawk":         "latest_h.last_squawk",
    # v2.60.1 (Phase 1A.5 perf): distance is now a stored column on
    # seen_aircraft (canonical km, populated by the collector at
    # write time + at startup / receiver-location-change). ORDER BY
    # the column directly is fast and sorts the FULL result set,
    # not just the visible page. NULL handling is done in the
    # ORDER BY construction below (see _build_order_by_clauses).
    "distance":       "seen_aircraft.last_distance",
    "seen_at":        "seen_aircraft.last_seen_at",
    "first_seen_at":  "seen_aircraft.first_seen_at",
    "sightings":      "seen_aircraft.sighting_count",
    # v3.4.62: track length = the all-time longest single continuous
    # track (session) for the aircraft, read from the stored
    # seen_aircraft.best_track_seconds column (maintained by the
    # collector from the aircraft_track_daily rollup; migration v11).
    # Through v3.4.61 this was `last_seen_at - first_seen_at` — the
    # lifetime span from first-ever to most-recent sighting, which for
    # a regular reads ~the retention window, not a track duration. A
    # stored column lets the ORDER BY sort the FULL result set (same
    # reason last_distance and sighting_count are stored). NULL (no
    # tracked session) falls out of the existing `<col> IS NULL` prefix
    # in the ORDER BY construction, sorting last like every other sort.
    "track_length":   "seen_aircraft.best_track_seconds",
}

# Per-column sensible default direction when the user hasn't specified
# one. Numeric / time columns default DESC (highest/most-recent first
# is the usual goal); text columns default ASC (alphabetical). Matches
# the click-cycle starting direction users would expect when they
# first click an unsorted column.
_SORT_DEFAULT_DIR = {
    "icao":           "asc",
    "callsign":       "asc",
    "aircraft_type":  "asc",
    "type_desc":      "asc",
    "operator":       "asc",
    "country":        "asc",
    "speed":          "desc",
    "altitude":       "desc",
    "squawk":         "asc",
    "distance":       "asc",
    "seen_at":        "desc",
    "first_seen_at":  "desc",
    "sightings":      "desc",
    # v2.81.0: longest-first matches the natural first-click question
    # ("which aircraft hung around the longest?"). Click-cycle flips
    # to ASC on second click for users who want one-sighting blips.
    "track_length":   "desc",
}


def _resolve_order_direction(order: Optional[str],
                              direction: Optional[str]
                              ) -> Tuple[Optional[str], Optional[str]]:
    """Validate and normalize the (order, direction) pair.

    Returns (sql_expr, dir) where:
      sql_expr is the safe SQL ORDER BY column expression, or None if
        the user-supplied order isn't in the allowlist (caller should
        fall back to relevance).
      dir is 'ASC' or 'DESC' (uppercase, for clean SQL emission).

    Both fields can be None — caller treats that as "use relevance".
    """
    if not order:
        return (None, None)
    sql_expr = SORTABLE_COLUMNS.get(order)
    if sql_expr is None:
        return (None, None)
    if direction and direction.lower() in ("asc", "desc"):
        dir_norm = direction.upper()
    else:
        dir_norm = _SORT_DEFAULT_DIR.get(order, "desc").upper()
    return (sql_expr, dir_norm)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_query(q: str, date_format: str = "MDY",
                 tz_offset_sec: int = 0) -> Dict[str, Any]:
    """Parse a search query string into a structured filter set.

    Args:
      q: the user's search input string
      date_format: locale for slash-separated dates. "MDY" (US, default),
                   "DMY" (European), or "ISO" (rejects slash dates entirely).
                   Case-insensitive. Invalid values fall back to MDY.
      tz_offset_sec: v2.66.2 — UTC offset in seconds for the user's
                   configured timezone (stats.timezone in config). Used
                   by the relative-date `today` and `hour:N` tokens to
                   compute window boundaries that match the Patterns
                   hourly histogram's tz semantics. Default 0 means
                   server-local time (the v2.65.0 behavior). Caller
                   typically computes this from CONFIG['stats']['timezone']
                   the same way the histogram backend does.

    Returns a dict with:
      filters:    list of {field, match, value} for indexed-column filters
      free_text:  list of strings for FTS5 MATCH (combined with implicit AND)
      time_range: optional (start_ts, end_ts) tuple from a date token
      raw_tokens: original token list for debugging

    The parser is forgiving: unrecognized tokens fall through to free
    text. A user who types nonsense gets a free-text search rather
    than an error.
    """
    # Normalize date_format. Anything unrecognized → MDY default.
    if isinstance(date_format, str):
        date_format = date_format.upper()
    if date_format not in ("MDY", "DMY", "ISO"):
        date_format = "MDY"
    out = {
        "filters": [],
        "free_text": [],
        "time_range": None,
        "raw_tokens": [],
    }
    if not q or not q.strip():
        return out

    # Split on whitespace, normalize. We don't lowercase — case matters
    # for some patterns (callsigns are uppercase, registrations are
    # uppercase, etc.). Case is normalized inside each classifier.
    tokens = q.strip().split()
    if len(tokens) > MAX_TOKENS:
        tokens = tokens[:MAX_TOKENS]
    out["raw_tokens"] = tokens

    countries = known_countries()
    # Build a country-name lookup that's case-insensitive and tolerates
    # multi-word entries. We have to handle "United States" and
    # "United Kingdom" which span two whitespace-split tokens. Approach:
    # try multi-token combinations FIRST (greedy left-to-right), then
    # single-token classification on whatever's left.
    countries_lower = {c.lower(): c for c in countries}

    consumed = [False] * len(tokens)

    # First pass: greedy multi-token country match. We try 2-, 3-,
    # 4-token spans. "United States Air Force" wouldn't be a country —
    # but "United States" would consume two tokens and the rest fall
    # through to free text.
    for span_len in (4, 3, 2):
        for i in range(len(tokens) - span_len + 1):
            if any(consumed[i:i + span_len]):
                continue
            phrase = " ".join(tokens[i:i + span_len]).lower()
            if phrase in countries_lower:
                out["filters"].append({
                    "field": "country",
                    "match": "exact",
                    "value": countries_lower[phrase],
                })
                for j in range(i, i + span_len):
                    consumed[j] = True
                break  # restart this span_len scan with one fewer position

    # Second pass: single-token classification on remaining tokens
    for i, tok in enumerate(tokens):
        if consumed[i]:
            continue
        classified = _classify_single_token(tok, countries_lower, date_format,
                                              tz_offset_sec=tz_offset_sec)
        if classified is None:
            out["free_text"].append(tok)
        elif classified["kind"] == "filter":
            out["filters"].append(classified["filter"])
        elif classified["kind"] == "time_range":
            # v2.65.0: was "last time range wins"; now "narrower wins"
            # so combinations like `today hour:14` work regardless of
            # token order. The narrower window has the smaller delta
            # (end - start). Same window → keep current (no-op).
            new_range = classified["range"]
            cur = out["time_range"]
            if cur is None:
                out["time_range"] = new_range
            else:
                cur_delta = cur[1] - cur[0]
                new_delta = new_range[1] - new_range[0]
                if new_delta < cur_delta:
                    out["time_range"] = new_range
        elif classified["kind"] == "ambiguous":
            # For ambiguous tokens (e.g. could be type OR registration),
            # add both filters with an `ambiguous_group` key so the SQL
            # builder OR's them. This is the "same token, multiple
            # meanings" case the design doc calls out.
            for f in classified["filters"]:
                f["ambiguous_group"] = i  # group ID = original token position
                out["filters"].append(f)

    # v2.91.0: merge multiple same-field "in" filters into one. The
    # category tokens (commercial / general_aviation / helicopter / unknown)
    # each emit a 1-element {match: "in", value: [...]} filter; when the
    # user types two or more, the natural intent is OR (return aircraft in
    # any of the named categories), not AND (return aircraft in all named
    # categories simultaneously, which is empty by definition since
    # category is exclusive per row). Walk the filter list once, union
    # values for same-field "in" filters keyed by field, rewrite. The
    # `_filter_clause` IN-clause emits the resulting list.
    #
    # Order of values within the merged filter is the order of first
    # occurrence in the user's query — stable, matches the user's mental
    # ordering. Doesn't affect SQL semantics (IN is set-membership).
    _in_merged: Dict[str, Dict[str, Any]] = {}
    _other_filters: List[Dict[str, Any]] = []
    for f in out["filters"]:
        if f.get("match") == "in":
            field = f["field"]
            if field in _in_merged:
                # Append values that aren't already in the merged set.
                existing = _in_merged[field]["value"]
                for v in f.get("value", []):
                    if v not in existing:
                        existing.append(v)
            else:
                # Copy so subsequent merges don't mutate the caller's data.
                _in_merged[field] = {
                    "field": field, "match": "in",
                    "value": list(f.get("value", [])),
                }
        else:
            _other_filters.append(f)
    out["filters"] = _other_filters + list(_in_merged.values())

    return out


def _classify_single_token(tok: str, countries_lower: Dict[str, str],
                            date_format: str = "MDY",
                            tz_offset_sec: int = 0
                            ) -> Optional[Dict[str, Any]]:
    """Classify a single whitespace-delimited token.

    Args:
        tok: the token to classify
        countries_lower: case-insensitive country lookup map
        date_format: locale for slash-separated dates ("MDY", "DMY", or "ISO")
        tz_offset_sec: v2.66.2 — UTC offset for `today` / `hour:N` window
            computation. Default 0 = server-local time (v2.65.0 behavior).

    Returns:
        None              — fall through to free text
        {kind: 'filter', filter: {...}}
        {kind: 'time_range', range: (start_ts, end_ts)}
        {kind: 'ambiguous', filters: [{...}, {...}]}
    """
    # v2.57.0: boolean filter tokens for special derived attributes
    # that aren't represented as a single column on seen_aircraft.
    # The query "watchlist" should return aircraft on the user's
    # watchlist; "mil" / "military" should return military aircraft.
    # These can't be represented as exact-match column filters because:
    #   - watchlist membership is a JOIN against the watchlist table
    #   - military classification is a derived check (icao prefix +
    #     callsign prefix + special_aircraft list, same logic as
    #     _annotate_military in server.py)
    #
    # The executor handles each via a dedicated WHERE clause path
    # (_build_where below). The chip UI renders the special "boolean"
    # match shape with a per-filter visual treatment (orange for
    # watchlist matching the .wl-label chip elsewhere in the app,
    # red for military matching the MIL pill).
    lower = tok.lower()
    if lower in ("watchlist", "wl"):
        return {"kind": "filter", "filter": {
            "field": "watchlist", "match": "boolean", "value": True
        }}
    if lower in ("mil", "military"):
        return {"kind": "filter", "filter": {
            "field": "military", "match": "boolean", "value": True
        }}

    # v2.91.0: category tokens (commercial / general_aviation / helicopter /
    # unknown). Backed by the seen_aircraft.category column added in v2.89.0
    # and maintained by the collector's per-poll UPSERT (sticky-military
    # rule preserves once-military classification across feeder flicker).
    # Aliases mirror the mil / military convention from v2.50.x: ga →
    # general_aviation, heli → helicopter. The match type is "in" with a
    # 1-element list so multiple category tokens in a query OR together
    # (parse_query has a post-pass that unions same-field "in" filter
    # values). `commercial helicopter` returns aircraft that are either,
    # not aircraft that are both — the latter is empty by definition since
    # category is exclusive per row, and OR is the user's actual intent.
    #
    # `military` / `mil` is intentionally NOT in this list — it stays on
    # the existing live-config-based filter (icao_prefixes / callsign_prefixes
    # / special_aircraft) so config changes apply immediately. The category
    # column's sticky-military rule reflects past determinations, which is
    # what the Stats category_mix card wants but not what an operator
    # tuning the prefix list expects from a Search query.
    _CATEGORY_TOKENS = {
        "commercial":       "commercial",
        "general_aviation": "general_aviation",
        "ga":               "general_aviation",
        "helicopter":       "helicopter",
        "heli":             "helicopter",
        "unknown":          "unknown",
    }
    if lower in _CATEGORY_TOKENS:
        return {"kind": "filter", "filter": {
            "field": "category", "match": "in",
            "value": [_CATEGORY_TOKENS[lower]],
        }}

    # v2.69.0 (Phase 3): first_seen_today filter — match aircraft whose
    # first-ever sighting on this receiver is today. Backs the redirect
    # from the Stats "First time seen today" card, which previously had
    # no usable Search target (Search lacked a way to express the
    # filter; v2.66.0 documented the omission).
    #
    # Semantics: aircraft is included if seen_aircraft.first_seen_at
    # falls within today's local-day window (per stats.timezone). This
    # matches the card's definition exactly — the card lists every
    # ICAO whose seen_aircraft row was first written today.
    #
    # Composes with other filters: `first_seen_today military` returns
    # military aircraft whose first sighting was today; `first_seen_today
    # B738` returns 737-800s whose first sighting was today; etc.
    # Composes with the unrelated `today` token too — `today
    # first_seen_today` AND's `last_seen_at >= today` (from `today`)
    # with `first_seen_at >= today` (from this token), which is a
    # stricter version of the card's filter (still seen today AND
    # first seen today). Both clauses apply.
    #
    # Why a boolean filter rather than a time_range: time_range is
    # column-bound to last_seen_at, and we want first_seen_at. The
    # boolean+precomputed-range shape mirrors how military/watchlist
    # work — the value is a flag, the actual SQL needs additional
    # state which travels alongside on the filter dict (here, the
    # day boundary timestamps so _boolean_filter_clauses doesn't
    # need tz_offset_sec re-injected).
    if lower == "first_seen_today":
        import time as _t
        now = int(_t.time())
        start_ts = ((now + tz_offset_sec) // 86400) * 86400 - tz_offset_sec
        end_ts = start_ts + 86400
        return {"kind": "filter", "filter": {
            "field": "first_seen_today",
            "match": "boolean",
            "value": True,
            "first_seen_range": (start_ts, end_ts),
        }}

    # v2.82.0: peak_today filter — match aircraft that were seen during
    # the today's "peak simultaneous" minute (the 60-second bucket with
    # the most distinct ICAOs). Closes the v2.68.0 misleading-redirect
    # gap: the Stats "Peak simultaneous" card's "View in Search →" used
    # to fall back to `today` + seen_at desc, which showed every
    # aircraft seen today rather than specifically the aircraft at the
    # peak moment.
    #
    # Semantics match the existing Stats card and drill panel exactly:
    # bin all_sightings.seen_at into 60s buckets, find the bucket with
    # the highest COUNT(DISTINCT icao), return aircraft whose sightings
    # fall in that bucket. Today's local-day window per stats.timezone.
    #
    # Two-stage resolution: parser attaches today_range here; the actual
    # peak-bucket lookup runs in execute_search() against the live DB
    # connection (peak data is live-derived, not stored). The resolved
    # ICAO set rides on the filter dict's `peak_icaos` field.
    #
    # Composes with other filters: `peak_today military` returns the
    # military aircraft at today's peak; `peak_today watchlist` returns
    # watchlist aircraft at the peak. AND'd against the existing query
    # — same shape as first_seen_today + military.
    if lower == "peak_today":
        import time as _t
        now = int(_t.time())
        start_ts = ((now + tz_offset_sec) // 86400) * 86400 - tz_offset_sec
        end_ts = start_ts + 86400
        return {"kind": "filter", "filter": {
            "field": "peak_today",
            "match": "boolean",
            "value": True,
            "today_range": (start_ts, end_ts),
            # peak_icaos and peak_at_ts populated by execute_search
            # at query time via _resolve_peak_today_if_present.
        }}

    # v2.65.0 (Phase 2): relative-date `today` token. Resolves at parse
    # time to the local-day window — same semantics as the Stats "Today"
    # date preset.
    # v2.66.2: now timezone-aware via tz_offset_sec parameter — matches
    # the Patterns histogram's tz semantics so chart-drill redirects with
    # `today hour:N` actually return the same aircraft the histogram
    # bucket showed. tz_offset_sec=0 (default) preserves v2.65.0
    # server-local behavior. Compute "today midnight in user-tz" as a
    # unix timestamp by shifting now into user-tz, taking the date
    # boundary, and shifting back: `(now + tz_off) // 86400 * 86400 - tz_off`.
    if lower == "today":
        import time as _t
        now = int(_t.time())
        start_ts = ((now + tz_offset_sec) // 86400) * 86400 - tz_offset_sec
        end_ts = start_ts + 86400
        return {"kind": "time_range", "range": (start_ts, end_ts)}

    # v2.91.0: last:Nd time-window token. N is any positive integer of
    # calendar days; the window starts at local midnight (N-1) days
    # before today's local midnight and ends at "now". Calendar-day
    # semantics match the `today` token's local-midnight alignment, not
    # rolling 24-hour clocks — `last:7d` includes today + 6 prior
    # calendar days, the way users mean "last week."
    #
    #   last:1d  ≡ today (functionally; the narrower-wins rule from
    #              v2.65.0 makes a co-occurring `today` win when present)
    #   last:7d  = approximately the past 168 hours, aligned to local midnight
    #   last:30d = approximately the past 30 calendar days
    #
    # Bounded by data.retention_days at the SQL level — if retention is
    # 7 and the user types `last:30d`, the query naturally returns
    # whatever's in the retention window. No warning surfaced (retention
    # is a per-install setting; warning text would be confusing).
    #
    # N must be ≥ 1. Fractional values (`last:0.5d`), negative values,
    # and non-numeric values fall through to free text — same forgiving-
    # parser convention as the rest of the token table.
    if lower.startswith("last:"):
        rest = tok.split(":", 1)[1].strip().lower()
        if rest.endswith("d") and len(rest) > 1:
            try:
                n = int(rest[:-1])
                if n >= 1:
                    import time as _t
                    now = int(_t.time())
                    today_start = ((now + tz_offset_sec) // 86400) * 86400 - tz_offset_sec
                    start_ts = today_start - (n - 1) * 86400
                    # End at tomorrow's local midnight, matching the
                    # `today` token's half-open [start, end) shape. Using
                    # `now` as end_ts here would exclude any aircraft
                    # whose last_seen_at equals query-time `now` to the
                    # second (the SQL clause is strict `<`); this happens
                    # naturally for sightings landing in the same second
                    # the user submits the query. Tomorrow-midnight as
                    # end_ts means the entire current day is covered
                    # inclusively, with no edge case at "now". Future
                    # rows past `now` don't exist yet, so this doesn't
                    # over-match anything real.
                    end_ts = today_start + 86400
                    return {"kind": "time_range", "range": (start_ts, end_ts)}
            except (ValueError, TypeError):
                pass
        # Anything we can't parse falls through to free text. The parser
        # is forgiving by design — typo-friendly. The user gets a
        # free-text search with "last:foo" as a search term, sees no
        # useful results, and corrects the syntax.

    # v2.65.0 (Phase 2): hour:N filter — single-hour bucket on today's
    # date. N must be integer 0-23. Composes naturally with `today`
    # (same time_range shrunk to one hour). Without `today`, the hour
    # filter still applies to today by default — common case is "show
    # me what flew at 14:00 today" which doesn't need explicit `today`.
    # v2.66.2: timezone-aware via tz_offset_sec — built on top of the
    # same today-midnight-in-user-tz computation above, then shifted by
    # `h * 3600` for the hour-of-day. The Patterns hourly_histogram
    # buckets via `((seen_at + tz_off) / 3600) % 24`, so an aircraft at
    # bucket H matches when seen_at is in `[H_start, H_start + 3600)`
    # where H_start is the unix timestamp of H:00 in user-tz today.
    # v2.70.0 (Phase 3): hour:LO-HI range syntax — inclusive on both
    # ends. `hour:14-16` matches the three-hour window 14:00 through
    # 16:59:59 (i.e. hour buckets 14, 15, AND 16). The window's unix
    # range is [midnight + lo*3600, midnight + (hi+1)*3600). Note the
    # asymmetry with `distance:LO-HI` which is inclusive-exclusive
    # (matches bucket UX); hours are inclusive-inclusive (matches
    # calendar UX — "from hour 14 through hour 16" naturally includes
    # hour 16). Single-hour `hour:14` semantics unchanged. LO must be
    # ≤ HI; LO == HI degenerates to single-hour behavior. Wraparound
    # (`hour:23-1`) rejected — user can run two queries.
    if lower.startswith("hour:"):
        rest = tok.split(":", 1)[1].strip()
        # Range form: "lo-hi"
        if "-" in rest:
            parts = rest.split("-", 1)
            try:
                lo = int(parts[0])
                hi = int(parts[1])
            except ValueError:
                return None  # malformed, fall through to free text
            if not (0 <= lo <= 23 and 0 <= hi <= 23):
                return None  # out of range
            if lo > hi:
                return None  # wraparound rejected
            import time as _t
            now = int(_t.time())
            midnight = ((now + tz_offset_sec) // 86400) * 86400 - tz_offset_sec
            start_ts = midnight + lo * 3600
            end_ts = midnight + (hi + 1) * 3600  # +1 because hi is inclusive
            return {"kind": "time_range", "range": (start_ts, end_ts)}
        # Single-hour form: "n"
        try:
            h = int(rest)
        except ValueError:
            return None  # malformed, fall through to free text
        if not (0 <= h <= 23):
            return None  # out of range, fall through
        import time as _t
        now = int(_t.time())
        midnight = ((now + tz_offset_sec) // 86400) * 86400 - tz_offset_sec
        start_ts = midnight + h * 3600
        end_ts = start_ts + 3600
        return {"kind": "time_range", "range": (start_ts, end_ts)}

    # v2.65.0 (Phase 2): distance:LO-HI filter — bucket range in the
    # user's configured display unit. The parser stores raw bounds; the
    # WHERE clause builder converts to canonical km using the receiver's
    # configured distance_unit (mi/km/nmi). This mirrors how the existing
    # distance display works at render-time.
    #
    # Examples: distance:0-50, distance:100-150, distance:200-250.
    # v2.70.0 (Phase 3): added single-bound `distance:<N` and
    # `distance:>N`. Comparison ops are exclusive (<100 = under 100,
    # not 100; >200 = over 200, not 200). Internally represented as
    # range filters with one bound set to None — `distance:<100`
    # becomes [None, 100], `distance:>200` becomes [200, None]. The
    # WHERE builder skips the SQL clause for whichever bound is None,
    # preserving the existing range path for two-sided ranges.
    if lower.startswith("distance:"):
        rest = tok.split(":", 1)[1].strip()
        # Comparison forms: "<N" or ">N"
        if rest.startswith("<"):
            try:
                hi = float(rest[1:])
            except ValueError:
                return None
            if hi <= 0:
                return None  # nonsensical: distance:<0 or distance:<-5
            return {"kind": "filter", "filter": {
                "field": "distance",
                "match": "range",
                "value": [None, hi],
            }}
        if rest.startswith(">"):
            try:
                lo = float(rest[1:])
            except ValueError:
                return None
            if lo < 0:
                return None  # nonsensical: distance:>-5
            return {"kind": "filter", "filter": {
                "field": "distance",
                "match": "range",
                "value": [lo, None],
            }}
        # Range form: "LO-HI" with non-negative numbers
        if "-" not in rest:
            return None
        parts = rest.split("-", 1)
        try:
            lo = float(parts[0])
            hi = float(parts[1])
        except ValueError:
            return None
        if lo < 0 or hi < 0 or lo >= hi:
            return None
        return {"kind": "filter", "filter": {
            "field": "distance",
            "match": "range",
            "value": [lo, hi],
        }}

    # ICAO hex (6 hex chars)
    if _ICAO_HEX_RE.match(tok):
        return {"kind": "filter", "filter": {
            "field": "icao", "match": "exact", "value": tok.upper()
        }}

    # Emergency squawk
    if _SQUAWK_RE.match(tok):
        # Squawk isn't currently a column on seen_aircraft (we'd need
        # to denormalize last_squawk for it to be a filter). For now
        # this falls through. Filed in design doc; user-visible
        # behavior: typing "7700" matches free text, finds nothing
        # useful. Fix is a Phase 2.5 schema add.
        return None

    # Date patterns
    date_range = _try_parse_date(tok, date_format)
    if date_range:
        return {"kind": "time_range", "range": date_range}

    # Aircraft type code (4-char, looked up in designators)
    upper = tok.upper()
    if upper in AIRCRAFT_TYPES:
        # Aircraft type codes are 4-char alphanumeric. Some collide with
        # registration prefixes (in theory). The design doc says: emit
        # both as OR-able candidates and let ranking sort it out. In
        # practice the aircraft_type filter will dominate since type is
        # a denormalized exact-match column.
        looks_like_reg = bool(_REGISTRATION_RE.match(upper))
        if looks_like_reg:
            return {"kind": "ambiguous", "filters": [
                {"field": "aircraft_type", "match": "exact", "value": upper},
                {"field": "registration", "match": "prefix", "value": upper},
            ]}
        return {"kind": "filter", "filter": {
            "field": "aircraft_type", "match": "exact", "value": upper
        }}

    # Single-token country (most countries are one word: "Canada",
    # "Germany", "France"). Multi-word countries were handled in the
    # caller's first pass.
    if tok.lower() in countries_lower:
        return {"kind": "filter", "filter": {
            "field": "country", "match": "exact", "value": countries_lower[tok.lower()],
        }}

    # Callsign with digits (UAL2024, AAL101) — try BEFORE registration
    # because the callsign pattern is tighter (3 letters then digits)
    # and would otherwise be missed if a stricter token like UAL2024
    # were also considered for some other field. We don't gate this on
    # AIRLINES membership because callsigns with unrecognized airline
    # prefixes still want to be searched against last_callsign.
    m = _CALLSIGN_FULL_RE.match(upper)
    if m:
        return {"kind": "filter", "filter": {
            "field": "last_callsign", "match": "exact", "value": upper
        }}

    # Registration (US tail like N12345, UK like G-XYZA)
    #
    # v2.83.4: emit ambiguous filter against BOTH `registration` AND
    # `last_callsign`. Rationale: US general aviation aircraft transmit
    # their tail number AS the ADS-B callsign, so for a Cessna registered
    # as N969TC, seen_aircraft.last_callsign='N969TC' and
    # seen_aircraft.registration may be empty (when hexdb hasn't resolved
    # the ICAO yet, or when hexdb doesn't have the aircraft at all).
    # Pre-v2.83.4 we matched on registration only — so a user typing
    # 'N969TC' got 0 results even though the aircraft was clearly in the
    # database with N969TC visible as the callsign throughout. Reported
    # by Pi user. Airlines and military aircraft transmit ICAO callsigns
    # (UAL2024, RCH507) that don't match the registration regex, so this
    # widening doesn't introduce false positives — only US-format tails
    # (N-prefix or letter-letter-dash) trigger the OR'd query.
    if _REGISTRATION_RE.match(upper):
        return {"kind": "ambiguous", "filters": [
            {"field": "registration", "match": "exact", "value": upper},
            {"field": "last_callsign", "match": "exact", "value": upper},
        ]}

    # Callsign prefix only (3 letters — only treat as prefix if it's a
    # known airline code, otherwise unknown 3-letter words like "XYZ"
    # would silently filter results to nothing instead of doing a
    # free-text search.
    m = _CALLSIGN_PREFIX_RE.match(upper)
    if m and upper in AIRLINES:
        return {"kind": "filter", "filter": {
            "field": "last_callsign", "match": "prefix", "value": upper
        }}
    # Unknown 3-letter token falls through to free text.

    return None


def _try_parse_date(tok: str, date_format: str = "MDY") -> Optional[Tuple[int, int]]:
    """Parse a date token into a UTC (start_ts, end_ts) range.

    Args:
        tok: the token to parse
        date_format: locale for slash-separated dates. "MDY" (US), "DMY"
                     (European), or "ISO" (rejects slash dates).

    Supports:
      ISO formats (always accepted regardless of date_format):
        2026-04-29 → that day, 00:00 to 23:59:59
        2026-04    → that month
        2026       → that year (only if it looks like a year, not a 4-digit number)

      Slash formats (accepted only when date_format is "MDY" or "DMY"):
        4/29/26    → MDY: April 29, 2026; DMY: rejected (no month 29)
        29/4/26    → DMY: April 29, 2026; MDY: rejected (no month 29)
        4/29/2026  → 4-digit year variant
        Per POSIX strptime convention, 2-digit years 00-69 → 2000-2069
        and 70-99 → 1970-1999.

    Returns None if the token isn't a recognizable date.
    """
    m = _DATE_FULL_RE.match(tok)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            t0 = _utc_timestamp(y, mo, d)
            t1 = t0 + 86400
            return (t0, t1)
        except Exception:
            return None

    m = _DATE_MONTH_RE.match(tok)
    if m:
        try:
            y, mo = int(m.group(1)), int(m.group(2))
            t0 = _utc_timestamp(y, mo, 1)
            # Add a calendar month — easiest is to compute next month's
            # start. Handle December rollover.
            if mo == 12:
                t1 = _utc_timestamp(y + 1, 1, 1)
            else:
                t1 = _utc_timestamp(y, mo + 1, 1)
            return (t0, t1)
        except Exception:
            return None

    m = _DATE_YEAR_RE.match(tok)
    if m:
        # Disambiguate: a 4-digit number is a year only if it's
        # plausibly a recent or near-future year. 1700, 2200, 9999
        # shouldn't classify as a date.
        try:
            y = int(m.group(1))
            if 2000 <= y <= 2099:
                t0 = _utc_timestamp(y, 1, 1)
                t1 = _utc_timestamp(y + 1, 1, 1)
                return (t0, t1)
        except Exception:
            pass

    # v2.52.0: locale-specific slash-separated dates. ISO mode rejects
    # these entirely (forces unambiguous input). MDY/DMY interpret the
    # first two numeric parts according to the locale convention.
    if date_format in ("MDY", "DMY"):
        m = _DATE_SLASH_RE.match(tok)
        if m:
            a, b, y_str = int(m.group(1)), int(m.group(2)), m.group(3)
            # 2-digit year resolution per POSIX strptime
            if len(y_str) == 2:
                y_int = int(y_str)
                y = (2000 + y_int) if y_int < 70 else (1900 + y_int)
            else:
                y = int(y_str)
            # Apply locale: MDY → (mo, d); DMY → (d, mo)
            if date_format == "MDY":
                mo, d = a, b
            else:  # DMY
                d, mo = a, b
            # Validate calendar bounds — reject "13/45/26" etc. so the
            # token falls through to free-text instead of producing
            # garbage. month 1-12, day 1-31 (full validation happens
            # inside _utc_timestamp via calendar.timegm rejecting bad
            # combinations like Feb 30).
            if not (1 <= mo <= 12 and 1 <= d <= 31):
                return None
            try:
                t0 = _utc_timestamp(y, mo, d)
                t1 = t0 + 86400
                return (t0, t1)
            except Exception:
                return None

    return None


def _utc_timestamp(y: int, mo: int, d: int) -> int:
    """Convert a UTC calendar date to a Unix timestamp (start of day)."""
    import calendar
    return calendar.timegm((y, mo, d, 0, 0, 0, 0, 0, 0))


# ---------------------------------------------------------------------------
# v2.82.0: peak_today filter resolver
# ---------------------------------------------------------------------------

def _resolve_peak_today_if_present(conn: sqlite3.Connection,
                                    parsed: Dict[str, Any]
                                    ) -> Dict[str, Any]:
    """Find today's peak 60-second bucket and the ICAOs seen during it.

    Mutates the peak_today filter dict IN PLACE so its `peak_icaos`
    (list[str]) and `peak_at_ts` (unix seconds at bucket start) become
    visible to the caller as well as to _boolean_filter_clauses. The
    caller-visible fields back the chip rendering on the frontend
    ("Peak today (15 aircraft at 14:32)").

    Semantics match the existing Stats card and drill panel exactly:
    bin all_sightings.seen_at into 60s buckets via (seen_at / 60),
    pick the bucket with the highest COUNT(DISTINCT icao), and return
    aircraft seen in that bucket. Tied buckets resolve to the
    earliest one — bucket_min ASC tiebreaker — so the search filter
    and the drill panel always agree on which moment to highlight.

    No-op when no peak_today filter is in parsed["filters"], or when
    the filter is already resolved (peak_icaos already populated).
    Returns the same `parsed` dict (in-place mutation; return is for
    chaining convenience).
    """
    pf = next(
        (f for f in parsed.get("filters", [])
         if f.get("field") == "peak_today" and f.get("match") == "boolean"),
        None,
    )
    if pf is None:
        return parsed
    if pf.get("peak_icaos") is not None:
        # Already resolved (e.g. test-injected). Idempotent.
        return parsed
    rng = pf.get("today_range")
    if not rng:
        # Parser didn't attach a range — shouldn't happen but defend.
        return parsed
    t0, t1 = rng

    # Stage 1: find the peak 60s bucket. Same query as server.py:4194.
    # Tiebreaker bucket_min ASC: when multiple buckets tie for max count,
    # pick the earliest. Deterministic + matches the drill panel after
    # the v2.82.0 server.py update.
    bucket_row = conn.execute(
        "SELECT (seen_at / 60) AS bucket_min, "
        "       COUNT(DISTINCT icao) AS n "
        "FROM all_sightings "
        "WHERE seen_at >= ? AND seen_at < ? "
        "GROUP BY bucket_min "
        "ORDER BY n DESC, bucket_min ASC "
        "LIMIT 1",
        (t0, t1),
    ).fetchone()

    if not bucket_row:
        # No sightings today — empty ICAO list yields 1=0 in the SQL,
        # producing an honest empty result rather than every aircraft.
        pf["peak_icaos"] = []
        pf["peak_at_ts"] = None
        pf["peak_count"] = 0
        return parsed

    peak_min = int(bucket_row["bucket_min"])
    peak_start = peak_min * 60
    peak_end = peak_start + 60
    peak_count = int(bucket_row["n"] or 0)

    # Stage 2: collect distinct ICAOs in the peak bucket. Bounded by
    # 60 seconds of sightings — typically 10-100 rows. The DISTINCT
    # is over a tiny set; cost is dominated by the index scan above.
    icao_rows = conn.execute(
        "SELECT DISTINCT icao FROM all_sightings "
        "WHERE seen_at >= ? AND seen_at < ?",
        (peak_start, peak_end),
    ).fetchall()
    peak_icaos = [r["icao"] for r in icao_rows]

    pf["peak_icaos"] = peak_icaos
    pf["peak_at_ts"] = peak_start
    pf["peak_count"] = peak_count
    return parsed


# ---------------------------------------------------------------------------
# Search executor
# ---------------------------------------------------------------------------

def execute_search(conn: sqlite3.Connection, parsed: Dict[str, Any],
                    limit: int = 50, offset: int = 0,
                    mil_config: Optional[Dict[str, Any]] = None,
                    watchlist_config: Optional[List[Dict[str, Any]]] = None,
                    resolved_tails: Optional[Dict[str, str]] = None,
                    order: Optional[str] = None,
                    direction: Optional[str] = None,
                    from_ts: Optional[int] = None,
                    to_ts: Optional[int] = None,
                    distance_unit: str = "mi",
                    ) -> Dict[str, Any]:
    """Run a parsed search query against the DB.

    Args:
      conn: open sqlite3 connection
      parsed: parser output from parse_query()
      limit: page size cap (caller typically clamps to <= 500)
      offset: page offset
      mil_config: v2.57.0 — military configuration dict
                  (icao_prefixes, callsign_prefixes, special_aircraft).
                  Required when the query includes a "mil" / "military"
                  filter token; ignored otherwise. None falls back to
                  treating military filters as empty result.
      watchlist_config: v2.57.0 — list of watchlist entry dicts from
                  CONFIG['watchlist']. Required when the query includes
                  a "watchlist" / "wl" filter token; ignored otherwise.
      resolved_tails: v2.57.1 — map of {tail_upper: icao_upper} for
                  tail-only watchlist entries that the server resolved
                  at startup via hexdb_cache. Optional; when omitted,
                  tail-only entries don't participate in watchlist
                  filtering (v2.57.0 behavior preserved for tests).
      order: v2.60.0 — optional column to sort by. Must be one of
                  SORTABLE_COLUMNS keys; otherwise falls back to
                  relevance order. None / unrecognized → relevance.
      direction: v2.60.0 — 'asc' or 'desc'. None / unrecognized → 'desc'
                  for numeric-style columns, 'asc' for text columns
                  (matches the per-column sensible-default in
                  _SORT_DEFAULT_DIR).
      from_ts: v2.62.0 (Phase 1E) — explicit start of date-range filter
                  in unix seconds. When set (with or without to_ts),
                  OVERRIDES any time_range that the parser extracted
                  from the query string. This implements the "preset
                  wins, typed dates ignored" UX: the user clicked a
                  preset, that's the explicit choice; whatever's in
                  the search box is supplementary text matching only.
      to_ts: v2.62.0 — explicit end of date-range filter, unix seconds.
                  Half-open interval [from_ts, to_ts) for consistency
                  with the parser-extracted ranges. Either bound can
                  be None — the query becomes open-ended in that
                  direction.

    Returns a dict with:
      total_count:  number of matching rows (without limit/offset)
      rows:         list of result dicts in score order
      execution_ms: how long the query took, for telemetry

    Empty parsed query (no filters, no free text) returns a "browse"
    result: every aircraft, ordered by last_seen_at DESC. This is the
    natural default for a user who navigated to the search page
    without typing anything yet — show them what's been seen recently.
    """
    t0 = time.time()

    # v2.62.0 (Phase 1E): URL-supplied date range overrides parser-
    # extracted time_range. The override happens in-place on a shallow
    # copy of parsed so _build_where sees the effective range without
    # caring about origin. This keeps _build_where unchanged and
    # makes the "preset wins" rule explicit at one location.
    if from_ts is not None or to_ts is not None:
        parsed = dict(parsed)
        # Use a sentinel high/low when only one bound supplied, so the
        # half-open [t0, t1) clause stays valid SQL. _build_where checks
        # truthiness of the tuple, so use 0 for unbounded-from and a
        # far-future timestamp for unbounded-to.
        effective_from = from_ts if from_ts is not None else 0
        effective_to = to_ts if to_ts is not None else 2 ** 31  # ~2038
        parsed["time_range"] = (effective_from, effective_to)

    # v2.82.0: resolve peak_today filter — find today's peak 60s bucket
    # and the ICAOs seen during it, then inject them into the filter
    # dict so _build_where can emit the IN-clause without DB I/O.
    # Keeps _build_where pure (no conn parameter) and matches the
    # caller-side enrichment pattern used for from_ts/to_ts above.
    parsed = _resolve_peak_today_if_present(conn, parsed)

    where_clauses, params = _build_where(
        parsed, mil_config=mil_config, watchlist_config=watchlist_config,
        resolved_tails=resolved_tails, distance_unit=distance_unit,
    )

    # Build the score expression. The components are added together
    # in SQL so ORDER BY can sort by the result. Coefficients match
    # the design doc.
    score_parts = [
        # Exact-match indicator: 1000 if any exact filter hit (built from filter list)
        _build_exact_match_score(parsed),
        # Sighting frequency: capped at 100
        "MIN(COALESCE(seen_aircraft.sighting_count, 0), 100)",
        # Recency boost: 50 if last_seen within 30 days, else 0
        f"CASE WHEN seen_aircraft.last_seen_at IS NOT NULL AND seen_aircraft.last_seen_at >= {int(time.time()) - 30*86400} THEN 50 ELSE 0 END",
    ]
    score_expr = " + ".join(score_parts)

    # FTS5 BM25 contribution. SQLite's bm25() returns NEGATIVE scores
    # (lower is better), so we negate and scale to fit the score range.
    # Only added when the query has free text.
    bm25_join = ""
    bm25_score = "0"
    if parsed["free_text"]:
        bm25_join = " JOIN seen_aircraft_fts f ON f.rowid = seen_aircraft.rowid"
        # Scale: bm25 typically returns -10 to -0.1; multiplying by -20
        # puts that in the 2-200 range, comparable to other components.
        bm25_score = "MIN(MAX(-bm25(seen_aircraft_fts) * 20, 0), 200)"
        score_expr = f"({score_expr} + {bm25_score})"

    # v2.56.0: extended SELECT to include last-state fields (speed,
    # altitude, squawk) so Search cards can render the same data
    # density as All-tab rows. Previously only the metadata fields
    # (icao, callsign, type, country, etc.) were surfaced; speed/
    # altitude/squawk were missing, forcing Search to render a
    # sparse card. With these fields available, the Search card
    # becomes a viable lookup surface — same per-card semantics as
    # All-tab rows: "latest known state of this aircraft."
    #
    # last_speed/last_altitude/last_squawk live on sightings_hourly,
    # not seen_aircraft (they're not denormalized to seen_aircraft
    # because they change continuously and would invalidate FTS more
    # often than necessary). We JOIN to sightings_hourly's latest
    # bucket per icao via a correlated subquery — same pattern that
    # the v2.40.1 All-tab page query introduced (since-removed in
    # Phase 1D; Search inherited the join shape). The subquery is
    # bounded by the outer LIMIT (max 500) so the join cost is small.
    #
    # Distance is NOT computed in SQL — it's a haversine of (last_lat,
    # last_lon) against the receiver's location, computed at result-
    # construction time after the query returns. last_lat / last_lon
    # are already in the SELECT, so distance is essentially free.
    # v2.60.1: last_distance is a stored column (canonical km) so
    # sort-by-distance can ORDER BY the full result set; column is
    # added to SELECT here for both the row dict and the ORDER BY.
    select_cols = """
        seen_aircraft.icao, seen_aircraft.registration, seen_aircraft.last_callsign,
        seen_aircraft.aircraft_type, seen_aircraft.aircraft_type_desc,
        seen_aircraft.operator, seen_aircraft.country,
        seen_aircraft.last_lat, seen_aircraft.last_lon,
        seen_aircraft.last_seen_at, seen_aircraft.sighting_count,
        seen_aircraft.first_seen_at,
        latest_h.last_speed, latest_h.last_altitude, latest_h.last_squawk,
        seen_aircraft.last_distance, seen_aircraft.best_track_seconds
    """

    # Correlated subquery: per-icao, the most recent hour_bucket's
    # last-state. LEFT JOIN so aircraft with no sightings_hourly rows
    # (rare — typically only fresh installs before the first rollup
    # cycle completes) still appear in results, just with NULL last-
    # state fields.
    latest_h_join = """
        LEFT JOIN sightings_hourly latest_h ON latest_h.icao = seen_aircraft.icao
            AND latest_h.hour_bucket = (
                SELECT MAX(hour_bucket) FROM sightings_hourly
                WHERE icao = seen_aircraft.icao
            )
    """

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    # v2.60.0 / v2.60.1: resolve user-specified sort. The column-id is
    # mapped to a hardcoded SQL fragment via SORTABLE_COLUMNS; user
    # input never goes into the SQL string itself.
    #
    # NULL handling: text columns and stored numerics can be NULL
    # (e.g. seen_aircraft.last_distance is NULL until the collector
    # populates it OR the user has set their receiver location). For
    # ALL user-chosen sorts, prefix the ORDER BY with `<col> IS NULL`
    # so NULL rows always sort last regardless of asc/desc — this
    # matches user expectation across every column type ("the
    # closest aircraft" shouldn't include rows with no known location;
    # "alphabetical by callsign" shouldn't lead with empty callsigns).
    sort_sql_expr, sort_dir = _resolve_order_direction(order, direction)
    if sort_sql_expr:
        # User-chosen sort. Score still in tie-break position so equally-
        # ranked rows on the chosen column maintain a stable order.
        order_by = (
            f"{sort_sql_expr} IS NULL, {sort_sql_expr} {sort_dir}, "
            f"score DESC, seen_aircraft.last_seen_at DESC"
        )
    else:
        # Default: relevance order (score DESC) — same as pre-v2.60.0.
        order_by = "score DESC, seen_aircraft.last_seen_at DESC"

    sql = f"""
        SELECT {select_cols}, ({score_expr}) AS score
        FROM seen_aircraft{bm25_join}
        {latest_h_join}
        WHERE {where_sql}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """
    count_sql = f"""
        SELECT COUNT(*) FROM seen_aircraft{bm25_join}
        WHERE {where_sql}
    """

    try:
        total_count = conn.execute(count_sql, params).fetchone()[0]
        cur = conn.execute(sql, params + [limit, offset])
        rows = []
        for r in cur.fetchall():
            rows.append({
                "icao": r[0],
                "registration": r[1],
                "last_callsign": r[2],
                # v2.56.0: also expose under "callsign" key so server-side
                # _annotate_military and other helpers that look for the
                # generic key work without needing the search-specific
                # last_callsign rename. Keeping both keys for back-compat
                # with anything that already reads last_callsign.
                "callsign": r[2],
                "aircraft_type": r[3],
                "aircraft_type_desc": r[4],
                "operator": r[5],
                "country": r[6],
                "last_lat": r[7],
                "last_lon": r[8],
                "last_seen_at": r[9],
                "sighting_count": r[10] or 0,
                "first_seen_at": r[11],
                # v2.56.0: last-state fields for Search card column parity
                "last_speed": r[12],
                "last_altitude": r[13],
                "last_squawk": r[14],
                # v2.60.1: stored last_distance in canonical km. server.py
                # converts to user-unit at response-annotation time.
                "last_distance_km": r[15],
                # v3.4.62: all-time longest single continuous track, in
                # seconds (NULL if no session has been tracked). Replaces
                # the client-side last_seen_at − first_seen_at computation
                # that used to back the Search "Track length" column.
                "track_length_sec": r[16],
                "score": r[17],
            })
        return {
            "total_count": total_count,
            "rows": rows,
            "execution_ms": round((time.time() - t0) * 1000, 2),
        }
    except sqlite3.OperationalError as e:
        # Most likely cause: FTS5 syntax error from a hostile query
        # like 'foo "bar' (unbalanced quote). Don't crash the endpoint.
        logger.warning(f"Search query failed: {e}")
        return {
            "total_count": 0,
            "rows": [],
            "execution_ms": round((time.time() - t0) * 1000, 2),
            "error": str(e),
        }


def _build_where(parsed: Dict[str, Any],
                 mil_config: Optional[Dict[str, Any]] = None,
                 watchlist_config: Optional[List[Dict[str, Any]]] = None,
                 resolved_tails: Optional[Dict[str, str]] = None,
                 distance_unit: str = "mi"
                 ) -> Tuple[List[str], List[Any]]:
    """Translate parsed filters + free text + time range into WHERE clauses.

    Args:
      parsed: parser output from parse_query()
      mil_config: optional military configuration dict (icao_prefixes,
                  callsign_prefixes, special_aircraft) used to expand
                  the "military: yes" boolean filter into a SQL clause.
                  When None, military filters degrade to "1=0" — the
                  user gets an honest empty result rather than every
                  aircraft.
      watchlist_config: optional list of watchlist entry dicts (from
                  CONFIG['watchlist']). Used to expand the "watchlist"
                  boolean filter. Same degradation: None or empty list
                  → "1=0".
      resolved_tails: v2.57.1 — optional map of {tail_upper: icao_upper}
                  for tail-only watchlist entries that the server
                  resolved at startup via hexdb_cache. Threaded through
                  to _watchlist_match_clause so tail-only entries
                  participate in watchlist filtering.
      distance_unit: v2.65.0 — receiver's configured display unit
                  ("mi", "km", or "nmi"). Used to convert distance
                  filter bounds (which the user types in display unit)
                  to canonical km for the seen_aircraft.last_distance
                  column comparison.

    Returns (clauses, params). Clauses are AND-ed at the SQL site by
    the caller. Ambiguous-group filters are OR-ed with each other,
    then AND-ed across groups.

    v2.57.0: boolean filters (military, watchlist) get their own
    handlers because they don't fit the column-equality model. See
    _boolean_filter_clauses below.
    """
    clauses: List[str] = []
    params: List[Any] = []

    # v2.57.0: boolean filters first — they don't go through the
    # column-equality grouping below. Each generates its own clause
    # (or sub-query) without participating in ambiguous-group logic.
    boolean_clauses, boolean_params = _boolean_filter_clauses(
        parsed, mil_config, watchlist_config, resolved_tails=resolved_tails
    )
    clauses.extend(boolean_clauses)
    params.extend(boolean_params)

    # v2.65.0: convert distance bounds from display unit to canonical km
    # for the last_distance column comparison. Multiplier is the inverse
    # of the display conversion in server._distance_km_to_user_unit.
    if (distance_unit or "mi").lower() == "nmi":
        to_km = 1.0 / 0.539957
    elif (distance_unit or "mi").lower() == "km":
        to_km = 1.0
    else:  # mi (default)
        to_km = 1.0 / 0.621371

    # Group COLUMN filters by ambiguous_group ID (filters not in a
    # group get standalone treatment). Boolean filters are filtered
    # out here since they were handled above.
    # v2.65.0: distance range filters also handled separately — their
    # value is a [lo, hi] list rather than a scalar, and the SQL
    # involves unit conversion.
    # v2.70.0: distance range value can have None on either bound for
    # `distance:<N` (lo=None) and `distance:>N` (hi=None) comparison
    # forms. Skip the corresponding SQL clause when a bound is None.
    grouped: Dict[Optional[int], List[Dict[str, Any]]] = {}
    for f in parsed["filters"]:
        if f.get("match") == "boolean":
            continue
        if f.get("field") == "distance" and f.get("match") == "range":
            lo, hi = f["value"]
            sub_clauses = ["seen_aircraft.last_distance IS NOT NULL"]
            if lo is not None:
                sub_clauses.append("seen_aircraft.last_distance > ?"
                                    if hi is None
                                    else "seen_aircraft.last_distance >= ?")
                params.append(lo * to_km)
            if hi is not None:
                # For two-sided ranges, hi is exclusive (matches
                # bucket convention); for `distance:<N`, hi is also
                # exclusive (matches < operator). Same SQL either way.
                sub_clauses.append("seen_aircraft.last_distance < ?")
                params.append(hi * to_km)
            clauses.append(" AND ".join(sub_clauses))
            continue
        gid = f.get("ambiguous_group")
        grouped.setdefault(gid, []).append(f)

    for gid, filters in grouped.items():
        if gid is None:
            # Standalone filters — each gets its own clause
            for f in filters:
                c, p = _filter_clause(f)
                clauses.append(c)
                params.extend(p)
        else:
            # Ambiguous group — OR within
            sub_clauses = []
            for f in filters:
                c, p = _filter_clause(f)
                sub_clauses.append(c)
                params.extend(p)
            clauses.append("(" + " OR ".join(sub_clauses) + ")")

    if parsed["time_range"]:
        t0, t1 = parsed["time_range"]
        clauses.append("seen_aircraft.last_seen_at >= ? AND seen_aircraft.last_seen_at < ?")
        params.extend([t0, t1])

    if parsed["free_text"]:
        # Combine free-text terms into a single FTS5 query string. Default
        # FTS5 syntax AND's terms together. Quote each term to defend
        # against FTS5 syntax injection from the user — without quotes,
        # a token like 'NEAR("foo")' would be interpreted as an FTS5
        # operator. Quoting makes them all literal-string matches.
        fts_query = " ".join(_fts_quote(t) for t in parsed["free_text"])
        clauses.append("seen_aircraft_fts MATCH ?")
        params.append(fts_query)

    return clauses, params


def _boolean_filter_clauses(parsed: Dict[str, Any],
                             mil_config: Optional[Dict[str, Any]],
                             watchlist_config: Optional[List[Dict[str, Any]]],
                             resolved_tails: Optional[Dict[str, str]] = None
                             ) -> Tuple[List[str], List[Any]]:
    """v2.57.0: build SQL clauses for boolean filters (military, watchlist).

    Both filters are derived attributes that don't map to a single
    column on seen_aircraft. They translate to dedicated SQL patterns:

    military: an OR-combination of icao prefix LIKE matches, callsign
              prefix LIKE matches, and an IN-list of special_aircraft
              icaos. Reads the same configuration that
              _annotate_military uses, so search results are
              consistent with how rows render the MIL pill elsewhere.
              When the install has no military config (or empty
              prefix lists), the clause becomes "1=0" so the user
              sees an honest empty result rather than every aircraft.

    watchlist: an OR-combination of icao IN-list (including
              tail-resolved ICAOs), callsign prefix LIKE matches,
              and model substring matches against aircraft_type /
              aircraft_type_desc. v2.57.1 fixed the tail-only entry
              gap by accepting a resolved_tails map (built at
              server startup from hexdb_cache reverse-lookup) and
              folding resolved ICAOs into the same IN-list as
              direct ICAO entries.

    Returns (clauses, params). Both lists may be empty if no
    boolean filter is present.
    """
    clauses: List[str] = []
    params: List[Any] = []

    has_military = any(
        f.get("field") == "military" and f.get("match") == "boolean"
        for f in parsed["filters"]
    )
    has_watchlist = any(
        f.get("field") == "watchlist" and f.get("match") == "boolean"
        for f in parsed["filters"]
    )
    # v2.69.0 (Phase 3): first_seen_today filter. The day-boundary
    # timestamps were precomputed at parse time and ride on the filter
    # dict's `first_seen_range` field — no tz_offset_sec needed here.
    # Find the filter dict (if present) so we can read its precomputed
    # range; if the user happens to type the token twice, we use the
    # first occurrence (same range either way, so it doesn't matter).
    first_seen_today_filter = next(
        (f for f in parsed["filters"]
         if f.get("field") == "first_seen_today"
         and f.get("match") == "boolean"),
        None,
    )

    # v2.82.0: peak_today filter. The peak bucket and ICAO set were
    # resolved by execute_search before _build_where via
    # _resolve_peak_today_if_present, so the filter dict here carries
    # peak_icaos (list[str], possibly empty). Same first-occurrence
    # rule as first_seen_today; same range either way.
    peak_today_filter = next(
        (f for f in parsed["filters"]
         if f.get("field") == "peak_today"
         and f.get("match") == "boolean"),
        None,
    )

    if has_military:
        mil_clause, mil_params = _military_match_clause(mil_config)
        clauses.append(mil_clause)
        params.extend(mil_params)

    if has_watchlist:
        wl_clause, wl_params = _watchlist_match_clause(
            watchlist_config, resolved_tails=resolved_tails
        )
        clauses.append(wl_clause)
        params.extend(wl_params)

    if first_seen_today_filter is not None:
        rng = first_seen_today_filter.get("first_seen_range")
        if rng:
            t0, t1 = rng
            clauses.append(
                "seen_aircraft.first_seen_at >= ? "
                "AND seen_aircraft.first_seen_at < ?"
            )
            params.extend([t0, t1])

    # v2.82.0: peak_today emits an IN-clause from the resolved ICAO set.
    # Empty list (no sightings today, or no peak bucket) emits "1=0" so
    # the user sees an honest empty result rather than every aircraft —
    # same degradation pattern as military/watchlist with empty config.
    # peak_icaos is None means execute_search wasn't called (e.g. unit
    # test of the parser alone) — defend by treating like empty.
    if peak_today_filter is not None:
        peak_icaos = peak_today_filter.get("peak_icaos")
        if not peak_icaos:
            clauses.append("1=0")
        else:
            placeholders = ",".join("?" for _ in peak_icaos)
            clauses.append(f"seen_aircraft.icao IN ({placeholders})")
            params.extend(peak_icaos)

    return clauses, params


def _watchlist_match_clause(watchlist_config: Optional[List[Dict[str, Any]]],
                             resolved_tails: Optional[Dict[str, str]] = None
                             ) -> Tuple[str, List[Any]]:
    """Build the SQL clause that matches watchlist aircraft.

    Mirrors collector.match_watchlist's logic across all four entry
    kinds: icao, tail, callsign, model.

    v2.57.1: tail entries now match via the resolved_tails map. The
    server resolves tail-only entries to ICAOs at startup by reverse-
    querying hexdb_cache (no network calls); the resolved map is
    threaded into search via this parameter. Tails not in the map
    (aircraft never seen by this install) are silently skipped — the
    startup resolution path logs the warning, so we don't double-log
    here on every search request.

    Args:
      watchlist_config: list of dicts as stored in CONFIG['watchlist'].
                        Each entry has one of: icao, tail, callsign,
                        model.
      resolved_tails: optional map of {tail_upper: icao_upper} for
                      tail-only entries. When None, tail entries are
                      skipped (v2.57.0 behavior preserved for
                      callers that don't pass the map — e.g. tests).

    Returns (clause, params). Empty config or zero translatable
    entries → ('1=0', []) so the user sees an honest empty result.
    """
    if not watchlist_config:
        return ("1=0", [])
    resolved_tails = resolved_tails or {}

    parts: List[str] = []
    params: List[Any] = []

    icaos: List[str] = []
    callsign_prefixes: List[str] = []
    model_substrings: List[str] = []

    for entry in watchlist_config:
        if not isinstance(entry, dict):
            continue
        if entry.get("icao"):
            icaos.append(str(entry["icao"]).strip().upper())
        elif entry.get("tail"):
            # v2.57.1: tail entries resolve to ICAOs via the cache.
            # Falls into the same icaos list as direct ICAO entries
            # so they share the IN-clause. Unresolved tails (not in
            # the map) silently skip — startup logged the warning.
            tail = str(entry["tail"]).strip().upper()
            resolved_icao = resolved_tails.get(tail)
            if resolved_icao:
                icaos.append(resolved_icao)
        elif entry.get("callsign"):
            callsign_prefixes.append(str(entry["callsign"]).strip().upper())
        elif entry.get("model"):
            sub = str(entry["model"]).strip().lower()
            if sub:
                model_substrings.append(sub)

    if icaos:
        placeholders = ",".join("?" * len(icaos))
        parts.append(f"seen_aircraft.icao IN ({placeholders})")
        params.extend(icaos)

    for p in callsign_prefixes:
        parts.append("UPPER(seen_aircraft.last_callsign) LIKE ?")
        params.append(p + "%")

    for sub in model_substrings:
        # Match against both the type code (e.g. SF50) and the
        # type description (e.g. "Cirrus SF50 Vision"). LIKE is
        # case-insensitive in SQLite by default for ASCII; the
        # substring is already lowercased. We additionally LOWER()
        # the column to be safe with mixed-case data.
        parts.append("(LOWER(seen_aircraft.aircraft_type) LIKE ? "
                     "OR LOWER(seen_aircraft.aircraft_type_desc) LIKE ?)")
        params.append(f"%{sub}%")
        params.append(f"%{sub}%")

    if not parts:
        return ("1=0", [])

    return ("(" + " OR ".join(parts) + ")", params)


def _military_match_clause(mil_config: Optional[Dict[str, Any]]) -> Tuple[str, List[Any]]:
    """Build the SQL clause that matches military aircraft.

    Mirrors the logic in server.py's _annotate_military:
      1. Special-aircraft list: icao IN (configured set)
      2. Military icao prefixes: icao LIKE 'PREFIX%' OR ...
      3. Military callsign prefixes: last_callsign LIKE 'PREFIX%' OR ...

    All three OR'd together. Empty config or empty lists → '1=0' so
    the user sees no results rather than every result.
    """
    if not mil_config:
        return ("1=0", [])

    parts: List[str] = []
    params: List[Any] = []

    # 1) Special-aircraft list
    specials = mil_config.get("special_aircraft") or {}
    if specials:
        keys = [k.upper() for k in specials.keys() if k]
        if keys:
            placeholders = ",".join("?" * len(keys))
            parts.append(f"seen_aircraft.icao IN ({placeholders})")
            params.extend(keys)

    # 2) ICAO prefixes
    icao_prefixes = [p.upper() for p in (mil_config.get("icao_prefixes") or []) if p]
    for p in icao_prefixes:
        parts.append("seen_aircraft.icao LIKE ?")
        params.append(p + "%")

    # 3) Callsign prefixes — match against last_callsign
    callsign_prefixes = [p.upper() for p in (mil_config.get("callsign_prefixes") or []) if p]
    for p in callsign_prefixes:
        parts.append("UPPER(seen_aircraft.last_callsign) LIKE ?")
        params.append(p + "%")

    if not parts:
        return ("1=0", [])

    return ("(" + " OR ".join(parts) + ")", params)


def _filter_clause(f: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """Build a SQL clause for a single filter. Returns (clause, params).

    Column references are qualified with `seen_aircraft.` because when
    a free-text query is present, the SQL also joins seen_aircraft_fts,
    and several columns (registration, last_callsign, aircraft_type,
    country, etc.) exist in both tables. Without the qualifier SQLite
    raises 'ambiguous column name'. Qualifying everything is harmless
    when there's no join.
    """
    field = f["field"]
    match = f["match"]
    value = f["value"]
    qualified = f"seen_aircraft.{field}"
    if match == "exact":
        return (f"{qualified} = ?", [value])
    elif match == "prefix":
        return (f"{qualified} LIKE ?", [value + "%"])
    elif match == "in":
        # v2.91.0: IN-clause for multi-value filters. Used by the category
        # tokens — multiple categories in one query OR together via this
        # clause. Empty value list defends against degenerate input by
        # returning a clause that matches nothing rather than emitting
        # "IN ()" which is a SQL syntax error in SQLite.
        if not value:
            return ("1=0", [])
        placeholders = ",".join("?" * len(value))
        return (f"{qualified} IN ({placeholders})", list(value))
    else:
        raise ValueError(f"unknown match type: {match}")


def _build_exact_match_score(parsed: Dict[str, Any]) -> str:
    """Score component: 1000 if any exact filter is present, else 0.

    A query that has any exact match filter should rank exact-match
    results above pure free-text matches. Computed as a constant
    expression because it's the same for every row in this query
    (every row that survives WHERE matches the filters).
    """
    has_exact = any(
        f.get("match") == "exact"
        for f in parsed["filters"]
    )
    return "1000" if has_exact else "0"


def _fts_quote(s: str) -> str:
    """Quote a free-text term for safe inclusion in an FTS5 MATCH query.

    FTS5 has its own mini-language (NEAR, AND, OR, parens, column
    filters). To prevent user input from being interpreted as syntax,
    we wrap each term in double-quotes and escape any double-quote
    inside by doubling it. This makes the term a literal string match.
    """
    escaped = s.replace('"', '""')
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# Per-aircraft detail
# ---------------------------------------------------------------------------

def detail_for_aircraft(conn: sqlite3.Connection, icao: str) -> Optional[Dict[str, Any]]:
    """Return per-aircraft detail for the /api/search/aircraft/{icao} endpoint.

    Returns None if the ICAO isn't in seen_aircraft. Otherwise returns
    a dict with the aircraft's denormalized fields plus a sighting
    history summary from sightings_hourly.
    """
    icao = icao.upper()
    row = conn.execute("""
        SELECT icao, first_seen_at, first_callsign, first_aircraft_type,
               registration, last_callsign, aircraft_type, aircraft_type_desc,
               operator, country, last_lat, last_lon, last_seen_at, sighting_count,
               registered_owner, manufacturer
        FROM seen_aircraft WHERE icao = ?
    """, (icao,)).fetchone()
    if row is None:
        return None

    out = {
        "icao": row[0], "first_seen_at": row[1],
        "first_callsign": row[2], "first_aircraft_type": row[3],
        "registration": row[4], "last_callsign": row[5],
        "aircraft_type": row[6], "aircraft_type_desc": row[7],
        "operator": row[8], "country": row[9],
        "last_lat": row[10], "last_lon": row[11],
        "last_seen_at": row[12], "sighting_count": row[13] or 0,
        # v3.4.58: registry enrichment — registered owner (e.g.
        # "DISTRIBUTORS DEVELOPMENT INC") and manufacturer, populated
        # lazily by the hexdb forward resolver. Empty string when not
        # yet resolved or unknown to hexdb.
        "registered_owner": row[14] or "", "manufacturer": row[15] or "",
    }

    # Sighting history summary from rollup. Cheap — bounded by the
    # number of hourly buckets this aircraft was seen in, typically
    # a few dozen even for frequently-seen aircraft.
    summary_row = conn.execute("""
        SELECT MIN(hour_bucket), MAX(hour_bucket),
               SUM(sighting_count), MAX(max_altitude), MAX(max_speed),
               COUNT(DISTINCT hour_bucket)
        FROM sightings_hourly WHERE icao = ?
    """, (icao,)).fetchone()
    if summary_row and summary_row[0] is not None:
        out["history"] = {
            "first_bucket": summary_row[0],
            "last_bucket": summary_row[1],
            "total_sightings": summary_row[2] or 0,
            "max_altitude_observed": summary_row[3],
            "max_speed_observed": summary_row[4],
            "active_hour_buckets": summary_row[5] or 0,
        }
    else:
        out["history"] = None

    # Distinct callsigns this aircraft has used
    callsigns = conn.execute("""
        SELECT DISTINCT callsign FROM sightings_hourly
        WHERE icao = ? AND callsign IS NOT NULL AND callsign != ''
        ORDER BY callsign
    """, (icao,)).fetchall()
    out["callsigns_used"] = [r[0] for r in callsigns]

    return out


# ---------------------------------------------------------------------------
# v2.53.0: detail page data — extended dataset for /aircraft/{ICAO}
# ---------------------------------------------------------------------------

# Threshold below which the detail page collapses to a stripped-down
# variant. Computed analytics (hour-of-day distribution, day-of-week
# pattern, "weekday operation" chip, etc.) are statistically meaningless
# on tiny sample sizes and would mislead the user. 10 is a starting
# value; adjust if real-world usage shows this is wrong.
LOW_SIGHTING_THRESHOLD = 10


def detail_page_data_for_aircraft(conn: sqlite3.Connection, icao: str) -> Optional[Dict[str, Any]]:
    """Return the rich dataset for the /aircraft/{ICAO} detail page.

    This is a superset of detail_for_aircraft() — it returns the same
    base fields plus computed analytics (hour-of-day distribution,
    day-of-week distribution, altitude/speed/sightings ranges, derived
    pattern chips, recent sightings). Returns None if the ICAO isn't
    in seen_aircraft.

    Cost analysis: every query is bounded by sightings_hourly rows for
    a single ICAO (typically a few dozen to a few thousand buckets),
    indexed by the (icao, hour_bucket) primary key. Sub-millisecond on
    any plausible install. The recent-sightings query touches
    all_sightings filtered to one ICAO, also fast via idx_all_icao.

    The 'mode' field in the response is either 'full' or 'sparse':
      - 'full'   — sighting_count >= LOW_SIGHTING_THRESHOLD; all sections render
      - 'sparse' — too few sightings for analytics to be meaningful;
                   frontend shows facts + sightings table only
    """
    icao = icao.upper()

    # Reuse the base detail function — same shape used by the inline drill,
    # so any existing fields stay consistent across both surfaces.
    base = detail_for_aircraft(conn, icao)
    if base is None:
        return None

    sighting_count = base.get("sighting_count") or 0
    mode = "full" if sighting_count >= LOW_SIGHTING_THRESHOLD else "sparse"

    out = dict(base)
    out["mode"] = mode
    out["low_sighting_threshold"] = LOW_SIGHTING_THRESHOLD

    # Recent sightings: last 20 from all_sightings. Used for the
    # "Recent sightings" panel. Sorted DESC so the most-recent appears
    # first. The all_sightings table has lat/lon/altitude/speed which
    # the rolled-up sightings_hourly doesn't preserve per-event, so we
    # query all_sightings directly here.
    #
    # v2.84.0: instrumented via slow_query_log so the diagnostics UI
    # captures timing and plan if this query crosses the slow threshold.
    # Suspect for the same query-planner pathology as the /api/all/drill
    # SELECT — without a seen_at range here the plan should pick
    # idx_all_icao cleanly, but on installs with millions of rows we
    # want visible evidence rather than assumption.
    from slow_query_log import time_query as _slow_q
    recent = _slow_q(conn, """
        SELECT seen_at, callsign, altitude, speed, lat, lon
        FROM all_sightings
        WHERE icao = ?
        ORDER BY seen_at DESC
        LIMIT 20
    """, (icao,),
    endpoint="/api/aircraft/{icao}", label="detail_recent_sightings")
    out["recent_sightings"] = [
        {
            "seen_at": r[0],
            "callsign": (r[1] or "").strip(),
            "altitude": r[2],
            "speed": r[3],
            "lat": r[4],
            "lon": r[5],
        }
        for r in recent
    ]

    # Sparse mode: stop here. Computed analytics on n<10 sightings are
    # noise. The frontend respects 'mode' and hides the sections that
    # depend on these fields.
    if mode == "sparse":
        out["hour_of_day"] = None
        out["day_of_week"] = None
        out["ranges"] = None
        out["chips"] = []
        return out

    # Hour-of-day distribution: 24-bucket histogram of total sightings
    # by hour-of-day-UTC. We extract the hour from hour_bucket (which
    # is unix epoch seconds aligned to hour boundaries) via integer
    # arithmetic. Indexed by the (icao, hour_bucket) primary key; the
    # query touches only this aircraft's buckets.
    hour_rows = conn.execute("""
        SELECT (hour_bucket / 3600) % 24 AS hour_of_day,
               SUM(sighting_count) AS hits
        FROM sightings_hourly
        WHERE icao = ?
        GROUP BY hour_of_day
    """, (icao,)).fetchall()
    hour_dist = [0] * 24
    for h, hits in hour_rows:
        hour_dist[int(h)] = int(hits or 0)
    out["hour_of_day"] = hour_dist

    # Day-of-week distribution: 7-bucket. Python's datetime.weekday() is
    # Monday=0, Sunday=6, which matches the chip rendering convention.
    # We compute this in Python because SQLite's strftime('%w') is
    # Sunday=0, which would require an extra +6 mod 7 in SQL. Easier
    # to just iterate the buckets.
    import datetime as _dt
    day_dist = [0] * 7
    for hb, hits in conn.execute("""
        SELECT hour_bucket, SUM(sighting_count) AS hits
        FROM sightings_hourly
        WHERE icao = ?
        GROUP BY hour_bucket
    """, (icao,)).fetchall():
        dow = _dt.datetime.utcfromtimestamp(int(hb)).weekday()
        day_dist[dow] += int(hits or 0)
    out["day_of_week"] = day_dist

    # Ranges: min/max altitude, max speed, sightings-per-day stats.
    # The first three come straight from the rollup. Sightings/day
    # requires bucketing into calendar days first, then taking
    # min/median/max of the per-day totals.
    range_row = conn.execute("""
        SELECT MIN(min_altitude), MAX(max_altitude), MAX(max_speed)
        FROM sightings_hourly WHERE icao = ?
        AND min_altitude IS NOT NULL
    """, (icao,)).fetchone()
    min_alt = range_row[0] if range_row and range_row[0] is not None else None
    max_alt = range_row[1] if range_row and range_row[1] is not None else None
    max_speed_row = conn.execute("""
        SELECT MAX(max_speed) FROM sightings_hourly
        WHERE icao = ? AND max_speed IS NOT NULL
    """, (icao,)).fetchone()
    max_speed = max_speed_row[0] if max_speed_row and max_speed_row[0] is not None else None
    # Sightings/day: aggregate hour buckets into calendar days (UTC).
    # 86400 seconds in a day; integer-divide hour_bucket by 86400.
    daily_rows = conn.execute("""
        SELECT (hour_bucket / 86400) AS day,
               SUM(sighting_count) AS hits
        FROM sightings_hourly WHERE icao = ?
        GROUP BY day
    """, (icao,)).fetchall()
    daily_totals = sorted([int(r[1]) for r in daily_rows if r[1]])
    if daily_totals:
        spd_min = daily_totals[0]
        spd_max = daily_totals[-1]
        spd_median = daily_totals[len(daily_totals) // 2]
    else:
        spd_min = spd_max = spd_median = 0
    days_active = len(daily_totals)
    out["ranges"] = {
        "min_altitude_ft": int(min_alt) if min_alt is not None else None,
        "max_altitude_ft": int(max_alt) if max_alt is not None else None,
        "max_speed_kt": int(max_speed) if max_speed is not None else None,
        "sightings_per_day_min": spd_min,
        "sightings_per_day_median": spd_median,
        "sightings_per_day_max": spd_max,
        "days_active": days_active,
    }

    # Derived chips. Each is a deterministic rule operating on the
    # data above. Rules are conservative — they emit only when the
    # signal is strong enough to be informative. Edge cases (rare
    # callsigns, evenly-distributed days, etc.) intentionally produce
    # no chip rather than risk a misleading one.
    chips = []
    total_sightings = sum(day_dist) or 1  # guard against div-by-zero

    # Chip: weekday vs weekend operation. Strong signal needs >=80%
    # one way or the other; closer to 50/50 doesn't get a chip.
    weekday_hits = sum(day_dist[0:5])  # Mon-Fri
    weekend_hits = sum(day_dist[5:7])  # Sat-Sun
    weekday_pct = round(100 * weekday_hits / total_sightings)
    if weekday_pct >= 80:
        chips.append({
            "label": "Weekday operation",
            "value": f"{weekday_pct}% Mon-Fri",
        })
    elif weekday_pct <= 20:
        chips.append({
            "label": "Weekend operation",
            "value": f"{100 - weekday_pct}% Sat-Sun",
        })

    # Chip: time-of-day peak. Look at the top 3 contiguous hours by
    # total hits and emit a chip if they account for >=40% of activity.
    # "Peak hours: 14:00-16:00" is the canonical form.
    if any(h > 0 for h in hour_dist):
        # Find the 3-hour window with the highest sum
        best_start = 0
        best_sum = 0
        for start in range(24):
            window_sum = sum(hour_dist[(start + i) % 24] for i in range(3))
            if window_sum > best_sum:
                best_sum = window_sum
                best_start = start
        peak_pct = round(100 * best_sum / total_sightings)
        if peak_pct >= 40:
            window_label = _hour_window_label(best_start)
            chips.append({
                "label": f"{window_label.title()} peak",
                "value": f"{best_start:02d}:00-{(best_start + 3) % 24:02d}:00",
            })

    # Chip: cruise altitude. If the aircraft has a strong concentration
    # of altitudes in one band (e.g. typical jet cruise FL360-FL400),
    # emit a chip. Bands are 4000-foot wide. Only emits if the dominant
    # band holds >=50% of the per-bucket altitudes.
    if max_alt is not None and max_alt > 5000:
        # Histogram by 4000-foot bands using max_altitude per bucket as a
        # proxy for "cruise altitude observed in this hour"
        alt_bands = {}
        for r in conn.execute("""
            SELECT max_altitude FROM sightings_hourly
            WHERE icao = ? AND max_altitude IS NOT NULL
        """, (icao,)).fetchall():
            alt = int(r[0])
            band = (alt // 4000) * 4000
            alt_bands[band] = alt_bands.get(band, 0) + 1
        if alt_bands:
            total_buckets = sum(alt_bands.values())
            top_band, top_count = max(alt_bands.items(), key=lambda kv: kv[1])
            if top_count / total_buckets >= 0.5:
                # Render as flight level (FL) for >18000ft, raw ft otherwise
                if top_band >= 18000:
                    chips.append({
                        "label": "Cruise altitude",
                        "value": f"typically FL{top_band // 100:03d}",
                    })
                else:
                    chips.append({
                        "label": "Cruise altitude",
                        "value": f"typically {top_band:,} ft",
                    })

    # Chip: primary callsign. If one callsign dominates >=60% of the
    # sightings, emit it. For aircraft that switch callsigns frequently
    # (e.g. tail-number callsigns that incorporate flight numbers), no
    # chip — would be misleading.
    callsign_counts = {}
    for r in conn.execute("""
        SELECT callsign, sighting_count FROM sightings_hourly
        WHERE icao = ? AND callsign IS NOT NULL AND callsign != ''
    """, (icao,)).fetchall():
        cs = r[0].strip()
        callsign_counts[cs] = callsign_counts.get(cs, 0) + (r[1] or 0)
    total_cs = sum(callsign_counts.values())
    if total_cs > 0:
        top_cs, top_cs_count = max(callsign_counts.items(), key=lambda kv: kv[1])
        cs_pct = round(100 * top_cs_count / total_cs)
        if cs_pct >= 60:
            chips.append({
                "label": "Primary callsign",
                "value": f"{top_cs} ({cs_pct}%)",
            })

    # Chip: activity span. "Active 23 of last 47 days" — concrete,
    # always applicable when in full mode. Always emit this one.
    if base.get("first_seen_at") and base.get("last_seen_at"):
        span_days = max(1, (base["last_seen_at"] - base["first_seen_at"]) // 86400)
        chips.append({
            "label": "Active",
            "value": f"{days_active} of {span_days} days",
        })

    out["chips"] = chips

    return out


def _hour_window_label(hour: int) -> str:
    """Map a 24-hour window starting hour to a human label.
    Used for the time-of-day peak chip."""
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "overnight"
