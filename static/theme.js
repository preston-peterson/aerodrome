/* =============================================================================
   Aerodrome theme toggle — shared across all admin templates.
   =============================================================================
   Exposes window.aerodromeSetTheme(mode) where mode is 'auto' | 'dark' | 'light'.
   The gear-menu buttons call this directly via onclick. Preference is stored
   in localStorage under 'aerodrome-theme' so it survives reloads and navigation
   between admin pages.

   'auto' resolves via prefers-color-scheme and listens for OS-level changes
   so flipping your system theme mid-session also flips Aerodrome.

   NOTE: this file does NOT include the FOUC-prevention script — that has to
   stay inline in each template's <head> so it runs synchronously before any
   CSS is applied. See the inline block marked "<!-- theme:inline-fouc -->"
   in each template.

   v2.47.0: extracted from nine template-local copies into this single file.
   ============================================================================= */

(function(){
    var STORAGE_KEY = 'aerodrome-theme';

    function applyResolved(mode) {
        var resolved;
        if (mode === 'auto') {
            resolved = window.matchMedia('(prefers-color-scheme: light)').matches
                ? 'light' : 'dark';
        } else {
            resolved = mode;
        }
        document.documentElement.setAttribute('data-theme', resolved);
    }

    function reflectUI(mode) {
        // Mark the active segmented button with .on. Done by attribute so
        // it works across all templates without coupling to IDs.
        document.querySelectorAll('[data-theme-choice]').forEach(function(b){
            b.classList.toggle('on', b.getAttribute('data-theme-choice') === mode);
        });
    }

    window.aerodromeSetTheme = function(mode) {
        if (mode !== 'auto' && mode !== 'dark' && mode !== 'light') mode = 'auto';
        try { localStorage.setItem(STORAGE_KEY, mode); } catch (e) { /* best effort */ }
        applyResolved(mode);
        reflectUI(mode);
    };

    // On page load: reflect the currently-saved mode in the UI.
    document.addEventListener('DOMContentLoaded', function(){
        var saved;
        try { saved = localStorage.getItem(STORAGE_KEY) || 'auto'; }
        catch (e) { saved = 'auto'; }
        reflectUI(saved);
    });

    // Listen for OS-level theme changes when the user has 'auto' selected.
    if (window.matchMedia) {
        var mq = window.matchMedia('(prefers-color-scheme: light)');
        var onChange = function(){
            var saved;
            try { saved = localStorage.getItem(STORAGE_KEY) || 'auto'; }
            catch (e) { saved = 'auto'; }
            if (saved === 'auto') applyResolved('auto');
        };
        // Newer API; fall back to addListener for older browsers.
        if (mq.addEventListener) mq.addEventListener('change', onChange);
        else if (mq.addListener) mq.addListener(onChange);
    }
})();

/* =============================================================================
   v2.58.1: Header-height measurement for sticky-positioning offsets.
   =============================================================================
   The pinned tabs bar (.tabs-wrap on the dashboard) and pinned breadcrumb
   (.crumbs-bar on the aircraft detail page) sit immediately below the
   pinned header. Their `top` value must equal the header's rendered height
   exactly — too high and they hover over the header's bottom border; too
   low and a hairline gap of scrolling content shows through between the
   two pinned bars (visible v2.58.0 regression).

   Hard-coding `top: 58px` worked in normal viewing but rendered ~1-2px short
   on some viewport widths / theme variations / browser zoom levels. Solution:
   measure the header's actual rendered height once on load and after
   every viewport resize, then expose it as the CSS custom property
   --aerodrome-header-height on the document root. The pinned bars
   reference it via top: var(--aerodrome-header-height, 58px) — the 58px
   fallback covers the brief moment between page render and first measurement.

   Also re-measures after fonts load (the fonts API resolves once webfonts
   become available; if it's not present, the initial measurement is fine
   since theme uses system fonts for 99% of the layout).

   v2.59.0: extended to ALSO measure the tabs-wrap height and expose
   --aerodrome-tabs-height. The new column-header strips on tab content
   (Live/Watchlist/Military/All/Search) pin at top:
   calc(var(--aerodrome-header-height) + var(--aerodrome-tabs-height))
   so they sit immediately below the tabs bar, forming a 3-level pin
   stack (header → tabs → column-header). Gracefully degrades on pages
   without .tabs-wrap (admin pages) — the variable just stays unset
   and any rule that references it falls back. */
(function(){
    function measureHeader(){
        var hdr = document.querySelector('.hdr');
        if (hdr) {
            var h = hdr.getBoundingClientRect().height;
            if (h > 0){
                document.documentElement.style.setProperty(
                    '--aerodrome-header-height', Math.ceil(h) + 'px'
                );
            }
        }
        var tabs = document.querySelector('.tabs-wrap');
        if (tabs) {
            var t = tabs.getBoundingClientRect().height;
            if (t > 0){
                document.documentElement.style.setProperty(
                    '--aerodrome-tabs-height', Math.ceil(t) + 'px'
                );
            }
        }
    }
    document.addEventListener('DOMContentLoaded', measureHeader);
    window.addEventListener('resize', measureHeader);
    if (document.fonts && document.fonts.ready){
        document.fonts.ready.then(measureHeader).catch(function(){});
    }
})();

