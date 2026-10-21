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


# Helicopter ICAO type designators: every designator ICAO DOC 8643 lists
# as a helicopter (v3.4.141 — refresh from scripts/doc8643.csv), plus the
# military/legacy codes some feeds send (CH47, UH60, …) and B206. The old
# 18-code list missed common types (R66, S76, B505, EN48) and drifted from
# the collector's TYPE_CEILINGS. Adding entries here picks them up in both
# the Stats card and the search filter.
HELICOPTER_TYPES = frozenset({
    "A109", "A119", "A129", "A139", "A149", "A169", "A189", "A2RT", "A600",
    "AC31", "AC33", "ALH", "ALO2", "ALO3", "ANST", "AS32", "AS3B", "AS50",
    "AS55", "AS65", "B06", "B06T", "B105", "B150", "B212", "B214", "B222",
    "B230", "B305", "B407", "B412", "B427", "B429", "B430", "B47G", "B47J",
    "B47T", "B505", "B525", "BABY", "BK17", "BRB2", "BSTP", "CH12", "CH14",
    "CH7", "CHIF", "COMU", "DJIN", "DRAG", "DYH2", "DYH3", "EC20", "EC25",
    "EC30", "EC35", "EC45", "EC55", "EC75", "EGL3", "EH10", "ELTO", "EN28",
    "EN48", "ES11", "EXEC", "EXEJ", "EXPL", "FH11", "FREL", "G2CA", "GAZL",
    "H12T", "H160", "H2", "H21", "H269", "H43A", "H43B", "H46", "H47",
    "H500", "H53", "H53S", "H60", "H64", "HSMT", "HUCO", "HX2", "IS2",
    "JAG2", "K126", "K209", "K226", "KA25", "KA26", "KA27", "KA50", "KA52",
    "KA62", "KH4", "KMAX", "LAMA", "LCH", "LR2T", "LYNX", "M74", "MD52",
    "MD60", "MH20", "MI10", "MI14", "MI2", "MI24", "MI26", "MI28", "MI34",
    "MI38", "MI4", "MI6", "MI8", "NA40", "NH90", "OH1", "PHIL", "PSW4",
    "PUMA", "R22", "R4", "R44", "R66", "RMOU", "RP1", "RVAL", "S274",
    "S278", "S285", "S330", "S360", "S434", "S51", "S52", "S55P", "S55T",
    "S58P", "S58T", "S61", "S61R", "S62", "S64", "S65C", "S76", "S92",
    "S97", "SB1", "SCOR", "SCOU", "SH09", "SH4", "SUCO", "SURN", "SYCA",
    "TIGR", "UH1", "UH12", "UH1Y", "ULTS", "V500", "W3", "WASP", "WESX",
    "WZ10", "X2", "X3", "X49", "YNHL", "ZA6", "ZEFR",
    "B206", "CH47", "EC20T", "H6", "H65", "UH60", "UH72",
})


# Commercial aircraft type-code prefixes. A type that begins with any of
# these strings (after upper-casing) classifies as commercial. v3.4.141:
# "A2", "A3" and "B3" were dropped — they also matched the King Air 350
# (B350), the turbine Bonanza (B36T) and light aircraft such as the A210;
# those airliner families are listed exactly below instead.
COMMERCIAL_PREFIXES = (
    "B7", "CRJ", "E1", "E7",
)


# Exact-match commercial type codes.
COMMERCIAL_EXACT = frozenset({
    # Airbus — the A220 flies as BCS1/BCS3 (the A220 entry never matched)
    "A306", "A30B", "A310", "A318", "A319", "A320", "A321", "A19N", "A20N",
    "A21N", "A332", "A333", "A337", "A338", "A339", "A342", "A343", "A345",
    "A346", "A359", "A35K", "A388", "A3ST", "BCS1", "BCS3",
    "A220", "A330", "A340", "A350", "A380",
    # Boeing 737 MAX
    "B37M", "B38M", "B39M", "B3XM",
    # Embraer E2 jets (E1/E7 cover the first-generation E-Jets)
    "E290", "E295",
    # McDonnell Douglas
    "DC10", "MD11", "MD80", "MD81", "MD82", "MD83", "MD87", "MD88", "MD90",
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
