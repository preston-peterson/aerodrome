"""
config_validator.py — Strict field-level validation for config.yaml updates.

Every validator returns a list of (path, message) error tuples.
Empty list means the value is valid.

Usage:
    errors = validate_config(new_config)
    if errors:
        # each error: {"path": "receiver.port", "message": "Must be 1-65535"}
        return error_response(errors)
"""
# Version: 3.1.0

import re
from typing import Any, List, Tuple

Errors = List[Tuple[str, str]]


# --- Small type helpers ---

def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool))


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


# --- Field validators ---

IPV4_RE = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')
HOSTNAME_RE = re.compile(
    r'^(([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9\-]*[a-zA-Z0-9])\.)*'
    r'([A-Za-z]|[A-Za-z][A-Za-z0-9\-]*[A-Za-z0-9])$'
)


def _validate_ip_or_hostname(value: Any, path: str) -> Errors:
    errs: Errors = []
    if not _is_str(value) or not value.strip():
        return [(path, "Required")]
    v = value.strip()
    if IPV4_RE.match(v):
        parts = [int(p) for p in v.split('.')]
        if all(0 <= p <= 255 for p in parts):
            return []
        return [(path, "Invalid IP address — each octet must be 0-255")]
    # Allow hostnames too (localhost, my-server.local, etc.)
    if HOSTNAME_RE.match(v):
        return []
    return [(path, "Must be a valid IPv4 address or hostname")]


def _validate_port(value: Any, path: str) -> Errors:
    if not _is_int(value):
        return [(path, "Must be a whole number")]
    if value < 1 or value > 65535:
        return [(path, "Must be between 1 and 65535")]
    return []


def _validate_path_str(value: Any, path: str, must_start_with_slash: bool = True) -> Errors:
    if not _is_str(value) or not value.strip():
        return [(path, "Required")]
    v = value.strip()
    if must_start_with_slash and not v.startswith('/'):
        return [(path, "Must start with '/' (e.g., /data/aircraft.json)")]
    return []


def _validate_poll_interval(value: Any, path: str) -> Errors:
    if not _is_int(value):
        return [(path, "Must be a whole number of seconds")]
    if value < 5:
        return [(path, "Too aggressive — minimum 5 seconds")]
    if value > 3600:
        return [(path, "Too slow — maximum 3600 seconds (1 hour)")]
    return []


def _validate_latitude(value: Any, path: str) -> Errors:
    if value is None:
        return []  # null disables distance column — allowed
    if not _is_num(value):
        return [(path, "Must be a number or null")]
    if value < -90 or value > 90:
        return [(path, "Must be between -90 and 90")]
    return []


def _validate_longitude(value: Any, path: str) -> Errors:
    if value is None:
        return []
    if not _is_num(value):
        return [(path, "Must be a number or null")]
    if value < -180 or value > 180:
        return [(path, "Must be between -180 and 180")]
    return []


def _validate_distance_unit(value: Any, path: str) -> Errors:
    if not _is_str(value):
        return [(path, "Required")]
    if value.lower() not in ("mi", "nmi", "km"):
        return [(path, "Must be one of: mi, nmi, km")]
    return []


# Known providers for the "Track ↗" link on aircraft rows. Kept as a module-
# level tuple so the set of valid values is defined in exactly one place —
# the validator, the /api/ui-config pass-through, and the config.html
# dropdown all reference the same list. Adding a new provider is a matter
# of adding it here, teaching the frontend's trackLink() helper how to
# build the URL, and adding an <option> to the dropdown.
TRACK_LINK_PROVIDERS = (
    "airplanes_live",
    "flightaware",
    "flightradar24",
    "airnavradar",
    "planefinder",
)


def _validate_track_link_provider(value: Any, path: str) -> Errors:
    # Missing / None / empty string is treated as "use the default", not an
    # error — the frontend falls back to airplanes_live. This mirrors the
    # handling of other optional receiver fields.
    if value is None or value == "":
        return []
    if not _is_str(value):
        return [(path, "Must be a string")]
    if value not in TRACK_LINK_PROVIDERS:
        return [(path, f"Must be one of: {', '.join(TRACK_LINK_PROVIDERS)}")]
    return []


def _validate_host(value: Any, path: str) -> Errors:
    if not _is_str(value) or not value.strip():
        return [(path, "Required")]
    v = value.strip()
    # 0.0.0.0 (any), 127.0.0.1 (localhost), specific IP, or hostname all valid
    return []


def _validate_retention_days(value: Any, path: str) -> Errors:
    if not _is_int(value):
        return [(path, "Must be a whole number of days")]
    if value < 1:
        return [(path, "Must be at least 1 day")]
    if value > 3650:
        return [(path, "Unrealistic — maximum 3650 days (10 years)")]
    return []


