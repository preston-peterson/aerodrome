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

    // The server decides (severity on /api/status), the same for every page.
    // update_available is gated on updates.github.notify.gear_badge server-side,
    // so respecting it here is correct.
    function classifyHealth(statJ){
        if (statJ.severity === 'error' || statJ.severity === 'warn') return statJ.severity;
        return statJ.update_available === true ? 'warn' : 'ok';
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

    // Two polls in a row without an answer means the server is gone, so the dot
    // goes red. One could just be a slow moment. /api/status can itself wait 5 s on
    // the receiver, so the limit has to be well past that.
    var failures = 0;
    function pollOnce(){
        var ctl = ('AbortController' in window) ? new AbortController() : null;
        var timer = ctl ? setTimeout(function(){ ctl.abort(); }, 15000) : null;
        var opts = ctl ? { signal: ctl.signal } : {};
        fetch('/api/status', opts)
            .then(function(r){
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function(j){
                if (timer) clearTimeout(timer);
                failures = 0;
                applyToGear(classifyHealth(j));
            })
            .catch(function(){
                if (timer) clearTimeout(timer);
                failures += 1;
                if (failures >= 2) applyToGear('error');
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
