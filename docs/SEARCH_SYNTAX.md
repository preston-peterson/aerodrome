# Search Syntax

The Search tab supports a free-form text query that the parser interprets
into one or more filters plus optional free-text matching. Multiple
filters AND together (an aircraft must match all of them).

## Field types

### Aircraft type
Four-character ICAO type designator. Common examples:

- `B738` — Boeing 737-800
- `A320` — Airbus A320
- `C172` — Cessna 172
- `B38M` — Boeing 737 MAX 8
- `CRJ9` — Bombardier CRJ-900

The parser recognizes any code in the project's aircraft type table
(roughly 1,500 designators).

### Country
Country of registration, derived from the aircraft's ICAO 24-bit address.

- `Canada`
- `United States`
- `Germany`
- `United Kingdom`

Multi-word countries work — the parser greedy-matches longest country
names first, so `B738 United States` parses as type=B738 + country=United States,
not type=B738 + free-text "United" + free-text "States".

### Operator
The airline derived from a callsign's 3-letter prefix. Search matches
both the ICAO code and the full airline name:

- `UAL` or `United Airlines` — same airline
- `Delta` or `DAL`
- `Southwest` or `SWA`
- `JetBlue` or `JBU`

### Callsign
Full or partial flight callsign:

- `UAL2024` — exact match
- `ACA192` — Air Canada flight 192

### Tail number (registration)
Aircraft registration code:

- `N12345` — US tail
- `G-XYZA` — UK tail
- `D-ABCD` — German tail
- `JA001A` — Japanese tail

### ICAO hex
Six-character hex aircraft address:

- `A12345`
- `C05044`

### Military
Match aircraft classified as military by the install's configuration:

- `mil`
- `military`

Both keywords mean the same thing. The match uses the same logic as the
MIL pill rendered on row data — an aircraft matches if its ICAO is in
the configured `military.special_aircraft` list, its ICAO starts with a
configured `military.icao_prefixes` entry, or its callsign starts with
a configured `military.callsign_prefixes` entry. Combine with other
tokens to narrow further:

- `mil C130` — military C-130 Hercules aircraft
- `mil United States` — US military aircraft

If your install hasn't configured any military prefixes or special
aircraft, this filter returns no results — the system can't classify
aircraft as military without rules to apply.

### Watchlist
Match aircraft on your configured watchlist:

- `watchlist`
- `wl`

Both keywords mean the same thing. The match uses the same logic as the
orange watchlist label on the Watchlist tab — an aircraft matches if
its ICAO, callsign prefix, or model substring is in your `watchlist`
config. Combine with other tokens to narrow further:

- `wl B738` — Boeing 737-800s on your watchlist
- `mil watchlist` — military aircraft on your watchlist

Tail-number-only watchlist entries (entries that specify only `tail:` and
not `icao:`) require hexdb lookup to translate to an ICAO, which isn't
done at search time. To make tail-only entries searchable, also add the
`icao:` to the watchlist entry once you know it.

### First seen today
Match aircraft whose first-ever sighting on this receiver was today:

- `first_seen_today`

The window is today's local-day boundary based on `stats.timezone` in
your config (same window as the Stats tab's "First time seen today"
card). An aircraft matches if `first_seen_at` falls within today's
window; aircraft seen previously and again today do not match. Combine:

- `first_seen_today military` — military aircraft seen for the first time today
- `first_seen_today B738` — 737-800s seen for the first time today
- `today first_seen_today` — aircraft seen today AND first seen today
  (equivalent to `first_seen_today` since "first seen today" implies
  "seen today"; the extra `today` token costs nothing but is redundant)

Backs the **View all in Search →** button on the Stats tab's "First
time seen today" card — that card shows the first ~10 ICAOs and a
"+N more" counter; clicking the button opens Search with this filter
active and the full list paginated as normal Search results.

### Peak today
Match aircraft seen during today's peak simultaneous moment:

- `peak_today`

The peak moment is the 60-second window with the highest
`COUNT(DISTINCT icao)` of any minute today (same definition as the
Stats tab's "Peak simultaneous" card). The filter resolves the peak
bucket at query time and includes every aircraft whose sightings fall
in that minute. On tied days (multiple minutes share the maximum
count), the earliest minute wins — the same tiebreaker the Stats
drill panel uses, so both surfaces always agree on which moment to
highlight. Combine:

- `peak_today military` — military aircraft at the peak moment
- `peak_today watchlist` — watchlist aircraft at the peak moment
- `peak_today B738` — 737-800s at the peak moment

The chip on the result page reads "Peak today (15 aircraft at 14:32)"
when resolution succeeds, so you can see what moment was selected.

Backs the **View in Search →** button on the Stats tab's "Peak
simultaneous" card. Before v2.82.0 that button fell back to today's
full list because Search had no peak-moment filter; the chip strip
showed `today` only and users couldn't tell whether they were looking
at the peak set or the day's set. With this token the redirect lands
on exactly the aircraft the drill panel shows.

### Date
The parser accepts multiple date formats. **ISO format is always
accepted regardless of the configured locale.** Slash-separated formats
depend on the `display.date_format` setting in your `config.yaml` (also
configurable from the Configuration → Display tab).

| Setting | Accepted slash format | ISO formats (always) |
|---------|----------------------|---------------------|
| `MDY` (default) | `4/29/26`, `4/29/2026` | `2026-04-29`, `2026-04`, `2026` |
| `DMY` | `29/4/26`, `29/4/2026` | `2026-04-29`, `2026-04`, `2026` |
| `ISO` | (none — strict) | `2026-04-29`, `2026-04`, `2026` |

Two-digit years follow POSIX strptime convention: `00`-`69` resolve to
2000-2069, `70`-`99` resolve to 1970-1999.

A date matches aircraft whose `last_seen_at` falls within that window
(day, month, or year). Combining a date with another filter narrows
results further:

- `B738 2026-04-29` — Boeing 737-800s last seen on April 29
- `Canada 2026-04` — Canadian aircraft last seen during April 2026
- `Delta 2026` — Delta aircraft last seen at any point in 2026

The bare `today` token resolves to today's local-day window (per
`stats.timezone`). Same window the Stats tab's "today" sections use.

