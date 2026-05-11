"""
Aircraft category classification.

One source of truth for the "what kind of aircraft is this?" heuristics
that the Stats `category_mix` card and (starting v2.90.0) the search
filter both rely on. Categorization happens at write time in the
collector and at backfill time in migration v7; both paths call
`classify()` with the same inputs and get the same answer.

History: prior to v2.89.0 these heuristics lived inline in
server.py's category_mix card. Moving them here:
  - lets the search system reach the same classification (planned
    v2.90.0: tokens like `commercial`, `general_aviation`, `helicopter`)
  - removes the Python-loop-per-render cost from the Stats query
    (replaced by a SELECT/GROUP BY against a stored column)
  - gives the heuristics a stable home for future tuning (more
    helicopter type codes, more commercial prefixes, etc.)

The five categories are mutually exclusive — every aircraft maps
to exactly one. Precedence (top wins on a tie):

  1. military       — `is_military` argument is True
  2. helicopter     — type code in HELICOPTER_TYPES, OR
                      type_desc contains "helicopter" (case-insensitive)
  3. commercial     — type code matches COMMERCIAL_PREFIXES tuple
                      OR is in COMMERCIAL_EXACT
  4. general_aviation — non-empty type code that didn't match
                        commercial or helicopter
  5. unknown        — empty type code

Tests in test_categorize.py.
"""
from typing import Optional


# Helicopter ICAO type designators. Lifted from the v2.85.9 category_mix
# implementation. Adding new entries here automatically picks them up
# in both the Stats card and the search filter.
HELICOPTER_TYPES = frozenset({
    "H60", "H47", "EC35", "EC45", "EC55", "AS50", "AS55", "R22", "R44",
    "B06", "B206", "B407", "B412", "B429", "B430", "A109", "A119", "A139",
})


# Commercial aircraft type-code prefixes. A type that begins with any of
# these strings (after upper-casing) classifies as commercial. Same list
# as the v2.85.9 inline test, just hoisted to module scope.
COMMERCIAL_PREFIXES = (
    "A3", "A2", "B7", "B3", "CRJ", "E1", "E7",
)


# Exact-match commercial type codes. The MD8x and Airbus codes can't
# be captured by prefix alone (some overlap with general aviation
# patterns); kept as an explicit set.
COMMERCIAL_EXACT = frozenset({
    "MD80", "MD82", "MD83", "MD88", "MD90",
    "A220", "A319", "A320", "A321", "A330", "A340", "A350", "A380",
})


def classify(aircraft_type: Optional[str],
              type_desc: Optional[str],
              is_military: bool) -> str:
    """Return one of: 'commercial', 'general_aviation', 'military',
    'helicopter', 'unknown'. See module docstring for precedence rules.

    Args:
        aircraft_type: ICAO type designator (e.g. "B738", "C172"). Empty
                       or None classifies as 'unknown' unless overridden
                       by is_military.
        type_desc:     Human-readable type description (e.g. "Boeing
                       737-800"). Used for the helicopter heuristic when
                       the type code itself isn't in HELICOPTER_TYPES
                       (some feeders only fill type_desc for helicopters).
        is_military:   Result of the collector's existing is_military()
                       check at write time. Pre-computed by the caller
                       so this module doesn't need access to the config
                       or the raw aircraft dict.
    """
    # Military precedence is highest — once an aircraft is military
    # it stays military regardless of type code patterns. The
    # collector's UPSERT enforces stickiness across polls (a military
    # classification never reverts even if a later poll's
    # is_military() returns False); this function just answers what
    # THIS poll's classification would be. Stickiness is a SQL-side
    # CASE in the conflict resolution.
    if is_military:
        return "military"

    t = (aircraft_type or "").strip().upper()
    desc = (type_desc or "").lower()

    # Helicopter: type code in our hardcoded set, OR type description
    # mentions helicopter. The desc fallback catches the case where
    # the feeder reports a generic "helicopter" string but no type
    # designator (or one we don't recognize).
    if t in HELICOPTER_TYPES or "helicopter" in desc:
        return "helicopter"

    # Commercial: prefix match OR exact match.
    if t:
        if t.startswith(COMMERCIAL_PREFIXES) or t in COMMERCIAL_EXACT:
            return "commercial"
        return "general_aviation"

    # No type code at all — can't classify any further.
    return "unknown"
