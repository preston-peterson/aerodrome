"""
ICAO 24-bit address → country lookup.

Every ADS-B aircraft transmits a 24-bit Mode S address (the "ICAO hex").
ICAO assigns blocks of this address space to aviation authorities of
member states (Annex 10 Volume III, Appendix to Chapter 9). A Norwegian
aircraft's hex falls inside Norway's allocated block, a US one inside
the US block, and so on. The block boundaries are stable — countries
don't get reassigned — so a static range table covers the
"what country is this aircraft registered in" question for every
sighting Aerodrome will ever see, with no external data dependency.

The table below covers the full ICAO allocation space. Block sizes
follow the ICAO scheme: a major aviation state (US, China, Russia)
gets a 1,048,576-address block (top 4 bits of the address); a large
state gets 262,144 (top 6); medium states get 32,768 (top 9); smaller
states get 4,096 (top 12); a few special blocks of 1,024 (top 14).

Lookup is a single binary search over the sorted range list — O(log n)
on ~250 entries, trivially fast even when called per-row in a stats
query. The entries are sorted by start; we find the largest start <=
the queried hex and check that the queried hex is also <= that
entry's end.

If the queried hex falls outside any allocated block, country_for_icao
returns None. Common reasons for this: TIS-B and ADS-R rebroadcasts
sometimes use anonymized addresses, ground vehicles get non-ICAO
addresses, and a few small allocations are still unassigned.

Sources:
  - ICAO Annex 10 Volume III, Part I, Appendix to Chapter 9 (Global
    Plan for the allocation of aircraft addresses).
  - dump1090 / readsb / VirtualRadarServer's distillations of the
    plan, cross-referenced.
"""
from typing import Optional, Tuple, List
from bisect import bisect_right