/* =============================================================================
   v3.1.0: Demo-mode banner — shared injection across all admin pages.
   =============================================================================
   On demo-mode installs, every page shows a persistent yellow banner that
   says "Demo mode — showing simulated data. [Configure real receiver]"
   with the bracket as a link to /config#demo (the Demo tab in
   Configuration, which houses the switch-to-real wizard launcher).

   The banner is created by JS rather than duplicating HTML across every
   admin template — eleven copies of the same markup would be eleven
   places to forget. theme.js loads on every admin page, so this module
   self-installs everywhere automatically.

   The banner is also injected into templates/index.html as inline HTML
   (the #demoBanner element) — when the inline element exists, this
   module finds and toggles it; otherwise it creates and prepends one.
   Either way, the per-page behavior is the same.

   Also exposes window.aerodromeDemoMode (read-only boolean) so other
   feature code — most importantly the external-link "Track it"
   confirmation guard — can read demo state without its own status
   fetch. Refreshed every 30s via the same poll cycle.
   ============================================================================= */
(function(){
    window.aerodromeDemoMode = false;  // updated by polls

    function ensureBanner() {
        var existing = document.getElementById('demoBanner');
        if (existing) return existing;
        var el = document.createElement('div');
        el.id = 'demoBanner';
        // Inline styles so the banner renders correctly even before any
        // page-specific CSS has loaded. Matches the inline styling used
        // by the existing systemBanner pattern in templates/index.html.
        el.style.cssText =
            'display:none; margin: 8px 12px 0; padding:10px 14px; ' +
            'background:rgba(245,158,11,0.13); border:1px solid rgba(245,158,11,0.45); ' +
            'border-radius:6px; color:var(--amber); font-size:13px;';
        el.innerHTML = 'Demo mode — showing simulated data. ' +
            '<a href="/config#demo" style="color:var(--amber); ' +
            'text-decoration:underline; font-weight:600;">Configure real receiver</a>';
        // Inject above the header so it doesn't push content down within
        // the main content area. document.body's first child is the
        // safest universal position — works regardless of whether the
        // page has .hdr, .tabs-wrap, or its own layout root.
        if (document.body.firstChild) {
            document.body.insertBefore(el, document.body.firstChild);
        } else {
            document.body.appendChild(el);
        }
        return el;
    }

    function refresh() {
        fetch('/api/status', { cache: 'no-store' })
            .then(function(r) { return r.ok ? r.json() : null; })
            .then(function(j) {
                if (!j) return;
                var on = !!j.demo_enabled;
                window.aerodromeDemoMode = on;
                var el = ensureBanner();
                el.style.display = on ? 'block' : 'none';
            })
            .catch(function(){ /* non-fatal — leave banner state alone */ });
    }

    // First call as soon as DOM is ready so the banner appears on the
    // initial render. Then refresh every 30s to pick up demo.enabled
    // flips from the switch-to-real wizard within one cycle.
    document.addEventListener('DOMContentLoaded', function(){
        refresh();
        setInterval(refresh, 30000);
    });
})();

/* =============================================================================
   v3.1.0: External "Track ↗" link guard for demo mode.
   =============================================================================
   When demo.enabled=true, clicking any external-tracker link (Track ↗ on
   tab rows or on the aircraft detail page) shows a confirmation dialog
   explaining that the ICAO is simulated and won't be found by the
   external tracker. "Continue anyway" still opens the link (respects
   the user's curiosity to see what the tracker returns); "Cancel"
   does nothing.

   Targets are anchors with class .track-external-link. Both
   templates/index.html (table-cell Track links via trackLink()) and
   templates/aircraft.html (#trackLink on the detail page) emit this
   class so the same delegation handles both. data-icao attribute on
   the anchor is included in the dialog so the user can see which
   aircraft they're trying to track.

   On real installs (window.aerodromeDemoMode === false), the click
   passes through untouched — zero behavior change for non-demo users.
   ============================================================================= */
(function(){
    document.addEventListener('click', function(e){
        if (!window.aerodromeDemoMode) return;
        // Walk up the event path looking for a track-external-link
        // anchor. Closest() handles cases where the click target is
        // nested inside the anchor (e.g. clicking the ↗ glyph).
        var a = e.target.closest ? e.target.closest('a.track-external-link') : null;
        if (!a) return;
        var icao = a.getAttribute('data-icao') || '?';
        var ok = window.confirm(
            'This is a simulated aircraft.\n\n' +
            'Demo mode shows synthetic data. ICAO ' + icao +
            ' doesn\u2019t correspond to a real aircraft, so the ' +
            'external tracker won\u2019t find it.\n\n' +
            'Continue anyway?'
        );
        if (!ok) {
            e.preventDefault();
            e.stopPropagation();
        }
    }, true);  // capture-phase so we run before any other handlers
})();
