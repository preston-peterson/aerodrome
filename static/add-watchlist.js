/* add-watchlist.js — shared "Add to Watchlist" component.
 *
 * v3.4.24: extracted from templates/index.html so the Live tab and the
 * aircraft details page run the IDENTICAL add-to-watchlist flow, instead
 * of two copies that look alike today and drift later — the failure mode
 * behind Lesson 4.18 / the v3.4.23 readiness-poll bug.
 *
 * Self-contained on purpose: it injects its own modal DOM and its own
 * namespaced CSS (.awl-*), so it depends on no host page's modal system.
 * Drop it in with a single <script src="/static/add-watchlist.js"> tag.
 *
 * Usage:
 *   AddWatchlist.open(aircraft, { onAdded, toast })
 *
 *   aircraft = {
 *     icao,           // required — hex string
 *     callsign,       // optional
 *     tail,           // optional — if omitted, the component does a
 *                     //   background hexdb lookup to offer a tail option;
 *                     //   if provided (the detail page has it already),
 *                     //   the lookup is skipped entirely
 *     model,          // optional — human-readable description or type
 *     modelTypeCode,  // optional — short type code (e.g. "S22T")
 *   }
 *   opts.onAdded(icao) — called after a successful add so the caller can
 *                        update its own UI (flip a button, refresh chips,
 *                        re-render tabs, etc.). Page-specific behavior
 *                        lives here, NOT in the component.
 *   opts.toast(msg)    — optional; if omitted, a built-in minimal toast is
 *                        used, so the component is drop-in on pages with
 *                        no toast of their own (e.g. the aircraft page).
 *
 * The /api/watchlist/add contract ({identifier, id_type, label}) is the
 * only thing that must stay in sync with the server — and that's
 * server-enforced, not duplicated logic.
 */