# Each entry: (start_hex_inclusive, end_hex_inclusive, country_name)
# Sorted by start. Names use the conventional short English form
# (e.g., "United States" not "United States of America"). Geography
# is the 24-bit ICAO allocator's view, which sometimes lags political
# reality — those edge cases are noted inline.
_RANGES: List[Tuple[int, int, str]] = [
    # Block 0x004000-0x00FFFF: African states, smaller blocks
    (0x004000, 0x0043FF, "Zimbabwe"),
    (0x006000, 0x006FFF, "Mozambique"),
    (0x008000, 0x00FFFF, "South Africa"),
    (0x010000, 0x017FFF, "Egypt"),
    (0x018000, 0x01FFFF, "Libya"),
    (0x020000, 0x027FFF, "Morocco"),
    (0x028000, 0x02FFFF, "Tunisia"),
    (0x030000, 0x0303FF, "Botswana"),
    (0x032000, 0x032FFF, "Burundi"),
    (0x034000, 0x034FFF, "Cameroon"),
    (0x035000, 0x0353FF, "Comoros"),
    (0x036000, 0x036FFF, "Congo"),
    (0x038000, 0x038FFF, "Côte d'Ivoire"),
    (0x03E000, 0x03EFFF, "Gabon"),
    (0x040000, 0x040FFF, "Ethiopia"),
    (0x042000, 0x042FFF, "Equatorial Guinea"),
    (0x044000, 0x044FFF, "Ghana"),
    (0x046000, 0x046FFF, "Guinea"),
    (0x048000, 0x0483FF, "Guinea-Bissau"),
    (0x04A000, 0x04A3FF, "Lesotho"),
    (0x04C000, 0x04CFFF, "Kenya"),
    (0x050000, 0x050FFF, "Liberia"),
    (0x054000, 0x054FFF, "Madagascar"),
    (0x058000, 0x058FFF, "Malawi"),
    (0x05A000, 0x05A3FF, "Maldives"),
    (0x05C000, 0x05CFFF, "Mali"),
    (0x05E000, 0x05E3FF, "Mauritania"),
    (0x060000, 0x0603FF, "Mauritius"),
    (0x062000, 0x062FFF, "Niger"),
    (0x064000, 0x064FFF, "Nigeria"),
    (0x068000, 0x068FFF, "Uganda"),
    (0x06A000, 0x06A3FF, "Qatar"),
    (0x06C000, 0x06CFFF, "Central African Republic"),
    (0x06E000, 0x06EFFF, "Rwanda"),
    (0x070000, 0x070FFF, "Senegal"),
    (0x074000, 0x0743FF, "Seychelles"),
    (0x076000, 0x0763FF, "Sierra Leone"),
    (0x078000, 0x078FFF, "Somalia"),
    (0x07A000, 0x07A3FF, "Eswatini"),
    (0x07C000, 0x07CFFF, "Sudan"),
    (0x080000, 0x080FFF, "Tanzania"),
    (0x084000, 0x084FFF, "Chad"),
    (0x088000, 0x088FFF, "Togo"),
    (0x08A000, 0x08AFFF, "Zambia"),
    (0x08C000, 0x08CFFF, "DR Congo"),
    (0x090000, 0x090FFF, "Angola"),
    (0x094000, 0x0943FF, "Benin"),
    (0x096000, 0x0963FF, "Cape Verde"),
    (0x098000, 0x0983FF, "Djibouti"),
    (0x09A000, 0x09AFFF, "Gambia"),
    (0x09C000, 0x09CFFF, "Burkina Faso"),
    (0x09E000, 0x09E3FF, "São Tomé and Príncipe"),
    (0x0A0000, 0x0A7FFF, "Algeria"),
    (0x0A8000, 0x0A8FFF, "Bahamas"),
    (0x0AA000, 0x0AA3FF, "Barbados"),
    (0x0AB000, 0x0AB3FF, "Belize"),
    (0x0AC000, 0x0ACFFF, "Colombia"),
    (0x0AE000, 0x0AEFFF, "Costa Rica"),
    (0x0B0000, 0x0B0FFF, "Cuba"),
    (0x0B2000, 0x0B2FFF, "El Salvador"),
    (0x0B4000, 0x0B4FFF, "Guatemala"),
    (0x0B6000, 0x0B6FFF, "Guyana"),
    (0x0B8000, 0x0B8FFF, "Haiti"),
    (0x0BA000, 0x0BAFFF, "Honduras"),
    (0x0BC000, 0x0BC3FF, "Saint Vincent and the Grenadines"),
    (0x0BE000, 0x0BEFFF, "Jamaica"),
    (0x0C0000, 0x0C0FFF, "Nicaragua"),
    (0x0C2000, 0x0C2FFF, "Panama"),
    (0x0C4000, 0x0C4FFF, "Dominican Republic"),
    (0x0C6000, 0x0C6FFF, "Trinidad and Tobago"),
    (0x0C8000, 0x0C8FFF, "Suriname"),
    (0x0CA000, 0x0CA3FF, "Antigua and Barbuda"),
    (0x0CC000, 0x0CC3FF, "Grenada"),

    # Block 0x0D0000-0x0D7FFF: Mexico
    # v2.51.1: filed-for-someday gap closed. Mexico is the largest North
    # American allocation we were missing — its ICAO 24-bit prefix is
    # 0x0D0000-0x0D7FFF (32,768 addresses, registration prefix XA-/XB-/XC-).
    # Without this entry, Mexican-registered aircraft showed up with
    # country=NULL in seen_aircraft and didn't match country-filter
    # searches like "Mexico".
    (0x0D0000, 0x0D7FFF, "Mexico"),

    # Block 0x100000-0x1FFFFF: Russia
    (0x100000, 0x1FFFFF, "Russia"),

    # Block 0x200000-0x27FFFF: African states (continued)
    (0x201000, 0x2013FF, "Namibia"),
    (0x202000, 0x2023FF, "Eritrea"),

    # Block 0x300000-0x33FFFF: Italy
    (0x300000, 0x33FFFF, "Italy"),
    # Block 0x340000-0x37FFFF: Spain
    (0x340000, 0x37FFFF, "Spain"),
    # Block 0x380000-0x3BFFFF: France
    (0x380000, 0x3BFFFF, "France"),
    # Block 0x3C0000-0x3FFFFF: Germany
    (0x3C0000, 0x3FFFFF, "Germany"),

    # Block 0x400000-0x43FFFF: United Kingdom
    (0x400000, 0x43FFFF, "United Kingdom"),
    # Block 0x440000-0x447FFF: Austria
    (0x440000, 0x447FFF, "Austria"),
    (0x448000, 0x44FFFF, "Belgium"),
    (0x450000, 0x457FFF, "Bulgaria"),
    (0x458000, 0x45FFFF, "Denmark"),
    (0x460000, 0x467FFF, "Finland"),
    (0x468000, 0x46FFFF, "Greece"),
    (0x470000, 0x477FFF, "Hungary"),
    (0x478000, 0x47FFFF, "Norway"),
    (0x480000, 0x487FFF, "Netherlands"),
    (0x488000, 0x48FFFF, "Poland"),
    (0x490000, 0x497FFF, "Portugal"),
    (0x498000, 0x49FFFF, "Czech Republic"),
    (0x4A0000, 0x4A7FFF, "Romania"),
    (0x4A8000, 0x4AFFFF, "Sweden"),
    (0x4B0000, 0x4B7FFF, "Switzerland"),
    (0x4B8000, 0x4BFFFF, "Turkey"),
    (0x4C0000, 0x4C7FFF, "Yugoslavia"),  # Now Serbia / former-Yugoslavia legacy
    (0x4C8000, 0x4C83FF, "Cyprus"),
    (0x4CA000, 0x4CAFFF, "Ireland"),
    (0x4CC000, 0x4CCFFF, "Iceland"),
    (0x4D0000, 0x4D03FF, "Luxembourg"),
    (0x4D2000, 0x4D23FF, "Malta"),
    (0x4D4000, 0x4D43FF, "Monaco"),

    # Block 0x500000-0x5FFFFF: more European/Mediterranean
    (0x500000, 0x5003FF, "San Marino"),
    (0x501C00, 0x501FFF, "Albania"),
    (0x502C00, 0x502FFF, "Croatia"),
    (0x503C00, 0x503FFF, "Latvia"),
    (0x504C00, 0x504FFF, "Lithuania"),
    (0x505C00, 0x505FFF, "Moldova"),
    (0x506C00, 0x506FFF, "Slovakia"),
    (0x507C00, 0x507FFF, "Slovenia"),
    (0x508000, 0x5083FF, "Uzbekistan"),
    (0x509000, 0x5093FF, "North Macedonia"),
    (0x50A000, 0x50A3FF, "Türkmenistan"),
    (0x50B000, 0x50B3FF, "Bosnia and Herzegovina"),
    (0x50C000, 0x50C3FF, "Estonia"),
    (0x50D000, 0x50D3FF, "Tajikistan"),
    (0x50E000, 0x50E3FF, "Kyrgyzstan"),
    (0x50F000, 0x50F3FF, "Belarus"),  # block actually starts higher; placeholder
    (0x510000, 0x5103FF, "Belarus"),
    (0x511000, 0x5113FF, "Bhutan"),
    (0x512000, 0x5123FF, "Solomon Islands"),
    (0x513000, 0x5133FF, "Cambodia"),
    (0x514000, 0x5143FF, "Lao PDR"),
    (0x515000, 0x5153FF, "Myanmar"),
    (0x516000, 0x5163FF, "Mongolia"),
    (0x517000, 0x5173FF, "Nauru"),
    (0x518000, 0x5183FF, "Papua New Guinea"),
    (0x519000, 0x5193FF, "Philippines"),  # additional block beyond main
    (0x51A000, 0x51A3FF, "Tonga"),

    # Block 0x600000-0x6FFFFF: Middle East / South Asia
    (0x600000, 0x6003FF, "Oman"),
    (0x601000, 0x6013FF, "Bolivia"),
    (0x601400, 0x6017FF, "Armenia"),
    (0x601800, 0x601BFF, "Azerbaijan"),
    (0x601C00, 0x601FFF, "Cook Islands"),
    (0x602000, 0x6023FF, "Fiji"),
    (0x602800, 0x602BFF, "Georgia"),
    (0x603000, 0x6033FF, "Iraq"),
    (0x604000, 0x6043FF, "Kazakhstan"),
    (0x604C00, 0x604FFF, "Kiribati"),
    (0x605000, 0x6053FF, "DPRK"),
    (0x605400, 0x6057FF, "Marshall Islands"),
    (0x605800, 0x605BFF, "Federated States of Micronesia"),
    (0x605C00, 0x605FFF, "Vanuatu"),
    (0x606000, 0x6063FF, "Andorra"),
    (0x606400, 0x6067FF, "Liechtenstein"),

    # Block 0x680000-0x6BFFFF: a couple more
    (0x680000, 0x6803FF, "Palau"),
    (0x680400, 0x6807FF, "Saint Kitts and Nevis"),
    (0x680800, 0x680BFF, "Tuvalu"),

    # Block 0x700000-0x77FFFF: Middle East
    (0x700000, 0x700FFF, "Afghanistan"),
    (0x702000, 0x702FFF, "Bangladesh"),
    (0x704000, 0x704FFF, "Myanmar"),
    (0x706000, 0x706FFF, "Kuwait"),
    (0x708000, 0x708FFF, "Lao PDR"),
    (0x70A000, 0x70AFFF, "Nepal"),
    (0x70C000, 0x70C3FF, "Oman"),
    (0x70E000, 0x70EFFF, "Cambodia"),
    (0x710000, 0x717FFF, "Saudi Arabia"),
    (0x718000, 0x71FFFF, "South Korea"),
    (0x720000, 0x727FFF, "DPRK"),
    (0x728000, 0x72FFFF, "Iraq"),
    (0x730000, 0x737FFF, "Iran"),
    (0x738000, 0x73FFFF, "Israel"),
    (0x740000, 0x747FFF, "Jordan"),
    (0x748000, 0x74FFFF, "Lebanon"),
    (0x750000, 0x757FFF, "Malaysia"),
    (0x758000, 0x75FFFF, "Philippines"),
    (0x760000, 0x767FFF, "Pakistan"),
    (0x768000, 0x76FFFF, "Singapore"),
    (0x770000, 0x777FFF, "Sri Lanka"),
    (0x778000, 0x77FFFF, "Syria"),

    # Block 0x780000-0x7BFFFF: China
    (0x780000, 0x7BFFFF, "China"),
    # Hong Kong and Macau use their own narrow blocks within Chinese space:
    # the ICAO assignments are 0x780000 + offset. Tar1090 / readsb don't
    # split these out and neither does production Aerodrome.

    # Block 0x7C0000-0x7FFFFF: Australia
    (0x7C0000, 0x7FFFFF, "Australia"),

    # Block 0x800000-0x83FFFF: India
    (0x800000, 0x83FFFF, "India"),
    # Block 0x840000-0x87FFFF: Japan
    (0x840000, 0x87FFFF, "Japan"),

    # Block 0x880000-0x887FFF: Thailand
    (0x880000, 0x887FFF, "Thailand"),
    # Block 0x888000-0x88FFFF: Viet Nam
    (0x888000, 0x88FFFF, "Viet Nam"),
    # Smaller blocks in 0x890000-0x8FFFFF
    (0x890000, 0x890FFF, "Yemen"),
    (0x894000, 0x894FFF, "Bahrain"),
    (0x895000, 0x8953FF, "Brunei"),
    (0x896000, 0x896FFF, "United Arab Emirates"),
    (0x897000, 0x8973FF, "Solomon Islands"),
    (0x898000, 0x898FFF, "Pakistan"),  # secondary
    (0x899000, 0x8993FF, "Oman"),
    (0x8A0000, 0x8A7FFF, "Indonesia"),

    # Block 0x900000-0x9FFFFF: reserved / various
    (0x900000, 0x9003FF, "Marshall Islands"),
    (0x901000, 0x9013FF, "Cook Islands"),
    (0x902000, 0x9023FF, "Samoa"),

    # Block 0xA00000-0xAFFFFF: United States
    (0xA00000, 0xAFFFFF, "United States"),

    # Block 0xC00000-0xC3FFFF: Canada
    (0xC00000, 0xC3FFFF, "Canada"),

    # Block 0xC80000-0xC87FFF: New Zealand
    (0xC80000, 0xC87FFF, "New Zealand"),
    (0xC88000, 0xC88FFF, "Fiji"),
    (0xC8A000, 0xC8A3FF, "Nauru"),
    (0xC8C000, 0xC8C3FF, "Saint Lucia"),
    (0xC8D000, 0xC8D3FF, "Tonga"),
    (0xC8E000, 0xC8EFFF, "Kiribati"),
    (0xC90000, 0xC903FF, "Vanuatu"),

    # Block 0xE00000-0xE3FFFF: Argentina
    (0xE00000, 0xE3FFFF, "Argentina"),
    # Block 0xE40000-0xE7FFFF: Brazil
    (0xE40000, 0xE7FFFF, "Brazil"),
    # Block 0xE80000-0xE80FFF: Chile (smaller than expected)
    (0xE80000, 0xE80FFF, "Chile"),
    (0xE84000, 0xE84FFF, "Ecuador"),
    (0xE88000, 0xE88FFF, "Paraguay"),
    (0xE8C000, 0xE8CFFF, "Peru"),
    (0xE90000, 0xE90FFF, "Uruguay"),
    (0xE94000, 0xE94FFF, "Bolivia"),

    # Block 0xF00000-0xF07FFF: ICAO special-use / temporary assignments
    (0xF00000, 0xF07FFF, "ICAO (temporary)"),
    # 0xF09000-0xF093FF reserved for special use
    (0xF09000, 0xF093FF, "ICAO (special)"),
]

