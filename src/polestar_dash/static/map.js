/* Shared Leaflet map helper for poledash.
 *
 * One place to build a map with a satellite/dark base-layer toggle, so the
 * tripbook, test and dashboard pages don't each re-implement it. Esri World
 * Imagery (free, no key) is the default "sat fotos" layer; the Carto dark
 * vector style is the alternative that matches the UJH dark theme.
 *
 * Captures window.L immediately: the date-fns CDN bundle on chart pages
 * reassigns one-letter globals (see the project conventions), and `L` is one of them.
 */
(function () {
  var Leaflet = window.L;

  function buildLayers(labels) {
    labels = labels || {};
    var sat = Leaflet.tileLayer(
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      { attribution: 'Esri, Maxar, Earthstar Geographics', maxZoom: 19 }
    );
    var dark = Leaflet.tileLayer(
      'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
      { attribution: '© OpenStreetMap © CARTO', subdomains: 'abcd', maxZoom: 19 }
    );
    var satLabel = labels.satellite || 'Satellite';
    var darkLabel = labels.dark || 'Dark';
    var bases = {};
    bases[satLabel] = sat;
    bases[darkLabel] = dark;
    return { sat: sat, dark: dark, bases: bases, satLabel: satLabel, darkLabel: darkLabel };
  }

  /* make(elId, {center, zoom, scroll, labels}) → Leaflet map with a layer
   * switcher. Remembers the chosen base layer in localStorage across pages. */
  function make(elId, opts) {
    opts = opts || {};
    if (typeof Leaflet === 'undefined') return null;
    var map = Leaflet.map(elId, { scrollWheelZoom: opts.scroll === true });
    var layers = buildLayers(opts.labels);
    var saved = localStorage.getItem('mapLayer');
    (saved === 'dark' ? layers.dark : layers.sat).addTo(map);
    Leaflet.control.layers(layers.bases, null, { position: 'topright' }).addTo(map);
    map.on('baselayerchange', function (e) {
      localStorage.setItem('mapLayer', e.name === layers.darkLabel ? 'dark' : 'sat');
    });
    if (opts.center) map.setView(opts.center, opts.zoom || 14);
    return map;
  }

  window.poleMap = { make: make, L: Leaflet };
})();
