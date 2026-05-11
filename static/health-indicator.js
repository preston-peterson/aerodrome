/* =============================================================================
   Aerodrome global health indicator — gear-icon alert state.
   =============================================================================
   v2.49.3: extracted into a shared file. Previously only index.html polled
   /api/status and applied .warn / .error classes to the gear icon — meaning
   the gear was static (no alert) on every other admin page (Status, Config,
   Logs, Documentation, Diagnostics, Updates). That left users on those
   pages without a visible "something's wrong" signal, which defeats the
   point of having a global indicator.

   Behavior:
     - Polls /api/status every 30 seconds (matches index.html's prior cadence)
     - Reads severity + failing[] from the response
     - Applies .warn / .error / (no class) to .settings-btn accordingly
     - CSS handles the rest — including mirroring the alert color onto the
       "Status" item inside the gear menu via sibling selector (see theme.css).

   Does NOT do:
     - Toasts on transition. Those still live in index.html where the user is
       most likely to be looking when state changes.
     - Health-dot updates. The health dot only exists in the index.html
       header; other admin pages don't have one.

   Skips itself entirely on index.html — index.html has its own richer
   updateHealthIndicator() that does toasts + dot updates. We detect this
   by looking for window.updateHealthIndicator at load time; if defined,
   yield to it.
   ============================================================================= */

(function(){
    // Don't run on pages that already have their own health indicator. We
    // check for the function name rather than a page URL because URLs are
    // implementation detail; the function is the contract.
    if (typeof window.updateHealthIndicator === 'function') return;

    var POLL_INTERVAL_MS = 30000;
    var pollTimer = null;

    // Mirror the classification logic from index.html so the gear's alert
    // state is consistent across pages. If the server's severity is exposed
    // directly on the response (it is, as of v2.41.x), use that; fall back
    // to deriving from components for older server builds.
    function classifyHealth(statJ){
        if (!statJ || typeof statJ !== 'object') return 'ok';
        if (statJ.severity === 'error') return 'error';
        if (statJ.severity === 'warn')  return 'warn';
        // Fallback: derive from components. Receiver / database / collector
        // are critical; hexdb_resolver is advisory only.
        var c = statJ.components || {};
        var critical = ['receiver', 'database', 'collector', 'webserver'];
        for (var i = 0; i < critical.length; i++) {
            if (c[critical[i]] && c[critical[i]].ok === false) return 'error';
        }
        if (c.hexdb_resolver && c.hexdb_resolver.ok === false) return 'warn';
        return 'ok';
    }

    function applyToGear(level){
        var gear = document.querySelector('.settings-btn');
        if (gear) {
            gear.classList.toggle('warn',  level === 'warn');
            gear.classList.toggle('error', level === 'error');
        }
        // v2.49.5: also toggle the header health dot. Mirrors the class
        // names index.html's richer updateHealthIndicator() uses, so the
        // shared theme.css rules drive both paths consistently:
        //   - ok    → bare .dot (green pulse)
        //   - warn  → .dot.warn (amber pulse)
        //   - error → .dot.off  (solid red, no animation)
        // Previously this function only updated the gear button — which
        // left the dot stuck on green-pulsing on every admin page even
        // when the gear correctly turned amber/red. Fixed in v2.49.5.
        var dot = document.getElementById('healthDot');
        if (dot) {
            dot.classList.toggle('warn', level === 'warn');
            dot.classList.toggle('off',  level === 'error');
        }
    }

    function pollOnce(){
        // Fetch with a short timeout — if /api/status is slow we don't want
        // to delay the next poll. AbortController is widely supported.
        var ctl = ('AbortController' in window) ? new AbortController() : null;
        var timer = ctl ? setTimeout(function(){ ctl.abort(); }, 5000) : null;
        var opts = ctl ? { signal: ctl.signal } : {};
        fetch('/api/status', opts)
            .then(function(r){ return r.ok ? r.json() : null; })
            .then(function(j){
                if (timer) clearTimeout(timer);
                applyToGear(classifyHealth(j));
            })
            .catch(function(){
                // Silent: a failed /api/status fetch is itself an indicator
                // that something's wrong, but we don't want to spam errors
                // into the console on a transient hiccup. Leave the previous
                // class state in place; next poll will retry.
                if (timer) clearTimeout(timer);
            });
    }

    function startPolling(){
        // Initial poll immediately so the gear reflects state on page load.
        // Subsequent polls every POLL_INTERVAL_MS.
        pollOnce();
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', startPolling);
    } else {
        startPolling();
    }
})();
