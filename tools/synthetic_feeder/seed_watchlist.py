"""
Demo-mode watchlist seeder.

Run once during `install.sh --demo` to pre-populate a small watchlist
so the watchlist feature is exercised right away when a user explores
demo mode. The picked ICAOs are deterministic — same seed (1903) as
the running synthetic feeder, so the watchlisted aircraft are real
"regulars" that the user will actually see come into range.

Usage::

    python3 -m tools.synthetic_feeder.seed_watchlist <config.yaml path>

Implementation:

  1. Build a Fleet with the same seed and home coords the feeder will
     use, take the first 8 aircraft's ICAOs.
  2. Edit config.yaml in-place: replace the line `watchlist: []` with
     a populated watchlist of 8 entries.
  3. Preserve all surrounding comments via line-pattern substitution
     (matches the bootstrap.sh config-patching convention).

Idempotent: running it twice has the same effect as running it once.
If config.yaml already has a non-empty watchlist (which shouldn't
happen on a fresh demo install but is defensive), this script bails
without modifying anything.
"""

from __future__ import annotations

import re
import sys
from typing import List

try:
    from .generator import Fleet
except ImportError:  # pragma: no cover — direct-script fallback
    from generator import Fleet  # type: ignore


# Same seed the feeder systemd unit passes via --seed. Keep these in sync
# (changing one without the other means the watchlisted ICAOs aren't the
# actual "regulars" the user sees).
DEMO_SEED = 1903

# How many regulars to pre-watchlist. Per v3.1.0 design: small enough that
# watchlist hits feel like noticing something rather than constant churn,
# large enough that hits actually trigger in a reasonable demo session.
DEMO_WATCHLIST_SIZE = 8


def pick_demo_icaos(
    home_lat: float,
    home_lon: float,
    seed: int = DEMO_SEED,
    count: int = DEMO_WATCHLIST_SIZE,
) -> List[str]:
    """Return the ICAOs of the first `count` aircraft the deterministic
    fleet generates. These are the "regulars" the user will see most
    often during a demo session."""
    # We don't need the whole 50-aircraft fleet; we just need to pull
    # `count` ICAOs out of the same Fleet construction. Build a small
    # one with the same seed and home coords — the seed determines the
    # ICAO sequence regardless of fleet size.
    fleet = Fleet(
        size=max(count, 1),
        home_lat=home_lat,
        home_lon=home_lon,
        seed=seed,
    )
    return [ac.hex.upper() for ac in fleet.aircraft[:count]]


def build_watchlist_yaml(icaos: List[str]) -> str:
    """Render the watchlist YAML block to slot into config.yaml.

    Format matches the example in config.yaml.example so a user reading
    the file sees consistent structure. Labels are sequential
    'Demo: regular #1' through '#8' so they're identifiable as
    auto-seeded entries (and easy to find + clear if the user later
    decides to wipe them manually before running the switch-to-real
    wizard).
    """
    lines = ["watchlist:"]
    for i, icao in enumerate(icaos, 1):
        lines.append(f'  - icao: "{icao}"')
        lines.append(f'    label: "Demo: regular #{i}"')
    return "\n".join(lines)


def patch_config_yaml(path: str, watchlist_yaml: str) -> str:
    """Replace `watchlist: []` in config.yaml with the rendered block.

    Returns a status message describing what happened. Raises on
    structural problems (missing watchlist key, malformed file).
    """
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    # Idempotency guard: if watchlist is already non-empty (e.g. user
    # ran the seeder twice, or hand-edited the file), bail without
    # damage. The seeder is meant for a fresh install.
    if re.search(r"^watchlist:\s*\n\s+-", text, re.M):
        return "watchlist already populated; skipping (no change)"

    # Match the canonical empty form first.
    pat_empty = re.compile(r"^watchlist:\s*\[\]\s*$", re.M)
    if pat_empty.search(text):
        new_text = pat_empty.sub(watchlist_yaml, text, count=1)
    else:
        # Fall back: match `watchlist:` followed by nothing-or-comment.
        # Defensive against minor config.yaml format drift.
        pat_bare = re.compile(r"^watchlist:\s*(#.*)?$", re.M)
        if not pat_bare.search(text):
            raise RuntimeError(
                "Could not find a `watchlist:` line to patch in config.yaml. "
                "Has the example file changed shape?"
            )
        new_text = pat_bare.sub(watchlist_yaml, text, count=1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return f"watchlist seeded with {len(watchlist_yaml.splitlines()) // 2} entries"


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print(
            "Usage: python3 -m tools.synthetic_feeder.seed_watchlist "
            "<config.yaml path> [home_lat] [home_lon]",
            file=sys.stderr,
        )
        return 2

    config_path = argv[1]

    # Home coords default to the feeder's defaults if not supplied.
    # install.sh will pass the user-supplied values from the bootstrap
    # prompt so the watchlisted aircraft are seeded against the same
    # geometry the running feeder uses.
    try:
        home_lat = float(argv[2]) if len(argv) > 2 else 40.0
        home_lon = float(argv[3]) if len(argv) > 3 else -75.0
    except ValueError:
        print("home_lat / home_lon must be floats", file=sys.stderr)
        return 2

    icaos = pick_demo_icaos(home_lat=home_lat, home_lon=home_lon)
    wl_yaml = build_watchlist_yaml(icaos)
    msg = patch_config_yaml(config_path, wl_yaml)
    print(f"seed_watchlist: {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