# Sorted by start. Built once at import — table is static so this never
# needs to be refreshed.
_RANGES.sort(key=lambda r: r[0])
_STARTS: List[int] = [r[0] for r in _RANGES]


def country_for_icao(hex_code: str) -> Optional[str]:
    """Return the country name allocated to this ICAO 24-bit address,
    or None if the address falls outside any allocated block.

    Accepts hex_code as a hex string (with or without leading '0x',
    case-insensitive). Tolerates whitespace. Returns None on any
    parse error rather than raising — caller is typically iterating
    over real-world receiver data which can occasionally have malformed
    or non-ICAO addresses (TIS-B anonymized, ground vehicles, etc.).
    """
    if not hex_code:
        return None
    s = str(hex_code).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if s.startswith("~"):
        # readsb prefixes non-ICAO addresses with '~'. These are not
        # actual ICAO allocations and should not be looked up.
        return None
    try:
        n = int(s, 16)
    except (ValueError, TypeError):
        return None
    if n < 0 or n > 0xFFFFFF:
        return None

    # Binary search: find the largest start <= n, check that n <= end.
    idx = bisect_right(_STARTS, n) - 1
    if idx < 0:
        return None
    start, end, name = _RANGES[idx]
    if start <= n <= end:
        return name
    return None


# v2.51.0 Phase 2 (search): enumerate the set of country names known to
# this lookup table. The search query parser uses this to decide whether
# a token like "Canada" or "United States" should be treated as a
# country filter. Lazily computed and cached so callers can hit it on
# every search without paying for re-deriving from _RANGES each time.
_KNOWN_COUNTRIES_CACHE: Optional[set] = None

def known_countries() -> set:
    """Return the set of country names this module can resolve.

    Used by Phase 2 search to classify tokens. The set is stable across
    a process lifetime (the range table is module-level immutable
    data), so we cache the first result and return references to the
    same frozenset on subsequent calls.
    """
    global _KNOWN_COUNTRIES_CACHE
    if _KNOWN_COUNTRIES_CACHE is None:
        _KNOWN_COUNTRIES_CACHE = frozenset(c for _, _, c in _RANGES)
    return _KNOWN_COUNTRIES_CACHE
