/* Airport locators for the Leaflet maps: the Live radar and the display board
   (v3.4.182). /api/airports gives the airports near the receiver (OurAirports
   data, shipped with Aerodrome). Each page attaches a layer to its map and
   adds a button + panel; the viewer's choice is kept per browser under the
   page's own localStorage key, and Configuration → Map (map.airports) is the
   starting choice until the viewer changes it.

   Look: a faint ring and code under the aircraft, like the range-ring labels;
   each category appears from its own zoom, and from RUNWAY_ZOOM the ring
   becomes the airport's real runways. No interaction: the pane takes no
   pointer events. */
(function(){
  'use strict';
  var CATS = [
    {key: 'major',    label: 'Major',        hint: 'large airline airports',  minZoom: 5,  r: 4.5},
    {key: 'regional', label: 'Regional',     hint: 'from mid zoom',           minZoom: 7,  r: 3.5},
    {key: 'small',    label: 'Small fields', hint: 'paved runway, zoomed in', minZoom: 10, r: 2.5}
  ];
  var BY_KEY = {};
  CATS.forEach(function(c){ BY_KEY[c.key] = c; });
  var RUNWAY_ZOOM = 11;
  var ICON = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" '
    + 'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<g transform="rotate(35 12 12)"><rect x="9" y="2.5" width="6" height="19" rx="1"/>'
    + '<line x1="12" y1="6" x2="12" y2="8"/><line x1="12" y1="11" x2="12" y2="13"/>'
    + '<line x1="12" y1="16" x2="12" y2="18"/></g></svg>';
  var CSS = ''
    + '.apt-label{font-size:9.5px;font-weight:500;line-height:12px;white-space:nowrap;letter-spacing:.02em;'
    +   'color:rgba(226,232,240,.5);text-shadow:0 0 2px rgba(0,0,0,.85);pointer-events:none;background:none;border:0}'
    + '.apt-label.apt-major{font-size:10.5px;font-weight:600;color:rgba(226,232,240,.6)}'
    + '.apt-label.apt-small{font-size:9px;color:rgba(226,232,240,.42)}'
    + '.apt-label.apt-light{color:rgba(30,41,59,.62);text-shadow:0 0 2px rgba(255,255,255,.95)}'
    + '.apt-label.apt-light.apt-major{color:rgba(30,41,59,.72)}'
    + '.apt-panel{position:absolute;left:48px;bottom:10px;z-index:1000;width:196px;padding:9px 11px;'
    +   'background:var(--bg1,#111827);border:1px solid var(--bdr,#2a3a54);border-radius:8px;'
    +   'box-shadow:0 8px 30px rgba(0,0,0,.35);font-size:11px;color:var(--t2,#94a3b8);cursor:default}'
    + '.apt-panel[hidden]{display:none}'
    + '.apt-panel .apt-title{color:var(--t1,#e2e8f0);font-weight:600;margin-bottom:6px}'
    + '.apt-panel .apt-row{display:flex;gap:7px;align-items:flex-start;padding:3px 0;cursor:pointer}'
    + '.apt-panel .apt-row input{accent-color:var(--cyan,#06b6d4);margin:1px 0 0;cursor:pointer}'
    + '.apt-panel .apt-row b{display:block;color:var(--t1,#e2e8f0);font-weight:600}'
    + '.apt-panel .apt-row i{display:block;font-style:normal;color:var(--t3,#64748b);font-size:10px}';

  var listPromise = null;
  function load(){
    if(!listPromise){
      listPromise = fetch('/api/airports')
        .then(function(r){ return r.ok ? r.json() : null; })
        .then(function(j){ return (j && Array.isArray(j.airports)) ? j.airports : []; })
        .catch(function(){ listPromise = null; return []; });
    }
    return listPromise;
  }

  function injectCss(){
    if(document.getElementById('aptCss')) return;
    var s = document.createElement('style');
    s.id = 'aptCss';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  // The server setting's starting choice: off / major (major + regional) / all.
  function defaults(setting){
    var on = setting !== 'off';
    return {major: on, regional: on, small: setting === 'all'};
  }
  function choice(storeKey, setting){
    try {
      var v = JSON.parse(localStorage.getItem(storeKey) || 'null');
      if(v && typeof v === 'object') return {major: !!v.major, regional: !!v.regional, small: !!v.small};
    } catch(e){}
    return defaults(setting);
  }
  function save(storeKey, c){
    try { localStorage.setItem(storeKey, JSON.stringify({major: !!c.major, regional: !!c.regional, small: !!c.small})); } catch(e){}
  }
  function anyOn(c){ return !!(c && (c.major || c.regional || c.small)); }

  function esc(s){
    return String(s == null ? '' : s).replace(/[&<>"']/g, function(ch){
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch];
    });
  }

  // attach(map, {isDark: () => bool, choice: () => {major, regional, small}})
  // → {redraw}. Draws once the list arrives and again after every move/zoom.
  function attach(map, opts){
    injectCss();
    if(!map.getPane('airports')){
      var pane = map.createPane('airports');
      pane.style.zIndex = 350;            // under the aircraft (overlays 400, markers 600)
      pane.style.pointerEvents = 'none';
    }
    var layer = L.layerGroup().addTo(map);
    var list = null;
    function draw(){
      layer.clearLayers();
      var c = opts.choice();
      if(!list || !list.length || !anyOn(c)) return;
      var z = map.getZoom(), bounds = map.getBounds().pad(0.25), dark = !!opts.isDark();
      var color = dark ? '#cbd5e1' : '#334155';
      list.forEach(function(a){
        var cat = BY_KEY[a.cat];
        if(!cat || !c[a.cat] || z < cat.minZoom || !bounds.contains([a.lat, a.lon])) return;
        var rw = z >= RUNWAY_ZOOM ? (a.runways || []) : [];
        var at = [a.lat, a.lon], anchor = [-(cat.r + 4), 6];
        if(rw.length){
          var east = -Infinity;
          rw.forEach(function(r){
            L.polyline([[r[0], r[1]], [r[2], r[3]]], {pane: 'airports', weight: a.cat === 'small' ? 2 : 3,
              color: color, opacity: 0.5, interactive: false, lineCap: 'butt'}).addTo(layer);
            east = Math.max(east, r[1], r[3]);
          });
          at = [a.lat, east];             // label beside the runways' east end
          anchor = [-6, 6];
        } else {
          L.circleMarker(at, {pane: 'airports', radius: cat.r, weight: 1.2, color: color,
            opacity: dark ? 0.5 : 0.6, fill: false, interactive: false}).addTo(layer);
        }
        L.marker(at, {pane: 'airports', interactive: false, keyboard: false,
          icon: L.divIcon({className: 'apt-label apt-' + a.cat + (dark ? '' : ' apt-light'),
                           html: esc(a.code), iconSize: null, iconAnchor: anchor})}).addTo(layer);
      });
    }
    load().then(function(l){ list = l; draw(); });
    map.on('moveend', draw);
    return {redraw: draw};
  }

  // panel(map, {choice, onChange(c)}) → {el, sync, open(bool), isOpen}.
  // Three checkboxes inside the map's container; the page positions it.
  function panel(map, opts){
    injectCss();
    var el = document.createElement('div');
    el.className = 'apt-panel';
    el.hidden = true;
    el.innerHTML = '<div class="apt-title">Airports</div>' + CATS.map(function(c){
      return '<label class="apt-row"><input type="checkbox" data-cat="' + c.key + '">'
           + '<span><b>' + c.label + '</b><i>' + c.hint + '</i></span></label>';
    }).join('');
    map.getContainer().appendChild(el);
    L.DomEvent.disableClickPropagation(el);
    L.DomEvent.disableScrollPropagation(el);
    function sync(){
      var c = opts.choice();
      Array.prototype.forEach.call(el.querySelectorAll('input[data-cat]'), function(i){ i.checked = !!c[i.dataset.cat]; });
    }
    el.addEventListener('change', function(e){
      var i = e.target;
      if(!i || !i.dataset || !i.dataset.cat) return;
      var c = opts.choice();
      c[i.dataset.cat] = i.checked;
      opts.onChange(c);
    });
    return {
      el: el, sync: sync,
      open: function(v){ if(v) sync(); el.hidden = !v; },
      isOpen: function(){ return !el.hidden; }
    };
  }

  window.AerodromeAirports = {attach: attach, panel: panel, choice: choice, save: save,
                              anyOn: anyOn, defaults: defaults, ICON: ICON, CATS: CATS};
})();
