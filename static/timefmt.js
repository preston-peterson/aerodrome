/* Aerodrome shared time formatting helpers — v2.85.11
 *
 * One source of truth for how times render in the UI. Reads the
 * display.time_format config setting (12h / 24h / auto) from
 * /api/ui-config and exposes a small set of formatter functions that
 * every page should use instead of rolling its own toLocaleString
 * options.
 *
 * Why this file exists: before v2.85.11 each template defined its own
 * inline `fmtTime()` helper with hard-coded `hour12: true` or
 * `hour12: false`. The Live tab was 12-hour, the diagnostics pages
 * were 24-hour, the search results were 24-hour despite being on the
 * same page as the 12-hour Live tab. There was no way for users to
 * pick a single format for the whole app. This module fixes that by
 * centralising the format choice and giving every page the same
 * formatter to call into.
 *
 * Migration plan: this module ships with v2.85.11 and is initially
 * wired into index.html (Live tab times). Subsequent patches migrate
 * other templates one at a time. Pages not yet migrated continue to
 * use their inline formatters and thus ignore the user's preference;
 * they're a known follow-up rather than a bug.
 *
 * Usage:
 *   1. Load this script: <script src="/static/timefmt.js"></script>
 *   2. After fetching /api/ui-config, call:
 *        initTimeFormat(uiConfig.display && uiConfig.display.time_format);
 *   3. Replace inline `new Date(...).toLocaleString(undefined, {hour:..., hour12:...})`
 *      calls with `formatTime(date)` or `formatTimeFull(date)`.
 */

(function() {
    'use strict';

    // Internal flag: true = 12-hour format with AM/PM, false = 24-hour.
    // Determined by initTimeFormat() based on the user's config setting
    // and (for "auto" mode) the browser's locale. Default before init
    // is false (24-hour) — chosen because if init never runs, 24-hour
    // is the unambiguous fallback that won't accidentally show "20:00 PM".
    var _is12Hour = false;

    /**
     * Initialize the time format based on config setting.
     *
     * @param {string} mode - One of "12h", "24h", "auto", or null/undefined.
     *                        Null/undefined and unrecognized values fall back
     *                        to "auto".
     */
    function initTimeFormat(mode) {
        mode = (mode || 'auto').toLowerCase();
        if (mode === '12h') {
            _is12Hour = true;
        } else if (mode === '24h') {
            _is12Hour = false;
        } else {
            // "auto" — respect the browser's locale preference.
            // Intl.DateTimeFormat().resolvedOptions().hourCycle returns
            // "h11" or "h12" for 12-hour locales (US, Canadian English,
            // Australian English, etc) and "h23" or "h24" for 24-hour
            // locales (most of Europe, Japan, ISO scientific).
            try {
                var hc = Intl.DateTimeFormat().resolvedOptions().hourCycle;
                _is12Hour = (hc === 'h11' || hc === 'h12');
            } catch (e) {
                // Older browsers without hourCycle support — fall back
                // to a hour:'numeric' check. If the rendered hour
                // contains "AM" or "PM" the locale is 12-hour.
                try {
                    var sample = new Date(2020, 0, 1, 13, 0).toLocaleTimeString();
                    _is12Hour = /AM|PM/i.test(sample);
                } catch (e2) {
                    _is12Hour = false;
                }
            }
        }
    }

    /**
     * Format a Date or unix-seconds-timestamp as HH:MM (or HH:MM AM/PM).
     * Used for compact time displays — Live tab "last seen" times,
     * recent activity timestamps, anywhere that just needs hour+minute.
     *
     * @param {Date|number} d - Date object or unix-epoch timestamp in seconds
     * @returns {string} Formatted time, or '—' if input is null/invalid
     */
    function formatTime(d) {
        var date = _toDate(d);
        if (!date) return '—';
        return date.toLocaleString(undefined, {
            hour: '2-digit',
            minute: '2-digit',
            hour12: _is12Hour,
        });
    }

    /**
     * Format with seconds: HH:MM:SS (or HH:MM:SS AM/PM).
     * Used for tooltips, sighting tables, anywhere that wants
     * second-level precision.
     */
    function formatTimeFull(d) {
        var date = _toDate(d);
        if (!date) return '—';
        return date.toLocaleString(undefined, {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hour12: _is12Hour,
        });
    }

    /**
     * Format with date prefix: "May 6, 09:54 AM" (or "May 6, 09:54").
     * Used for "last updated" timestamps where the date matters too.
     */
    function formatDateTime(d) {
        var date = _toDate(d);
        if (!date) return '—';
        return date.toLocaleString(undefined, {
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            hour12: _is12Hour,
        });
    }

    /**
     * Coerce input to a Date. Accepts Date instances, unix-seconds
     * (the Aerodrome convention — most timestamps come from the API
     * as integer seconds), or null/undefined for "no time".
     *
     * Heuristic for distinguishing seconds vs milliseconds: any
     * positive number under 10^11 is treated as seconds (years 1970-5138).
     * Anything bigger is treated as milliseconds. This matches what
     * Aerodrome's API actually emits — it never sends millisecond
     * timestamps from server-side code.
     */
    function _toDate(d) {
        if (d == null || d === '') return null;
        if (d instanceof Date) {
            return isNaN(d.getTime()) ? null : d;
        }
        var n = Number(d);
        if (!isFinite(n) || n <= 0) return null;
        // Heuristic: treat values < 1e11 as seconds, otherwise milliseconds.
        // 1e11 seconds = year 5138, 1e11 ms = year 1973. Safe bracket.
        if (n < 1e11) n = n * 1000;
        var date = new Date(n);
        return isNaN(date.getTime()) ? null : date;
    }

    // Expose. Pages call these directly; no module system required.
    window.initTimeFormat = initTimeFormat;
    window.formatTime = formatTime;
    window.formatTimeFull = formatTimeFull;
    window.formatDateTime = formatDateTime;

    // Read-only inspection for diagnostics — useful when debugging
    // "why is this page showing 24h when I picked 12h?" — open the
    // console and check window._timeFormatIs12h.
    Object.defineProperty(window, '_timeFormatIs12h', {
        get: function() { return _is12Hour; },
    });

    // v2.85.12: self-initialize from a server-injected window variable.
    // The server's _serve_template helper injects a tiny <script> block
    // that sets window._aerodromeTimeFormat to the configured display
    // mode just before this script loads. That means by the time any
    // page's inline JS calls formatTime(), the format flag is already
    // resolved — no async fetch latency, no first-render-uses-wrong-
    // format flash. If the variable is missing (e.g. a page that
    // doesn't go through _serve_template), we stay in the default
    // "auto" mode and pages can still call initTimeFormat() manually.
    if (typeof window._aerodromeTimeFormat === 'string') {
        initTimeFormat(window._aerodromeTimeFormat);
    } else {
        initTimeFormat('auto');
    }
})();
