/* Links out from an airport: the airport page's "Charts and info" row and the
   Live map's airport panel. Nothing is fetched; each is a plain link, followed
   only when it's clicked. `info` is the `info` of /api/airports/<code> and its
   /summary (static/airport-info.json, through airports.details()). */
(function () {
    // The FAA's plate list for one airport needs the 28-day cycle in effect in
    // its link (AIRAC: cycle 2601 took effect 2026-01-22 at 0901Z, a new one
    // every 28 days). scripts/build_airports.py's airac_cycle() is the same.
    function cycle(now) {
        const STEP = 28 * 86400000, EPOCH = Date.UTC(2026, 0, 22, 9, 1);
        const n = Math.floor((now - EPOCH) / STEP);
        const year = new Date(EPOCH + n * STEP).getUTCFullYear();
        const first = Math.ceil((Date.UTC(year, 0, 1) - EPOCH) / STEP);
        return String(year % 100).padStart(2, '0') + String(n - first + 1).padStart(2, '0');
    }

    // [[label, url], ...]: FAA plates (US airports that have them), SkyVector
    // (every airport), then Wikipedia and the website when known. The FAA and
    // SkyVector want the FAA's own code for a US airport (DFW for KDFW).
    function links(code, info, now) {
        const i = info || {}, id = encodeURIComponent(i.ident || code), out = [];
        if (i.faa_plates) out.push(['FAA plates', 'https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/dtpp/search/results/'
            + '?cycle=' + cycle(now == null ? Date.now() : now) + '&ident=' + id]);
        out.push(['SkyVector', 'https://skyvector.com/airport/' + id]);
        if (i.wikipedia) out.push(['Wikipedia', i.wikipedia]);
        if (i.website) out.push(['Website', i.website]);
        return out;
    }

    window.AerodromeAirportLinks = { cycle: cycle, links: links };
})();
