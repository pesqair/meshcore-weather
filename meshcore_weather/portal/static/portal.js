// Meshcore Weather Portal — MapLibre helpers.
// All map data is served from /static/geo/ — fully offline.

/**
 * Build a MapLibre style for the weather portal.
 * Renders states, countries, cities from bundled GeoJSON.
 * Returns a style JSON object you can pass to MapLibre's Map constructor.
 */
function buildWeatherMapStyle() {
  return {
    version: 8,
    name: "Meshcore Weather",
    // No sprite, no glyphs (no labels in base map)
    sources: {
      countries: {
        type: "geojson",
        data: "/static/geo/countries.geojson",
      },
      states: {
        type: "geojson",
        data: "/static/geo/states.geojson",
      },
      cities: {
        type: "geojson",
        data: "/static/geo/cities.geojson",
      },
    },
    layers: [
      {
        id: "background",
        type: "background",
        paint: { "background-color": "#1b2636" },
      },
      {
        id: "countries-fill",
        type: "fill",
        source: "countries",
        paint: {
          "fill-color": "#2a3546",
          "fill-opacity": 1,
        },
      },
      {
        id: "states-fill",
        type: "fill",
        source: "states",
        filter: ["==", ["get", "admin"], "United States of America"],
        paint: {
          "fill-color": "#334259",
          "fill-opacity": 1,
        },
      },
      {
        id: "states-line",
        type: "line",
        source: "states",
        paint: {
          "line-color": "#5a6a80",
          "line-width": [
            "interpolate", ["linear"], ["zoom"],
            3, 0.3,
            6, 0.7,
            10, 1.2,
          ],
        },
      },
      {
        id: "countries-line",
        type: "line",
        source: "countries",
        paint: {
          "line-color": "#8fa3bd",
          "line-width": [
            "interpolate", ["linear"], ["zoom"],
            3, 0.5,
            6, 1.0,
            10, 1.6,
          ],
        },
      },
      {
        id: "city-dots",
        type: "circle",
        source: "cities",
        filter: [">", ["get", "pop"], 100000],
        paint: {
          "circle-radius": [
            "interpolate", ["linear"], ["zoom"],
            3, 1.5,
            6, 3,
            10, 5,
          ],
          "circle-color": "#f5f5f7",
          "circle-opacity": 0.7,
          "circle-stroke-color": "#0a1220",
          "circle-stroke-width": 0.5,
        },
      },
    ],
  };
}

/**
 * Warning type codes → display colors (matches the protocol spec).
 */
const WARNING_COLORS = {
  1: "#e11d48", // tornado - red
  2: "#f59e0b", // severe thunderstorm - orange
  3: "#06b6d4", // flash flood - cyan
  4: "#3b82f6", // flood - blue
  5: "#a855f7", // winter storm - purple
  6: "#f97316", // high wind - orange
  7: "#dc2626", // fire - dark red
  8: "#0891b2", // marine - teal
  9: "#fbbf24", // special - yellow
  15: "#9ca3af", // other - gray
};

const WARNING_TYPE_NAMES = {
  1: "Tornado",
  2: "Severe T-Storm",
  3: "Flash Flood",
  4: "Flood",
  5: "Winter Storm",
  6: "High Wind",
  7: "Fire",
  8: "Marine",
  9: "Special",
  15: "Other",
};

/**
 * Create a MapLibre map instance bound to an element ID.
 */
function createWeatherMap(elementId, options = {}) {
  const map = new maplibregl.Map({
    container: elementId,
    style: buildWeatherMapStyle(),
    center: options.center || [-96, 38],  // CONUS center
    zoom: options.zoom || 3.5,
    minZoom: 1,
    maxZoom: 10,
    attributionControl: false,
  });

  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

  return map;
}

/**
 * Add warning polygons to a map as a GeoJSON source + layers.
 * Returns a function to update the data.
 */
function addWarningsLayer(map, warnings) {
  const sourceId = "warnings";
  const features = warnings
    .filter(w => w.vertices && w.vertices.length >= 3)
    .map(w => ({
      type: "Feature",
      properties: {
        type: w.warning_type,
        severity: w.severity,
        color: WARNING_COLORS[w.warning_type] || WARNING_COLORS[15],
        name: WARNING_TYPE_NAMES[w.warning_type] || "Unknown",
        headline: w.headline || "",
        in_coverage: w.in_coverage !== false,
      },
      geometry: {
        type: "Polygon",
        // MapLibre expects [lon, lat], our vertices are [lat, lon]
        coordinates: [w.vertices.map(v => [v[1], v[0]])],
      },
    }));

  const data = { type: "FeatureCollection", features };

  if (map.getSource(sourceId)) {
    map.getSource(sourceId).setData(data);
  } else {
    map.addSource(sourceId, { type: "geojson", data });
    map.addLayer({
      id: "warnings-fill",
      type: "fill",
      source: sourceId,
      paint: {
        "fill-color": ["get", "color"],
        "fill-opacity": [
          "case",
          ["==", ["get", "in_coverage"], true], 0.35,
          0.12,  // outside coverage = faded
        ],
      },
    });
    map.addLayer({
      id: "warnings-line",
      type: "line",
      source: sourceId,
      paint: {
        "line-color": ["get", "color"],
        "line-width": [
          "case",
          ["==", ["get", "in_coverage"], true], 2,
          1,
        ],
        "line-opacity": [
          "case",
          ["==", ["get", "in_coverage"], true], 1,
          0.4,
        ],
      },
    });

    // Popup on click
    map.on("click", "warnings-fill", (e) => {
      const f = e.features[0];
      const html = `
        <div style="font-family: var(--font-sans, sans-serif); font-size: 13px;">
          <strong style="color: ${f.properties.color}">${f.properties.name}</strong>
          <div style="margin-top: 4px;">${escapeHtml(f.properties.headline)}</div>
          ${f.properties.in_coverage ? '' : '<div style="margin-top: 6px; color: #9ca3af; font-size: 11px;">⊘ Outside coverage</div>'}
        </div>
      `;
      new maplibregl.Popup()
        .setLngLat(e.lngLat)
        .setHTML(html)
        .addTo(map);
    });

    map.on("mouseenter", "warnings-fill", () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", "warnings-fill", () => { map.getCanvas().style.cursor = ""; });
  }
}

/**
 * Add a coverage-area polygon overlay (union of zone centroids as a hull).
 * For simplicity, draws a bounding box for now.
 */
function addCoverageLayer(map, bbox) {
  if (!bbox) return;
  const [n, s, w, e] = bbox;
  const sourceId = "coverage";
  const data = {
    type: "FeatureCollection",
    features: [{
      type: "Feature",
      properties: {},
      geometry: {
        type: "Polygon",
        coordinates: [[
          [w, n], [e, n], [e, s], [w, s], [w, n],
        ]],
      },
    }],
  };

  if (map.getSource(sourceId)) {
    map.getSource(sourceId).setData(data);
  } else {
    map.addSource(sourceId, { type: "geojson", data });
    map.addLayer({
      id: "coverage-line",
      type: "line",
      source: sourceId,
      paint: {
        "line-color": "#22d3ee",
        "line-width": 2,
        "line-dasharray": [3, 2],
        "line-opacity": 0.7,
      },
    });
  }
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}