def _validate_log_level(value: Any, path: str) -> Errors:
    if not _is_str(value):
        return [(path, "Required")]
    valid = {"DEBUG", "INFO", "WARNING", "ERROR"}
    if value.upper() not in valid:
        return [(path, f"Must be one of: {', '.join(sorted(valid))}")]
    return []


def _validate_non_empty_string(value: Any, path: str) -> Errors:
    if not _is_str(value) or not value.strip():
        return [(path, "Required")]
    return []


def _validate_icao_hex(value: Any, path: str) -> Errors:
    if not _is_str(value):
        return [(path, "Must be a string")]
    v = value.strip().upper()
    if not re.match(r'^[0-9A-F]{6}$', v):
        return [(path, "Must be exactly 6 hex characters (e.g., A835D2)")]
    return []


def _validate_color(value: Any, path: str) -> Errors:
    if not _is_str(value):
        return [(path, "Must be a hex color")]
    v = value.strip()
    if not re.match(r'^#[0-9A-Fa-f]{6}$', v):
        return [(path, "Must be a hex color like #3b82f6")]
    return []


def _validate_string_list(value: Any, path: str, allow_empty: bool = True) -> Errors:
    if value is None:
        return [] if allow_empty else [(path, "Required")]
    if not isinstance(value, list):
        return [(path, "Must be a list")]
    errs: Errors = []
    for i, item in enumerate(value):
        if not _is_str(item) or not item.strip():
            errs.append((f"{path}[{i}]", "Each item must be a non-empty string"))
    return errs


def _validate_hhmm(value: Any, path: str) -> Errors:
    """HH:MM in 24-hour format. 00:00 through 23:59."""
    if not _is_str(value):
        return [(path, "Must be a string in HH:MM format")]
    import re
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value):
        return [(path, "Must be HH:MM (00:00–23:59)")]
    return []


def _validate_url_scheme(value: Any, path: str, required: bool = False) -> Errors:
    """Basic check that a URL starts with http:// or https:// and has a host.
    Empty string is allowed when required=False (treated as 'not configured')."""
    if value is None or (isinstance(value, str) and value == ""):
        return [] if not required else [(path, "Required")]
    if not _is_str(value):
        return [(path, "Must be a string")]
    v = value.strip()
    if not (v.startswith("http://") or v.startswith("https://")):
        return [(path, "Must start with http:// or https://")]
    # After the scheme there should be at least a host character
    rest = v.split("://", 1)[1]
    if not rest or rest.startswith("/"):
        return [(path, "URL must include a host (e.g. http://localhost:2586/topic)")]
    return []


def _validate_ntfy_priority(value: Any, path: str) -> Errors:
    if value is None:
        return []  # optional, defaults to "default"
    if not _is_str(value):
        return [(path, "Must be a string")]
    valid = {"min", "low", "default", "high", "max"}
    if value not in valid:
        return [(path, f"Must be one of: {', '.join(sorted(valid))}")]
    return []


# --- Top-level validator ---

