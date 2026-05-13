#!/usr/bin/env python3
"""
Interactive menu for the synthetic feeder.

Wraps `serve.py` (Mode A) and `backfill.py` (Mode B) with prompted
defaults so you don't have to remember flags. Run from the project
root::

    python3 -m tools.synthetic_feeder.menu

Each menu choice that runs something launches the underlying module
as a subprocess. Ctrl-C kills the subprocess and returns to the menu
rather than killing the whole script. The menu loop continues until
you pick Quit.

For automation or scripted use, invoke the underlying modules directly
(serve.py / backfill.py) with their own flags — the menu is purely a
convenience for interactive sessions.
"""

from __future__ import annotations

import os
import subprocess
import sys


# Path to the project root, computed from this file's location. Used
# when launching subprocess so the `-m tools.synthetic_feeder.X`
# invocation resolves regardless of the user's working directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))


# ---------------------------------------------------------------------
# Prompt helpers

def _prompt(label: str, default: str = "") -> str:
    """Read one line from stdin, returning the default if blank."""
    suffix = f" [{default}]" if default else ""
    raw = input(f"  {label}{suffix}: ").strip()
    return raw if raw else default


def _prompt_int(label: str, default: int) -> int:
    while True:
        raw = _prompt(label, str(default))
        try:
            return int(raw)
        except ValueError:
            print(f"    Not a number: {raw!r}. Try again.")


def _prompt_float(label: str, default: float) -> float:
    while True:
        raw = _prompt(label, str(default))
        try:
            return float(raw)
        except ValueError:
            print(f"    Not a number: {raw!r}. Try again.")


def _prompt_yes_no(label: str, default: bool = False) -> bool:
    default_str = "Y/n" if default else "y/N"
    raw = _prompt(label, default_str).lower()
    if raw in ("y", "yes", "y/n"):
        return True
    if raw in ("n", "no", "y/n"):
        return False
    return default


# ---------------------------------------------------------------------
# Subprocess launch

def _run_module(module: str, args: list) -> None:
    """Launch a sibling module as a subprocess. Ctrl-C in the child
    raises KeyboardInterrupt here, which we swallow so we land back at
    the menu loop instead of killing the menu itself.

    Two layouts supported:
      1. Inside an Aerodrome repo at tools/synthetic_feeder/ — invoke
         via `python3 -m tools.synthetic_feeder.<module>` from the
         project root. This is needed for backfill, which imports
         collector.init_db from the project root.
      2. Standalone (e.g. on a dedicated feeder VM) — invoke the
         sibling .py directly. backfill won't work in this layout
         since collector isn't importable, but serve is fully
         self-contained and works fine.
    Auto-detects which layout we're in by looking for collector.py
    two levels up.
    """
    project_root = os.path.abspath(os.path.join(_HERE, "..", ".."))
    is_in_aerodrome_repo = os.path.exists(
        os.path.join(project_root, "collector.py")
    )
    if is_in_aerodrome_repo:
        cmd = [sys.executable, "-m",
               f"tools.synthetic_feeder.{module}"] + args
        cwd = project_root
    else:
        cmd = [sys.executable,
               os.path.join(_HERE, f"{module}.py")] + args
        cwd = _HERE
    print()
    print(f"$ {' '.join(cmd)}")
    print()
    try:
        subprocess.run(cmd, cwd=cwd)
    except KeyboardInterrupt:
        # The child already saw the SIGINT; we just want to return
        # to the menu without propagating.
        print("\n  (interrupted — returning to menu)")
    print()


# ---------------------------------------------------------------------
# Mode A — server

def _menu_serve() -> None:
    print()
    print("Mode A — Synthetic Feeder Server")
    print("-" * 40)
    print("  1) Start with defaults (port 8080, 100 aircraft, listening")
    print("     on all interfaces — works for the typical case)")
    print("  2) Customize (port / visible / location / range / etc.)")
    print("  0) Back to main menu")
    print()
    choice = _prompt("Choice", "1")
    if choice == "0":
        return
    if choice == "1":
        # Just go. The feeder defaults are the typical case: bind on
        # 0.0.0.0:8080 (so a separate Aerodrome VM can reach it),
        # 100 visible aircraft, generic US-east receiver location.
        print()
        print("Starting feeder with defaults. Configure your test")
        print("Aerodrome's config.yaml:")
        print("  receiver:")
        print("    ip: <THIS_HOST_LAN_IP>")
        print("    port: 8080")
        print("    path: /data/aircraft.json")
        _run_module("serve", ["--host", "0.0.0.0", "--port", "8080"])
        return
    if choice != "2":
        print(f"  Unrecognised: {choice!r}")
        return
    # Custom path — same prompts as before.
    print()
    print("  Press Enter at any prompt to accept the default in brackets.")
    print()
    port = _prompt_int("Port", 8080)
    visible = _prompt_int("Visible aircraft", 100)
    home_lat = _prompt_float("Home latitude", 40.0)
    home_lon = _prompt_float("Home longitude", -75.0)
    range_km = _prompt_float("Range km", 250.0)
    advanced = _prompt_yes_no("Configure military fraction / tick / seed?", False)
    args = [
        "--host", "0.0.0.0",
        "--port", str(port),
        "--visible", str(visible),
        "--home-lat", str(home_lat),
        "--home-lon", str(home_lon),
        "--range", str(range_km),
    ]
    if advanced:
        mil_frac = _prompt_float("Military fraction (0.0–1.0)", 0.05)
        tick = _prompt_float("Tick interval seconds", 1.0)
        seed_raw = _prompt("Random seed (blank for random)", "")
        args += [
            "--military-fraction", str(mil_frac),
            "--tick-interval", str(tick),
        ]
        if seed_raw:
            try:
                int(seed_raw)
                args += ["--seed", seed_raw]
            except ValueError:
                print(f"  Skipping invalid seed: {seed_raw!r}")
    print()
    print(f"Starting feeder. Configure your test Aerodrome's config.yaml:")
    print(f"  receiver:")
    print(f"    ip: <THIS_HOST_LAN_IP>")
    print(f"    port: {port}")
    print(f"    path: /data/aircraft.json")
    _run_module("serve", args)