### Hour of day
Filter to a specific hour bucket on today's date:

- `hour:14` — aircraft seen during 14:00–14:59 today
- `hour:8-10` — aircraft seen during 08:00–10:59 today (inclusive on
  both ends — the window covers all of hours 8, 9, AND 10)
- `hour:0-23` — full day, equivalent to `today`

The hour value follows the configured `stats.timezone` so the bucket
matches the Stats tab's hourly histogram exactly. `hour:14` without
`today` still applies to today (single-hour windows narrower than a
day; "today" is implicit). Wraparound (`hour:23-1`) is rejected — run
two queries instead.

Range syntax is **inclusive on both ends** because hour ranges read
as calendar intervals ("hours 14 through 16" naturally includes hour
16). This differs from `distance:LO-HI` below, which is
**inclusive-exclusive** (matches bucket UX where `50-100` is a
50-mile-wide bucket, not 51 miles wide).

### Distance
Filter by distance from the receiver, in your configured unit
(`receiver.distance_unit` — mi/km/nmi):

- `distance:50-100` — between 50 and 100 (inclusive lo, exclusive hi)
- `distance:<100` — under 100 (exclusive)
- `distance:>200` — over 200 (exclusive)
- `distance:0-25` — within 25 of the receiver

Distance comparison is exclusive on both `<` and `>` operators
(`distance:<100` matches exactly under 100, not equal to or under 100).
Bounds must be non-negative numbers.

Aircraft with no last-known position (rare but possible) are excluded
from distance-filtered results.

## Combining tokens

All tokens space-separated; conditions AND together:

```
B738 Canada 2026-04
```

Returns: Boeing 737-800s registered in Canada whose last sighting was
in April 2026.

Tokens that don't match any field type fall through to free-text
search, which uses SQLite FTS5 against the searchable columns
(operator, country, callsign, type description, etc.). FTS5 handles
casing and partial matching automatically.

## Filter chips

After running a query, chips appear below the search box showing what
the parser understood:

- **Solid border, cyan value** — recognized as a specific filter type
  (Type, Country, Operator, etc.)
- **Dashed border, italic value** — fell through to free-text. Useful
  for spotting typos: typing `Canda` shows a dashed chip rather than
  silently returning nothing.

Click the `×` on any chip to remove that filter and re-run.

## Date format setting

The `display.date_format` config option controls which slash-date
locale the search parser accepts. Located in:

- **UI:** Configuration page → Display tab → Date format
- **File:** `config.yaml` under the `display` section

Change is live — no restart required. The `?` button next to the search
input shows examples matching the current setting.

## Examples

| Query | Returns |
|-------|---------|
| `B738` | All Boeing 737-800s ever seen |
| `Canada` | All Canadian-registered aircraft |
| `Delta` | All Delta Airlines aircraft (matches operator `DAL`) |
| `UAL2024` | Specific United Airlines flight |
| `N12345` | Aircraft with that exact tail number |
| `A12345` | Aircraft with that ICAO hex address |
| `B738 Canada` | Canadian Boeing 737-800s |
| `Delta 2026-04` | Delta aircraft seen in April 2026 |
| `2026-04-29` | All aircraft last seen on April 29, 2026 |
| `4/29/26` (MDY locale) | Same as above |
| `29/4/26` (DMY locale) | Same as above |

## Output options

- **Cards** — clickable, expand to drill into per-aircraft detail
- **Sightings table** (inside drill panel) — click `N sightings ↗` to
  see individual sighting rows; defaults to last 30 days, expandable
  to lifetime
- **Track ↗** — opens the aircraft in your configured external tracker
  (set via `receiver.track_link_provider`)
- **Export ▾** — CSV export of current page or all matches up to 5,000
  rows
- **Hash URL** — every search updates `#search?q=...` so links are
  shareable

## Limits

- **5,000 rows** — hard cap on "Export all matches" CSV
- **2,000 sightings** — hard cap on lifetime sightings drill in the
  Search results panel. For unbounded sighting history per aircraft,
  click the ICAO to open the aircraft detail page (`/aircraft/{ICAO}`),
  which paginates the full sighting history via "Load more".
- **16 tokens** — query parser truncates beyond this (defense against
  pasted paragraphs)
- **500** — maximum per-page result limit (default 100, selectable
  via Rows: dropdown)