def validate_config(cfg: Any) -> Errors:
    """Run every field-level check. Returns list of (path, message) errors."""
    errs: Errors = []

    if not isinstance(cfg, dict):
        return [("", "Config must be a YAML mapping/object")]

    # --- receiver ---
    r = cfg.get("receiver")
    if not isinstance(r, dict):
        errs.append(("receiver", "Section is required"))
    else:
        errs += _validate_ip_or_hostname(r.get("ip"), "receiver.ip")
        errs += _validate_port(r.get("port"), "receiver.port")
        errs += _validate_path_str(r.get("path"), "receiver.path")
        errs += _validate_poll_interval(r.get("poll_interval"), "receiver.poll_interval")
        errs += _validate_latitude(r.get("latitude"), "receiver.latitude")
        errs += _validate_longitude(r.get("longitude"), "receiver.longitude")
        errs += _validate_distance_unit(r.get("distance_unit", "mi"), "receiver.distance_unit")
        errs += _validate_track_link_provider(r.get("track_link_provider"), "receiver.track_link_provider")
        # Cross-field: if one of lat/lon is set, both must be
        if (r.get("latitude") is None) != (r.get("longitude") is None):
            errs.append(("receiver.latitude",
                         "Set both latitude and longitude (or leave both empty)"))

    # --- web ---
    w = cfg.get("web")
    if not isinstance(w, dict):
        errs.append(("web", "Section is required"))
    else:
        errs += _validate_host(w.get("host"), "web.host")
        errs += _validate_port(w.get("port"), "web.port")

    # --- display (v2.52.0; v2.85.11 added time_format) ---
    # Optional section. If present, validates date_format and time_format.
    # If absent or missing keys, defaults are applied at read-time
    # (date_format=MDY, time_format=auto). We don't reject installs that
    # don't have the section yet — they work on the defaults, and the
    # example-config-merger adds the section on next restart.
    disp = cfg.get("display")
    if disp is not None:
        if not isinstance(disp, dict):
            errs.append(("display", "Must be a mapping"))
        else:
            df = disp.get("date_format")
            if df is not None:
                valid_date_formats = {"MDY", "DMY", "ISO"}
                if df not in valid_date_formats:
                    errs.append((
                        "display.date_format",
                        f"Must be one of {sorted(valid_date_formats)}; got {df!r}"
                    ))
            tf = disp.get("time_format")
            if tf is not None:
                valid_time_formats = {"auto", "12h", "24h"}
                if tf not in valid_time_formats:
                    errs.append((
                        "display.time_format",
                        f"Must be one of {sorted(valid_time_formats)}; got {tf!r}"
                    ))

    # --- retention ---
    ret = cfg.get("retention")
    if not isinstance(ret, dict):
        errs.append(("retention", "Section is required"))
    else:
        for key in ("military_days", "watchlist_days", "all_days"):
            errs += _validate_retention_days(ret.get(key), f"retention.{key}")

    # --- data ---
    d = cfg.get("data")
    if not isinstance(d, dict):
        errs.append(("data", "Section is required"))
    else:
        errs += _validate_non_empty_string(d.get("db_file"), "data.db_file")
        # v2.50.13: SQLite tuning profile (optional block)
        tuning = d.get("tuning")
        if tuning is not None:
            if not isinstance(tuning, dict):
                errs.append(("data.tuning", "Must be a mapping"))
            else:
                profile = tuning.get("profile")
                if profile is not None:
                    valid_profiles = {"auto", "default", "conservative",
                                      "balanced", "aggressive", "high_memory"}
                    if profile not in valid_profiles:
                        errs.append((
                            "data.tuning.profile",
                            f"Must be one of: {', '.join(sorted(valid_profiles))}"
                        ))

    # --- logging ---
    lg = cfg.get("logging")
    if not isinstance(lg, dict):
        errs.append(("logging", "Section is required"))
    else:
        errs += _validate_log_level(lg.get("level"), "logging.level")
        errs += _validate_non_empty_string(lg.get("dir"), "logging.dir")

    # --- military ---
    m = cfg.get("military")
    if m is not None:
        if not isinstance(m, dict):
            errs.append(("military", "Must be a mapping"))
        else:
            if "use_db_flags" in m and not isinstance(m["use_db_flags"], bool):
                errs.append(("military.use_db_flags", "Must be true or false"))
            if "default_color" in m:
                errs += _validate_color(m["default_color"], "military.default_color")
            errs += _validate_string_list(m.get("callsign_prefixes"), "military.callsign_prefixes")
            errs += _validate_string_list(m.get("icao_prefixes"), "military.icao_prefixes")

            sa = m.get("special_aircraft")
            if sa is not None:
                if not isinstance(sa, dict):
                    errs.append(("military.special_aircraft", "Must be a mapping"))
                else:
                    for icao, spec in sa.items():
                        errs += _validate_icao_hex(icao, f"military.special_aircraft.{icao}")
                        if not isinstance(spec, dict):
                            errs.append((f"military.special_aircraft.{icao}",
                                         "Each entry must have 'label' and 'color'"))
                            continue
                        errs += _validate_non_empty_string(
                            spec.get("label"),
                            f"military.special_aircraft.{icao}.label")
                        errs += _validate_color(
                            spec.get("color"),
                            f"military.special_aircraft.{icao}.color")

    # --- watchlist ---
    wl = cfg.get("watchlist")
    if wl is not None and wl != []:
        if not isinstance(wl, list):
            errs.append(("watchlist", "Must be a list"))
        else:
            for i, entry in enumerate(wl):
                if not isinstance(entry, dict):
                    errs.append((f"watchlist[{i}]", "Each entry must be a mapping"))
                    continue
                identifier_keys = [k for k in ("icao", "tail", "callsign", "model") if k in entry]
                if not identifier_keys:
                    errs.append((f"watchlist[{i}]",
                                 "Each entry needs one of: icao, tail, callsign, model"))
                if "icao" in entry:
                    errs += _validate_icao_hex(entry["icao"], f"watchlist[{i}].icao")
                if "tail" in entry and not _is_str(entry["tail"]):
                    errs.append((f"watchlist[{i}].tail", "Must be a string"))
                if "callsign" in entry and not _is_str(entry["callsign"]):
                    errs.append((f"watchlist[{i}].callsign", "Must be a string"))
                if "model" in entry:
                    if not _is_str(entry["model"]) or not entry["model"].strip():
                        errs.append((f"watchlist[{i}].model",
                                     "Must be a non-empty string"))

    # --- watchlist_alerts ---
    wa = cfg.get("watchlist_alerts")
    if wa is not None:
        if not isinstance(wa, dict):
            errs.append(("watchlist_alerts", "Must be a mapping"))
        else:
            if "enabled" in wa and not isinstance(wa["enabled"], bool):
                errs.append(("watchlist_alerts.enabled", "Must be true or false"))
            if "trigger" in wa:
                # v2.50.23: 'new' is kept in the valid set for backward
                # compatibility — its behavior is identical to
                # 'continuous_dismissable' and the API translates it
                # transparently. Validator must continue accepting it
                # so existing config.yaml files don't fail validation
                # after upgrade. Removing it from the valid set would
                # break any user who upgraded without editing config.
                valid = {"new", "continuous", "continuous_dismissable", "live"}
                if wa["trigger"] not in valid:
                    errs.append(("watchlist_alerts.trigger",
                                 f"Must be one of: {', '.join(sorted(valid))}"))
            if "effect" in wa:
                valid = {"pulse_dot", "pulse", "dot", "flash"}
                if wa["effect"] not in valid:
                    errs.append(("watchlist_alerts.effect",
                                 f"Must be one of: {', '.join(sorted(valid))}"))
            if "color" in wa:
                errs += _validate_color(wa["color"], "watchlist_alerts.color")

    # --- stats ---
    st = cfg.get("stats")
    if st is not None:
        if not isinstance(st, dict):
            errs.append(("stats", "Must be a mapping"))
        else:
            if "enabled" in st and not isinstance(st["enabled"], bool):
                errs.append(("stats.enabled", "Must be true or false"))
            if "timezone" in st:
                tz = st["timezone"]
                if tz not in (None, "") and not isinstance(tz, str):
                    errs.append(("stats.timezone", "Must be a string (IANA timezone) or blank"))
                elif isinstance(tz, str) and tz != "":
                    # Validate with zoneinfo if present
                    try:
                        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                        try:
                            ZoneInfo(tz)
                        except ZoneInfoNotFoundError:
                            errs.append(("stats.timezone",
                                         f"Unknown timezone: {tz!r} (use IANA name like 'America/Los_Angeles')"))
                    except ImportError:
                        pass  # older Python, skip validation
            if "refresh_interval" in st:
                ri = st["refresh_interval"]
                if not isinstance(ri, int) or isinstance(ri, bool):
                    errs.append(("stats.refresh_interval", "Must be an integer (seconds)"))
                elif ri != 0 and (ri < 30 or ri > 3600):
                    errs.append(("stats.refresh_interval",
                                 "Must be 0 (disabled) or between 30 and 3600 seconds"))
            if "track_gap_minutes" in st:
                # Gap threshold for "longest continuous track" detection.
                # 1-60 min is the useful range — below 1 splits legitimate
                # tracks on normal signal fluctuation; above 60 defeats the
                # purpose of gap detection.
                tg = st["track_gap_minutes"]
                if not isinstance(tg, int) or isinstance(tg, bool):
                    errs.append(("stats.track_gap_minutes", "Must be an integer (minutes)"))
                elif tg < 1 or tg > 60:
                    errs.append(("stats.track_gap_minutes",
                                 "Must be between 1 and 60 minutes"))
            if "cards" in st:
                if not isinstance(st["cards"], dict):
                    errs.append(("stats.cards", "Must be a mapping of card_name: bool"))
                else:
                    for cname, cval in st["cards"].items():
                        if not isinstance(cval, bool):
                            errs.append((f"stats.cards.{cname}", "Must be true or false"))
            if "groups" in st:
                if not isinstance(st["groups"], dict):
                    errs.append(("stats.groups", "Must be a mapping of group_name: bool"))
                else:
                    for gname, gval in st["groups"].items():
                        if not isinstance(gval, bool):
                            errs.append((f"stats.groups.{gname}", "Must be true or false"))
            if "groups_order" in st:
                go = st["groups_order"]
                if not isinstance(go, list):
                    errs.append(("stats.groups_order", "Must be a list of group ids"))
                else:
                    for i, g in enumerate(go):
                        if not isinstance(g, str):
                            errs.append((f"stats.groups_order[{i}]", "Must be a string (group id)"))
            if "range_rose" in st:
                rr = st["range_rose"]
                if not isinstance(rr, dict):
                    errs.append(("stats.range_rose", "Must be a mapping"))
                else:
                    if "window" in rr:
                        valid_windows = {"today", "7d", "30d", "all_time", "custom"}
                        if rr["window"] not in valid_windows:
                            errs.append(("stats.range_rose.window",
                                         f"Must be one of: {', '.join(sorted(valid_windows))}"))
                    if "window_custom_days" in rr:
                        d = rr["window_custom_days"]
                        if not isinstance(d, int) or isinstance(d, bool):
                            errs.append(("stats.range_rose.window_custom_days",
                                         "Must be an integer (days)"))
                        elif d < 1 or d > 365:
                            errs.append(("stats.range_rose.window_custom_days",
                                         "Must be between 1 and 365 days"))
                    if "distance_buckets" in rr:
                        db = rr["distance_buckets"]
                        if not isinstance(db, list):
                            errs.append(("stats.range_rose.distance_buckets",
                                         "Must be a list of numbers"))
                        elif len(db) < 2 or len(db) > 10:
                            errs.append(("stats.range_rose.distance_buckets",
                                         "Must have between 2 and 10 entries"))
                        else:
                            prev = 0
                            ok = True
                            for i, v in enumerate(db):
                                if not isinstance(v, (int, float)) or isinstance(v, bool):
                                    errs.append((f"stats.range_rose.distance_buckets[{i}]",
                                                 "Must be a positive number"))
                                    ok = False
                                    break
                                if v <= prev:
                                    errs.append(("stats.range_rose.distance_buckets",
                                                 "Entries must be strictly increasing positive numbers"))
                                    ok = False
                                    break
                                prev = v

            if "new_record_alerts" in st:
                nra = st["new_record_alerts"]
                if not isinstance(nra, dict):
                    errs.append(("stats.new_record_alerts", "Must be a mapping"))
                else:
                    if "enabled" in nra and not isinstance(nra["enabled"], bool):
                        errs.append(("stats.new_record_alerts.enabled",
                                     "Must be true or false"))
                    if "color" in nra:
                        errs += _validate_color(nra["color"],
                                                "stats.new_record_alerts.color")
                    if "dismiss_after_seconds" in nra:
                        d = nra["dismiss_after_seconds"]
                        if not isinstance(d, int) or isinstance(d, bool):
                            errs.append(("stats.new_record_alerts.dismiss_after_seconds",
                                         "Must be an integer (seconds)"))
                        elif d < 1 or d > 600:
                            errs.append(("stats.new_record_alerts.dismiss_after_seconds",
                                         "Must be between 1 and 600 seconds"))

    # --- all_tab ---
    # v2.40.1: pagination config for the (since-removed) All tab. Kept
    # in the validator for backwards-compat — old configs that still
    # have an `all_tab:` block don't fail validation. The Search tab
    # uses a separate `search:` config block; see the search section
    # below.
    at = cfg.get("all_tab")
    if at is not None:
        if not isinstance(at, dict):
            errs.append(("all_tab", "Must be a mapping"))
        else:
            if "default_page_size" in at:
                dps = at["default_page_size"]
                if isinstance(dps, bool) or not isinstance(dps, int):
                    errs.append(("all_tab.default_page_size",
                                 "Must be an integer"))
                elif dps < 10 or dps > 10000:
                    errs.append(("all_tab.default_page_size",
                                 "Must be between 10 and 10000"))

    # --- notifications ---
    # Optional section. If present and notifications.enabled=true, the url
    # must be valid. Event toggles must be booleans; cooldowns must be
    # non-negative ints; quiet-hours times must be HH:MM; priority must be
    # a valid ntfy value.
    nt = cfg.get("notifications")
    if nt is not None:
        if not isinstance(nt, dict):
            errs.append(("notifications", "Must be a mapping"))
        else:
            if "enabled" in nt and not isinstance(nt["enabled"], bool):
                errs.append(("notifications.enabled", "Must be true or false"))

            # URL — required when enabled, optional when disabled
            url_required = bool(nt.get("enabled"))
            errs += _validate_url_scheme(
                nt.get("url"), "notifications.url", required=url_required
            )

            # v2.43.0: public_url — always optional. When set, enables tap-to-open
            # actions on notifications. Empty string is fine (disables the feature);
            # any non-empty value must be a valid http(s) URL so the notifier
            # doesn't post a broken Click header that the ntfy app refuses to
            # render.
            pu = nt.get("public_url")
            if pu is not None and pu != "":
                errs += _validate_url_scheme(
                    pu, "notifications.public_url", required=False
                )

            errs += _validate_ntfy_priority(nt.get("priority"), "notifications.priority")

            # Events — each must be a bool if present
            ev = nt.get("events")
            if ev is not None:
                if not isinstance(ev, dict):
                    errs.append(("notifications.events", "Must be a mapping"))
                else:
                    known_events = {
                        "receiver_offline", "receiver_recovered", "watchlist_hit",
                        "new_record", "special_aircraft", "daily_summary",
                        # v2.48.0: fires when an aircraft becomes visible with
                        # an emergency squawk (7500/7600/7700). Default off in
                        # the example config.
                        "emergency_squawk",
                        # v2.50.31: capacity threshold alerts.
                        "capacity_low", "capacity_recovered",
                        # v3.0.2: ntfy push when a new release is discovered.
                        # v3.0.8: added here after a missed-mirror bug — the
                        # event was registered in notifier.py's KNOWN_EVENTS in
                        # v3.0.2 but this validator set wasn't updated, so any
                        # user trying to enable `notifications.events.update_available`
                        # (which v3.0.7's own Updates-tab helper text instructs
                        # them to do) hit "Unknown event type" on save.
                        "update_available",
                    }
                    for k, v in ev.items():
                        if k not in known_events:
                            errs.append((f"notifications.events.{k}",
                                         f"Unknown event type (known: {', '.join(sorted(known_events))})"))
                        elif not isinstance(v, bool):
                            errs.append((f"notifications.events.{k}", "Must be true or false"))

            # Cooldowns — per-event minutes
            cd = nt.get("cooldown_minutes")
            if cd is not None:
                if not isinstance(cd, dict):
                    errs.append(("notifications.cooldown_minutes", "Must be a mapping"))
                else:
                    for k, v in cd.items():
                        if isinstance(v, bool) or not isinstance(v, int):
                            errs.append((f"notifications.cooldown_minutes.{k}",
                                         "Must be an integer (minutes)"))
                        elif v < 0 or v > 1440:
                            errs.append((f"notifications.cooldown_minutes.{k}",
                                         "Must be between 0 and 1440 minutes"))

            # Rate limit (per hour)
            if "rate_limit_per_hour" in nt:
                rl = nt["rate_limit_per_hour"]
                if isinstance(rl, bool) or not isinstance(rl, int):
                    errs.append(("notifications.rate_limit_per_hour",
                                 "Must be an integer (0 = unlimited)"))
                elif rl < 0 or rl > 1000:
                    errs.append(("notifications.rate_limit_per_hour",
                                 "Must be between 0 and 1000"))

            # Quiet hours
            qh = nt.get("quiet_hours")
            if qh is not None:
                if not isinstance(qh, dict):
                    errs.append(("notifications.quiet_hours", "Must be a mapping"))
                else:
                    if "enabled" in qh and not isinstance(qh["enabled"], bool):
                        errs.append(("notifications.quiet_hours.enabled",
                                     "Must be true or false"))
                    if "start" in qh:
                        errs += _validate_hhmm(qh["start"], "notifications.quiet_hours.start")
                    if "end" in qh:
                        errs += _validate_hhmm(qh["end"], "notifications.quiet_hours.end")

            # Receiver offline threshold
            ro = nt.get("receiver_offline")
            if ro is not None:
                if not isinstance(ro, dict):
                    errs.append(("notifications.receiver_offline", "Must be a mapping"))
                else:
                    cfp = ro.get("consecutive_failed_polls")
                    if cfp is not None:
                        if isinstance(cfp, bool) or not isinstance(cfp, int):
                            errs.append(("notifications.receiver_offline.consecutive_failed_polls",
                                         "Must be an integer"))
                        elif cfp < 1 or cfp > 1000:
                            errs.append(("notifications.receiver_offline.consecutive_failed_polls",
                                         "Must be between 1 and 1000"))

            # Daily summary time
            ds = nt.get("daily_summary")
            if ds is not None:
                if not isinstance(ds, dict):
                    errs.append(("notifications.daily_summary", "Must be a mapping"))
                elif "time" in ds:
                    errs += _validate_hhmm(ds["time"], "notifications.daily_summary.time")

            # v2.50.31: capacity alert thresholds
            cap = nt.get("capacity")
            if cap is not None:
                if not isinstance(cap, dict):
                    errs.append(("notifications.capacity", "Must be a mapping"))
                else:
                    en = cap.get("enabled")
                    if en is not None and not isinstance(en, bool):
                        errs.append(("notifications.capacity.enabled",
                                     "Must be true or false"))
                    rn = cap.get("recovery_notification")
                    if rn is not None and not isinstance(rn, bool):
                        errs.append(("notifications.capacity.recovery_notification",
                                     "Must be true or false"))
                    ht = cap.get("headroom_threshold")
                    if ht is not None:
                        if isinstance(ht, bool) or not isinstance(ht, (int, float)):
                            errs.append(("notifications.capacity.headroom_threshold",
                                         "Must be a number"))
                        elif ht < 1.0 or ht > 100.0:
                            errs.append(("notifications.capacity.headroom_threshold",
                                         "Must be between 1.0 and 100.0 (typical: 1.2 to 2.0)"))
                    fl = cap.get("disk_free_floor_mb")
                    if fl is not None:
                        if isinstance(fl, bool) or not isinstance(fl, (int, float)):
                            errs.append(("notifications.capacity.disk_free_floor_mb",
                                         "Must be a number (megabytes)"))
                        elif fl < 0 or fl > 1024 * 1024:  # 1 TB ceiling
                            errs.append(("notifications.capacity.disk_free_floor_mb",
                                         "Must be between 0 and 1048576 MB"))
                    pf = cap.get("disk_free_pct_floor")
                    if pf is not None:
                        if isinstance(pf, bool) or not isinstance(pf, (int, float)):
                            errs.append(("notifications.capacity.disk_free_pct_floor",
                                         "Must be a number (fraction, e.g. 0.05 for 5%)"))
                        elif pf < 0 or pf > 1:
                            errs.append(("notifications.capacity.disk_free_pct_floor",
                                         "Must be between 0 and 1"))

    # --- updates (v3.0.0+: GitHub Releases-based update channel) ---
    # Optional section. If absent, defaults apply at read-time (enabled=true,
    # poll_interval=monthly, notify.banner=true, notify.gear_badge=true).
    # Pre-v3.0.0 configs that lack the section still validate cleanly.
    upd = cfg.get("updates")
    if upd is not None:
        if not isinstance(upd, dict):
            errs.append(("updates", "Must be a mapping"))
        else:
            gh = upd.get("github")
            if gh is not None:
                if not isinstance(gh, dict):
                    errs.append(("updates.github", "Must be a mapping"))
                else:
                    en = gh.get("enabled")
                    if en is not None and not isinstance(en, bool):
                        errs.append(("updates.github.enabled",
                                     "Must be a boolean (true or false)"))
                    pi = gh.get("poll_interval")
                    if pi is not None:
                        valid_intervals = {"daily", "weekly", "monthly", "never"}
                        if pi not in valid_intervals:
                            errs.append(("updates.github.poll_interval",
                                         f"Must be one of {sorted(valid_intervals)}; got {pi!r}"))
                    nt = gh.get("notify")
                    if nt is not None:
                        if not isinstance(nt, dict):
                            errs.append(("updates.github.notify", "Must be a mapping"))
                        else:
                            for key in ("banner", "gear_badge", "ntfy"):
                                v = nt.get(key)
                                if v is not None and not isinstance(v, bool):
                                    errs.append((f"updates.github.notify.{key}",
                                                 "Must be a boolean (true or false)"))

    # --- cross-cutting: update push notifications require ntfy to be usable ---
    # v3.0.10: refuse `updates.github.notify.ntfy: true` when the underlying
    # ntfy infrastructure isn't configured to fire. The Updates-tab UI lets
    # users toggle this flag without leaving for the Notifications tab, which
    # made it easy to end up in a "I enabled push but nothing happens" dead
    # state: the flag is true, but `notifications.enabled` is false or
    # `notifications.url` is empty, so the notifier never sends. Two cases
    # produce that dead state and both surface here:
    #   1. notifications.enabled is missing/false → ntfy master switch is off
    #   2. notifications.url is missing/empty → no ntfy server to post to
    # Validation runs only when the user is actively trying to enable the
    # update-push flag; existing installs with the flag at default (false)
    # are unaffected. Error path lives on the Updates tab so the UI's
    # auto-switch-to-error-tab logic delivers the user to the field they
    # toggled; the message tells them exactly which Notifications-tab keys
    # to fix before retrying. Same pattern used by the receiver lat/lon
    # cross-check above (single error, points at one path).
    upd = cfg.get("updates")
    nt_cfg = cfg.get("notifications")
    if isinstance(upd, dict):
        gh = upd.get("github")
        if isinstance(gh, dict):
            notify = gh.get("notify")
            if isinstance(notify, dict) and notify.get("ntfy") is True:
                nt_enabled = isinstance(nt_cfg, dict) and bool(nt_cfg.get("enabled"))
                nt_url = (nt_cfg or {}).get("url") if isinstance(nt_cfg, dict) else None
                nt_url_set = isinstance(nt_url, str) and nt_url.strip() != ""
                if not nt_enabled or not nt_url_set:
                    # v3.0.11: shorter message + clickable link to the
                    # Notifications tab. Adaptive copy names specifically
                    # what's missing so the user knows exactly what to fix
                    # after one click. The error message renders as HTML
                    # in the /config error-display path (errMsg gets
                    # injected via template literal, not escaped) which
                    # makes the <a> tag clickable; /config#notifications
                    # uses the v2.42.1 URL-hash tab-routing to land the
                    # user directly on the Notifications tab.
                    if not nt_enabled and not nt_url_set:
                        what = "turn ntfy on and set a URL"
                    elif not nt_enabled:
                        what = "turn ntfy on"
                    else:
                        what = "set a URL"
                    errs.append((
                        "updates.github.notify.ntfy",
                        f"Configure ntfy on the "
                        f'<a href="/config#notifications">Notifications tab</a>'
                        f": {what}."
                    ))

    # --- demo (v3.1.0: demo mode with synthetic feeder) ---
    # Optional section. demo.enabled defaults to false at read-time, so
    # existing configs without the section validate cleanly. The flag is
    # set by the bootstrap when --demo is chosen at install time, and
    # flipped to false by the switch-to-real wizard. Users shouldn't edit
    # this in YAML — the wizard handles the full transition including
    # stopping the feeder service, clearing the demo DB, and clearing the
    # demo watchlist (see CHANGELOG and config.yaml.example commentary).
    demo = cfg.get("demo")
    if demo is not None:
        if not isinstance(demo, dict):
            errs.append(("demo", "Must be a mapping"))
        else:
            en = demo.get("enabled")
            if en is not None and not isinstance(en, bool):
                errs.append(("demo.enabled",
                             "Must be a boolean (true or false)"))

    return errs