(function () {
    'use strict';

    var MODAL_ID = 'awlModal';
    var _ctx = null;    // { icao, callsign, tail, model, modelTypeCode, selectedType, selectedValue }
    var _opts = null;   // { onAdded, toast }
    var _toastTimer = null;

    // ---- styles -----------------------------------------------------------
    // Namespaced (.awl-*) clones of index.html's .modal-* styling, so the
    // component carries its own look and never collides with or depends on
    // a host page's modal CSS. .add-wl-btn is intentionally NOT namespaced —
    // it is the shared button class both pages render, and owning it here is
    // what keeps the button consistent across the project.
    var CSS = `
.add-wl-btn{background:transparent;border:1px solid var(--bdr);color:var(--t2);
  width:28px;height:22px;border-radius:4px;cursor:pointer;display:inline-flex;
  align-items:center;justify-content:center;font-size:14px;font-weight:600;
  line-height:1;padding:0;transition:all .15s;}
.add-wl-btn:hover{border-color:var(--cyan);color:var(--cyan);}
.add-wl-btn.added{border-color:var(--green);color:var(--green);cursor:default;}
.add-wl-btn.added:hover{border-color:var(--green);color:var(--green);}
.awl-modal-bg{position:fixed;inset:0;background:rgba(10,14,23,.7);display:none;
  align-items:center;justify-content:center;z-index:500;}
.awl-modal-bg.on{display:flex;}
.awl-modal{background:var(--bg2);border:1px solid var(--bdr);border-radius:10px;
  padding:20px 22px;min-width:360px;max-width:440px;}
.awl-modal-title{font-size:15px;font-weight:600;margin-bottom:4px;}
.awl-modal-sub{font-size:12px;color:var(--t2);margin-bottom:14px;font-family:var(--mono);}
.awl-modal-label{font-size:11px;color:var(--t2);text-transform:uppercase;
  letter-spacing:.05em;margin-bottom:6px;display:block;}
.awl-modal-options{display:flex;flex-direction:column;gap:6px;margin-bottom:14px;}
.awl-modal-option{padding:10px 12px;border:1px solid var(--bdr);border-radius:6px;
  background:var(--bg1);cursor:pointer;display:flex;align-items:center;
  justify-content:space-between;gap:10px;transition:all .15s;}
.awl-modal-option:hover{border-color:var(--cyan);background:var(--bg3);}
.awl-modal-option.selected{border-color:var(--cyan);background:rgba(6,182,212,.08);}
.awl-modal-opt-type{font-size:11px;color:var(--t2);text-transform:uppercase;letter-spacing:.05em;}
.awl-modal-opt-val{font-family:var(--mono);font-size:13px;color:var(--t1);}
.awl-modal-input{width:100%;background:var(--bg1);border:1px solid var(--bdr);
  color:var(--t1);font-family:var(--font);font-size:13px;padding:8px 10px;
  border-radius:6px;margin-bottom:14px;outline:none;}
.awl-modal-input:focus{border-color:var(--cyan);}
.awl-modal-actions{display:flex;gap:8px;justify-content:flex-end;}
.awl-modal-btn{font-family:var(--font);font-size:13px;font-weight:500;
  padding:7px 16px;border-radius:6px;border:1px solid var(--bdr);background:var(--bg1);
  color:var(--t2);cursor:pointer;transition:all .15s;}
.awl-modal-btn:hover{border-color:var(--cyan);color:var(--cyan);}
.awl-modal-btn-primary{background:var(--cyan);border-color:var(--cyan);color:var(--bg0);}
.awl-modal-btn-primary:hover{filter:brightness(1.15);color:var(--bg0);}
.awl-modal-btn-primary:disabled{opacity:.4;cursor:not-allowed;filter:none;}
.awl-toast{position:fixed;bottom:20px;right:20px;background:var(--bg2);
  border:1px solid var(--bdr);border-radius:8px;padding:10px 18px;font-size:12px;
  color:var(--t1);opacity:0;transform:translateY(10px);transition:all .3s;
  z-index:1000;max-width:400px;pointer-events:none;}
.awl-toast.show{opacity:1;transform:translateY(0);}
`;

    // ---- helpers ----------------------------------------------------------
    function _esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // Use the host page's toast if it provided one; otherwise fall back to
    // the component's own minimal toast, so the component is drop-in on
    // pages with no toast of their own (e.g. the aircraft details page).
    function _toast(msg) {
        if (_opts && typeof _opts.toast === 'function') {
            _opts.toast(msg);
            return;
        }
        var el = document.getElementById('awlToast');
        if (!el) return;
        el.textContent = msg;
        el.className = 'awl-toast show';
        clearTimeout(_toastTimer);
        _toastTimer = setTimeout(function () { el.className = 'awl-toast'; }, 6000);
    }

    // Resolve a tail number via hexdb.io. The hex goes directly after
    // /aircraft/ — /aircraft/icao/<hex> 404s. (Mirrors collector.py's
    // resolve_icao_to_tail.) Ported verbatim from index.html's lookupTail.
    async function _lookupTail(icao) {
        try {
            var r = await fetch('https://hexdb.io/api/v1/aircraft/' + icao);
            if (!r.ok) return null;
            var j = await r.json();
            return (j.Registration || '').trim() || null;
        } catch (e) {
            return null;
        }
    }

    // ---- DOM injection ----------------------------------------------------
    // CSS is injected eagerly at load: the host page renders .add-wl-btn
    // buttons before the modal is ever opened, so the styles have to be
    // present from the start, not lazily on first open().
    function _injectStyles() {
        if (document.getElementById('awlStyles')) return;
        var style = document.createElement('style');
        style.id = 'awlStyles';
        style.textContent = CSS;
        document.head.appendChild(style);
    }

    // Modal DOM is injected once, as soon as document.body is available.
    function _injectModal() {
        if (document.getElementById(MODAL_ID)) return;

        var wrap = document.createElement('div');
        wrap.innerHTML =
            '<div class="awl-modal-bg" id="' + MODAL_ID + '">' +
              '<div class="awl-modal">' +
                '<div class="awl-modal-title">Add to Watchlist</div>' +
                '<div class="awl-modal-sub" id="awlSub">\u2014</div>' +
                '<label class="awl-modal-label">Identifier</label>' +
                '<div class="awl-modal-options" id="awlOptions"></div>' +
                '<label class="awl-modal-label">Label</label>' +
                '<input type="text" class="awl-modal-input" id="awlLabel" placeholder="e.g. Dad\'s Cessna">' +
                '<div class="awl-modal-actions">' +
                  '<button class="awl-modal-btn" id="awlCancel">Cancel</button>' +
                  '<button class="awl-modal-btn awl-modal-btn-primary" id="awlConfirm">Add</button>' +
                '</div>' +
              '</div>' +
            '</div>' +
            '<div class="awl-toast" id="awlToast"></div>';
        while (wrap.firstChild) document.body.appendChild(wrap.firstChild);

        // Static handlers wired once. The options list is re-rendered, so
        // its clicks are delegated rather than bound per-option.
        var bg = document.getElementById(MODAL_ID);
        bg.addEventListener('click', function (e) {
            if (e.target === bg) _close();   // backdrop click only
        });
        document.getElementById('awlCancel').addEventListener('click', _close);
        document.getElementById('awlConfirm').addEventListener('click', _confirm);
        document.getElementById('awlOptions').addEventListener('click', function (e) {
            var opt = e.target.closest('.awl-modal-option');
            if (!opt || opt.dataset.type === '_pending') return;
            _selectOption(opt.dataset.type, opt.dataset.value);
        });
    }

    function _ensure() {
        _injectStyles();
        _injectModal();
    }

    // ---- modal logic (ported from index.html, behavior-identical) ---------
    function _renderOptions(resolving) {
        var c = _ctx;
        var ctr = document.getElementById('awlOptions');
        var opts = [];

        opts.push({ type: 'icao', label: 'ICAO Hex', value: c.icao });
        if (c.callsign) opts.push({ type: 'callsign', label: 'Callsign', value: c.callsign });
        if (c.tail) {
            opts.push({ type: 'tail', label: 'Tail Number', value: c.tail });
        } else if (resolving) {
            opts.push({ type: '_pending', label: 'Tail Number', value: 'looking up\u2026' });
        }
        // Model matching — offer the description and/or type code as substring
        // matches, so the user can watch any aircraft of this type.
        if (c.model) {
            opts.push({ type: 'model', label: 'Model (matches any aircraft of this type)', value: c.model });
        }
        // A distinct short type code AND a different description → offer both.
        if (c.modelTypeCode && c.modelTypeCode !== c.model) {
            opts.push({ type: 'model', label: 'Type code', value: c.modelTypeCode });
        }

        ctr.innerHTML = opts.map(function (o) {
            if (o.type === '_pending') {
                return '<div class="awl-modal-option" data-type="_pending" style="opacity:.5;cursor:wait">' +
                    '<span class="awl-modal-opt-type">' + _esc(o.label) + '</span>' +
                    '<span class="awl-modal-opt-val" style="font-style:italic">' + _esc(o.value) + '</span>' +
                    '</div>';
            }
            // Selected state matches BOTH type and value — there can be two
            // model entries (description + short code).
            var isSel = (c.selectedType === o.type && c.selectedValue === o.value);
            return '<div class="awl-modal-option' + (isSel ? ' selected' : '') + '"' +
                ' data-type="' + _esc(o.type) + '" data-value="' + _esc(o.value) + '">' +
                '<span class="awl-modal-opt-type">' + _esc(o.label) + '</span>' +
                '<span class="awl-modal-opt-val">' + _esc(o.value) + '</span>' +
                '</div>';
        }).join('');
    }

    function _selectOption(type, value) {
        _ctx.selectedType = type;
        _ctx.selectedValue = value;
        // Auto-overwrite the label only if it still holds a prior default —
        // i.e. the user hasn't typed their own.
        var labelEl = document.getElementById('awlLabel');
        var priorDefaults = [_ctx.icao, _ctx.callsign, _ctx.tail, _ctx.model, _ctx.modelTypeCode]
            .filter(Boolean);
        if (priorDefaults.indexOf(labelEl.value) !== -1) labelEl.value = value;
        _renderOptions(false);
    }

    function _close() {
        var bg = document.getElementById(MODAL_ID);
        if (bg) bg.classList.remove('on');
        _ctx = null;
        _opts = null;
    }

    async function _confirm() {
        if (!_ctx) return;
        var btn = document.getElementById('awlConfirm');
        btn.disabled = true;
        var label = document.getElementById('awlLabel').value.trim() || _ctx.selectedValue;
        // Capture everything needed before _close() nulls the state.
        var identifier = _ctx.selectedValue;
        var idType = _ctx.selectedType;
        var icao = _ctx.icao;
        var onAdded = _opts && _opts.onAdded;

        try {
            var r = await fetch('/api/watchlist/add', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ identifier: identifier, id_type: idType, label: label })
            });
            var j = await r.json();
            if (r.ok) {
                _toast('Added ' + identifier + ' \u2014 live within one poll cycle');
                _close();
                if (typeof onAdded === 'function') onAdded(icao);
            } else {
                _toast((j && j.error) || 'Failed');
                btn.disabled = false;
            }
        } catch (e) {
            _toast('Error adding');
            btn.disabled = false;
        }
    }

    // ---- public API -------------------------------------------------------
    async function open(aircraft, opts) {
        _ensure();
        aircraft = aircraft || {};
        var icao = (aircraft.icao || '').toUpperCase();
        if (!icao) return;

        _opts = opts || {};
        _ctx = {
            icao: icao,
            callsign: (aircraft.callsign || '').trim() || null,
            tail: (aircraft.tail || '').trim() || null,
            model: (aircraft.model || '').trim() || null,
            modelTypeCode: (aircraft.modelTypeCode || '').trim() || null,
            selectedType: 'icao',
            selectedValue: icao
        };

        document.getElementById('awlSub').textContent =
            _ctx.icao +
            (_ctx.callsign ? ' \u00b7 ' + _ctx.callsign : '') +
            (_ctx.model ? ' \u00b7 ' + _ctx.model : '');

        var needLookup = !_ctx.tail;
        _renderOptions(needLookup);   // show the "looking up…" row only if we will
        document.getElementById('awlLabel').value = _ctx.icao;
        document.getElementById('awlConfirm').disabled = false;
        document.getElementById(MODAL_ID).classList.add('on');

        // If the caller already supplied a tail (the detail page has it from
        // its payload), skip the hexdb lookup entirely. Otherwise resolve it
        // in the background and re-render the options once it lands.
        if (needLookup) {
            var ctxAtLookup = _ctx;
            var tail = await _lookupTail(_ctx.icao);
            // Guard: the modal may have been closed or reopened during the await.
            if (_ctx !== ctxAtLookup) return;
            if (tail) _ctx.tail = tail;
            _renderOptions(false);
        }
    }

    // Inject styles now so host-rendered .add-wl-btn buttons are styled
    // immediately; inject the modal DOM as soon as the body is available.
    _injectStyles();
    if (document.body) {
        _injectModal();
    } else {
        document.addEventListener('DOMContentLoaded', _injectModal);
    }

    window.AddWatchlist = { open: open };
})();