# ---------------------------------------------------------------------
# Mode B — backfill

# Backfill presets. Each entry is (label, rows, days, aircraft, est_minutes).
# Times are rough estimates from the README's commodity-hardware table.
_BACKFILL_PRESETS = [
    ("Tiny smoke test    (50k rows,    2 days,    500 aircraft)", 50_000, 2, 500, "~10 sec"),
    ("Quick test         (1M rows,     7 days,    10k aircraft)", 1_000_000, 7, 10_000, "~3 min"),
    ("Match loaded install      (12.6M rows,  18 days,   26k aircraft)", 12_600_000, 18, 26_000, "~12 min"),
    ("Custom             (you specify rows/days/aircraft)", 0, 0, 0, "varies"),
]


def _menu_backfill() -> None:
    print()
    print("Mode B — Historical Backfill")
    print("-" * 40)
    # Detect: if collector.py isn't importable from the project root,
    # we're running standalone (e.g. on a dedicated feeder VM). Backfill
    # imports collector.init_db so it has to run from inside an
    # Aerodrome repo. Fail with a clear message rather than letting the
    # subprocess bomb out with an opaque ImportError.
    project_root = os.path.abspath(os.path.join(_HERE, "..", ".."))
    if not os.path.exists(os.path.join(project_root, "collector.py")):
        print()
        print("  Backfill is not available in this layout.")
        print()
        print("  Backfill creates a synthetic Aerodrome database, which")
        print("  requires importing collector.init_db for the schema.")
        print("  That means this menu has to run from inside an Aerodrome")
        print("  repository, with the layout:")
        print()
        print("    /path/to/aerodrome/")
        print("      collector.py, server.py, ...")
        print("      tools/synthetic_feeder/menu.py  <- run from project root")
        print()
        print("  On a dedicated feeder VM (no Aerodrome installed),")
        print("  Mode A still works and is what you want anyway —")
        print("  the feeder VM serves live data, the Aerodrome VM does")
        print("  its own backfill against its own database.")
        print()
        input("  (press Enter to return to menu)")
        return
    print("  Generates a fresh synthetic database. Existing files are")
    print("  not touched unless you opt in. Press Enter to accept the")
    print("  default in brackets at any prompt.")
    print()
    for i, (label, _, _, _, est) in enumerate(_BACKFILL_PRESETS, 1):
        print(f"  {i}) {label}  [{est}]")
    print(f"  0) Back to main menu")
    print()
    choice_raw = _prompt("Preset", "1")
    try:
        choice = int(choice_raw)
    except ValueError:
        print(f"  Not a number: {choice_raw!r}")
        return
    if choice == 0:
        return
    if not (1 <= choice <= len(_BACKFILL_PRESETS)):
        print(f"  Out of range: {choice}")
        return
    label, rows, days, aircraft, est = _BACKFILL_PRESETS[choice - 1]
    if rows == 0:  # Custom
        rows = _prompt_int("Rows", 1_000_000)
        days = _prompt_int("Days", 7)
        aircraft = _prompt_int("Unique aircraft", 2_000)
    db_path = _prompt(
        "Output DB path", "./aircraft_history_synthetic.db"
    )
    home_lat = _prompt_float("Home latitude", 40.0)
    home_lon = _prompt_float("Home longitude", -75.0)
    overwrite = False
    if os.path.exists(db_path):
        overwrite = _prompt_yes_no(
            f"{db_path} exists. Overwrite?", False
        )
        if not overwrite:
            print("  Aborted.")
            return
    args = [
        "--db", db_path,
        "--rows", str(rows),
        "--days", str(days),
        "--aircraft", str(aircraft),
        "--home-lat", str(home_lat),
        "--home-lon", str(home_lon),
    ]
    if overwrite:
        args.append("--force")
    _run_module("backfill", args)


# ---------------------------------------------------------------------
# Main loop

def _print_main_menu() -> None:
    print()
    print("=" * 50)
    print("Aerodrome Synthetic Feeder")
    print("=" * 50)
    print("  1) Start synthetic feeder server (Mode A)")
    print("     Live JSON feed for the collector to poll.")
    print()
    print("  2) Backfill historical database (Mode B)")
    print("     Bulk-load synthetic sightings at scale.")
    print()
    print("  3) Show README")
    print()
    print("  q) Quit")
    print()


def _show_readme() -> None:
    readme_path = os.path.join(_HERE, "README.md")
    try:
        with open(readme_path, "r", encoding="utf-8") as f:
            print()
            print(f.read())
            print()
            input("(press Enter to continue)")
    except FileNotFoundError:
        print(f"  README not found at {readme_path}")


def main() -> int:
    while True:
        _print_main_menu()
        choice = _prompt("Choice", "1").lower()
        if choice in ("q", "quit", "exit"):
            print("Goodbye.")
            return 0
        if choice == "1":
            _menu_serve()
        elif choice == "2":
            _menu_backfill()
        elif choice == "3":
            _show_readme()
        else:
            print(f"  Unrecognised choice: {choice!r}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(0)