# The collector re-reads these from CONFIG on each poll interval.
LIVE_KEYS = {
    "retention.military_days",
    "retention.watchlist_days",
    "retention.all_days",
    "receiver.latitude",
    "receiver.longitude",
    "receiver.distance_unit",
    "receiver.track_link_provider",
    "military.use_db_flags",
    "military.callsign_prefixes",
    "military.icao_prefixes",
    "military.special_aircraft",
    "watchlist",
    "watchlist_alerts",
    "stats",
    "notifications",
    "all_tab",
    # v3.0.0: GitHub-update-channel config. The scheduler wakes at most
    # hourly and re-reads CONFIG, so poll_interval changes (and enabled
    # toggle, and notify flags) take effect within an hour without restart.
    "updates",
    # v2.52.0: date format preference is purely cosmetic (controls
    # which date inputs the search parser accepts and how dates render).
    # No service-state implications — live-reloadable.
    "display.date_format",
    # v2.85.11: time format preference (12h / 24h / auto). Frontend-only,
    # consumed by static/timefmt.js to drive the format helpers used by
    # the Live tab and (future patches) other time displays. "auto"
    # respects the user's browser locale.
    "display.time_format",
}

# Everything else requires a restart. The frontend uses this list to show
# the "Restart required" banner when the user edits a restart-only setting.
RESTART_KEYS = {
    "receiver.ip",
    "receiver.port",
    "receiver.path",
    "receiver.poll_interval",
    "web.host",
    "web.port",
    "data.db_file",
    "logging.level",
    "logging.dir",
}


def diff_keys(old: dict, new: dict, prefix: str = "") -> List[str]:
    """Return dotted paths of keys whose values differ between old and new.
    Top-level sections and nested dicts are walked; lists/scalars are compared whole."""
    changed = []
    keys = set((old or {}).keys()) | set((new or {}).keys())
    for k in keys:
        path = f"{prefix}.{k}" if prefix else k
        ov = (old or {}).get(k)
        nv = (new or {}).get(k)
        if isinstance(ov, dict) and isinstance(nv, dict):
            changed.extend(diff_keys(ov, nv, path))
        elif ov != nv:
            changed.append(path)
    return changed


def requires_restart(changed_paths: List[str]) -> bool:
    """True if any changed path is NOT in LIVE_KEYS (i.e., needs restart)."""
    for path in changed_paths:
        # A nested change under a LIVE_KEYS section still counts as live
        # e.g., military.special_aircraft.A1B2C3.label → matches military.special_aircraft
        matches_live = any(
            path == lk or path.startswith(lk + ".")
            for lk in LIVE_KEYS
        )
        if not matches_live:
            return True
    return False
