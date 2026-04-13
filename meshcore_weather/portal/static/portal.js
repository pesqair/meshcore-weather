// Meshcore Weather Portal — SPA controller + MapLibre helpers.
// All map data served from /static/geo/ — fully offline.

// ---------------------------------------------------------------------------
// Map helpers (reused by Weather Map section + System preview map)
// ---------------------------------------------------------------------------

function buildWeatherMapStyle() {
  return {
    version: 8,
    name: "Meshcore Weather",
    sources: {
      countries: { type: "geojson", data: "/static/geo/countries.geojson" },
      states: { type: "geojson", data: "/static/geo/states.geojson" },
      cities: { type: "geojson", data: "/static/geo/cities.geojson" },
    },
    layers: [
      { id: "background", type: "background", paint: { "background-color": "#1b2636" } },
      { id: "countries-fill", type: "fill", source: "countries", paint: { "fill-color": "#2a3546", "fill-opacity": 1 } },
      {
        id: "states-fill", type: "fill", source: "states",
        filter: ["==", ["get", "admin"], "United States of America"],
        paint: { "fill-color": "#334259", "fill-opacity": 1 },
      },
      {
        id: "states-line", type: "line", source: "states",
        paint: { "line-color": "#5a6a80", "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.3, 6, 0.7, 10, 1.2] },
      },
      {
        id: "countries-line", type: "line", source: "countries",
        paint: { "line-color": "#8fa3bd", "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.5, 6, 1.0, 10, 1.6] },
      },
      {
        id: "city-dots", type: "circle", source: "cities",
        filter: [">", ["get", "pop"], 100000],
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 1.5, 6, 3, 10, 5],
          "circle-color": "#f5f5f7", "circle-opacity": 0.7,
          "circle-stroke-color": "#0a1220", "circle-stroke-width": 0.5,
        },
      },
    ],
  };
}

const WARNING_COLORS = {
  1: "#e11d48", 2: "#f59e0b", 3: "#06b6d4", 4: "#3b82f6",
  5: "#a855f7", 6: "#f97316", 7: "#dc2626", 8: "#0891b2",
  9: "#fbbf24", 15: "#9ca3af",
};

const WARNING_TYPE_NAMES = {
  1: "Tornado", 2: "Severe T-Storm", 3: "Flash Flood", 4: "Flood",
  5: "Winter Storm", 6: "High Wind", 7: "Fire", 8: "Marine",
  9: "Special", 15: "Other",
};

function createWeatherMap(elementId, options) {
  options = options || {};
  var map = new maplibregl.Map({
    container: elementId,
    style: buildWeatherMapStyle(),
    center: options.center || [-96, 38],
    zoom: options.zoom || 3.5,
    minZoom: 1,
    maxZoom: 10,
    attributionControl: false,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  return map;
}

function addWarningsLayer(map, warnings) {
  var sourceId = "warnings";
  var features = warnings
    .filter(function (w) { return w.vertices && w.vertices.length >= 3; })
    .map(function (w) {
      return {
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
          coordinates: [w.vertices.map(function (v) { return [v[1], v[0]]; })],
        },
      };
    });

  var data = { type: "FeatureCollection", features: features };

  if (map.getSource(sourceId)) {
    map.getSource(sourceId).setData(data);
  } else {
    map.addSource(sourceId, { type: "geojson", data: data });
    map.addLayer({
      id: "warnings-fill", type: "fill", source: sourceId,
      paint: {
        "fill-color": ["get", "color"],
        "fill-opacity": ["case", ["==", ["get", "in_coverage"], true], 0.35, 0.12],
      },
    });
    map.addLayer({
      id: "warnings-line", type: "line", source: sourceId,
      paint: {
        "line-color": ["get", "color"],
        "line-width": ["case", ["==", ["get", "in_coverage"], true], 2, 1],
        "line-opacity": ["case", ["==", ["get", "in_coverage"], true], 1, 0.4],
      },
    });
    map.on("click", "warnings-fill", function (e) {
      var f = e.features[0];
      var html =
        '<div style="font-family:var(--font-sans,sans-serif);font-size:13px;">' +
        '<strong style="color:' + f.properties.color + '">' + f.properties.name + '</strong>' +
        '<div style="margin-top:4px;">' + escapeHtml(f.properties.headline) + '</div>' +
        (f.properties.in_coverage ? '' : '<div style="margin-top:6px;color:#9ca3af;font-size:11px;">Outside coverage</div>') +
        '</div>';
      new maplibregl.Popup().setLngLat(e.lngLat).setHTML(html).addTo(map);
    });
    map.on("mouseenter", "warnings-fill", function () { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", "warnings-fill", function () { map.getCanvas().style.cursor = ""; });
  }
}

function addCoverageLayer(map, bbox) {
  if (!bbox) return;
  var n = bbox[0], s = bbox[1], w = bbox[2], e = bbox[3];
  var sourceId = "coverage";
  var data = {
    type: "FeatureCollection",
    features: [{
      type: "Feature", properties: {},
      geometry: { type: "Polygon", coordinates: [[[w, n], [e, n], [e, s], [w, s], [w, n]]] },
    }],
  };
  if (map.getSource(sourceId)) {
    map.getSource(sourceId).setData(data);
  } else {
    map.addSource(sourceId, { type: "geojson", data: data });
    map.addLayer({
      id: "coverage-line", type: "line", source: sourceId,
      paint: { "line-color": "#22d3ee", "line-width": 2, "line-dasharray": [3, 2], "line-opacity": 0.7 },
    });
  }
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

// ---------------------------------------------------------------------------
// Portal SPA controller
// ---------------------------------------------------------------------------

var Portal = {

  // -- Router ---------------------------------------------------------------

  router: {
    currentSection: null,
    _sections: ["overview", "broadcast", "map", "system"],

    init: function () {
      var self = this;
      // Wire nav links
      document.querySelectorAll("#main-nav a[data-section]").forEach(function (a) {
        a.addEventListener("click", function (e) {
          e.preventDefault();
          self.navigate(a.dataset.section);
        });
      });
      // Also handle in-page hash links (quick actions)
      document.querySelectorAll('a[href^="#"]').forEach(function (a) {
        if (a.dataset.section) return; // already handled above
        a.addEventListener("click", function (e) {
          var target = a.getAttribute("href").replace("#", "");
          if (self._sections.indexOf(target) !== -1) {
            e.preventDefault();
            self.navigate(target);
          }
        });
      });
      window.addEventListener("hashchange", function () { self._onHashChange(); });
      this._onHashChange();
    },

    navigate: function (section) {
      if (this._sections.indexOf(section) === -1) section = "overview";
      if (section === this.currentSection) return;
      window.location.hash = "#" + section;
    },

    _onHashChange: function () {
      var hash = (window.location.hash || "#overview").replace("#", "");
      if (this._sections.indexOf(hash) === -1) hash = "overview";
      if (hash === this.currentSection) return;

      var prev = this.currentSection;
      this.currentSection = hash;

      // Update nav
      document.querySelectorAll("#main-nav a[data-section]").forEach(function (a) {
        a.classList.toggle("active", a.dataset.section === hash);
      });

      // Update sections
      document.querySelectorAll(".section").forEach(function (sec) {
        sec.classList.toggle("active", sec.id === "section-" + hash);
      });

      // Section lifecycle hooks
      if (prev === "map") Portal.weatherMap.onLeave();
      if (hash === "map") Portal.weatherMap.onEnter();
      if (hash === "overview") Portal.overview.refresh();
      if (hash === "broadcast") Portal.broadcast.onEnter();
      if (hash === "system") Portal.system.onEnter();
    },
  },

  // -- UI utilities ---------------------------------------------------------

  ui: {
    _toastTimer: null,

    showToast: function (msg, ok) {
      if (ok === undefined) ok = true;
      var el = document.getElementById("toast");
      el.textContent = msg;
      el.className = "toast" + (ok ? "" : " err");
      el.style.display = "block";
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(function () { el.style.display = "none"; }, 3500);
    },

    openModal: function (html, wide) {
      var content = document.getElementById("modal-content");
      content.innerHTML = html;
      content.className = "modal-content" + (wide ? " modal-wide" : "");
      document.getElementById("modal-overlay").style.display = "flex";
    },

    closeModal: function () {
      document.getElementById("modal-overlay").style.display = "none";
      document.getElementById("modal-content").innerHTML = "";
    },

    formatAgo: function (seconds) {
      if (seconds == null) return "\u2014";
      if (seconds < 60) return seconds + "s";
      if (seconds < 3600) return Math.floor(seconds / 60) + "m";
      if (seconds < 86400) return Math.floor(seconds / 3600) + "h";
      return Math.floor(seconds / 86400) + "d";
    },
  },

  // -- Overview section -----------------------------------------------------

  overview: {
    _rendered: false,

    render: function (boot) {
      var zoneCount = boot.zone_count || 0;
      var regionCount = boot.region_count || 0;
      var el = document.getElementById("overview-stats");
      el.innerHTML =
        '<div class="stat"><div class="stat-label">Coverage Zones</div>' +
          '<div class="stat-value">' + (zoneCount || "All") + '</div>' +
          '<div class="stat-hint">' + (zoneCount ? "Filtered broadcast" : "No filter set") + '</div></div>' +
        '<div class="stat"><div class="stat-label">Radar Regions</div>' +
          '<div class="stat-value">' + (regionCount || "10") + '</div>' +
          '<div class="stat-hint">MeshWX grids</div></div>' +
        '<div class="stat"><div class="stat-label">EMWIN Products</div>' +
          '<div class="stat-value">' + boot.product_count + '</div>' +
          '<div class="stat-hint">In store (last 12h)</div></div>' +
        '<div class="stat"><div class="stat-label">Data Channel</div>' +
          '<div class="stat-value">' +
            (boot.data_channel != null
              ? '<span class="dot dot-green"></span>#' + boot.data_channel
              : '<span class="dot dot-gray"></span>Off') +
          '</div><div class="stat-hint">MeshWX binary broadcast</div></div>' +
        '<div class="stat"><div class="stat-label">Text Channel</div>' +
          '<div class="stat-value">' +
            (boot.channel_idx != null
              ? '<span class="dot dot-green"></span>#' + boot.channel_idx
              : '<span class="dot dot-gray"></span>Off') +
          '</div><div class="stat-hint">Meshtastic channel</div></div>';

      document.getElementById("overview-coverage-text").textContent =
        boot.coverage_summary || "No coverage filter set. Broadcasting for the entire CONUS.";

      this._rendered = true;
    },

    refresh: function () {
      fetch("/api/status").then(function (r) { return r.json(); }).then(function (data) {
        var grid = document.getElementById("overview-status-grid");
        grid.innerHTML =
          '<div class="stat"><div class="stat-label">Text Channel</div>' +
            '<div class="stat-value"><span class="dot dot-green"></span>#' + (data.radio.channel_idx || "?") + '</div></div>' +
          '<div class="stat"><div class="stat-label">Data Channel</div>' +
            '<div class="stat-value">' +
              (data.radio.data_channel_idx != null
                ? '<span class="dot dot-green"></span>#' + data.radio.data_channel_idx
                : '<span class="dot dot-gray"></span>Off') +
            '</div></div>' +
          '<div class="stat"><div class="stat-label">EMWIN Products</div>' +
            '<div class="stat-value">' + data.store.product_count + '</div></div>' +
          '<div class="stat"><div class="stat-label">Known Contacts</div>' +
            '<div class="stat-value">' + data.contacts.known + '</div></div>';

        document.getElementById("overview-status-time").textContent =
          "Updated " + new Date().toLocaleTimeString();
      }).catch(function (e) {
        document.getElementById("overview-status-time").textContent = "Load failed: " + e.message;
      });

      this.loadActivity();
      this.loadStats();
    },

    _activitySSE: null,
    _activityCount: 0,
    _MAX_ACTIVITY_ROWS: 200,

    _renderActivityRow: function (e) {
      var ts = new Date((e.ts || 0) * 1000);
      var timeStr = ts.toLocaleTimeString();
      var dirBadge = e.direction === "in"
        ? '<span class="badge badge-success">IN</span>'
        : '<span class="badge badge-muted">OUT</span>';
      var typeLabel = {
        v2_request: "Data Request",
        v2_response: "Response",
        v1_refresh: "Refresh",
        broadcast: "Broadcast",
        throttled: "Throttled",
      }[e.event_type] || e.event_type;
      return '<tr>' +
        '<td class="text-small text-muted">' + timeStr + '</td>' +
        '<td>' + dirBadge + '</td>' +
        '<td class="text-small">' + escapeHtml(typeLabel) + '</td>' +
        '<td class="text-small">' + escapeHtml(e.summary) + '</td></tr>';
    },

    loadActivity: function () {
      var self = this;
      // Load the backlog via REST — real-time updates come from the
      // global activityPanel SSE stream which feeds BOTH the panel
      // AND this Overview section's activity table.
      fetch("/api/activity?limit=100").then(function (r) { return r.json(); }).then(function (data) {
        var tbody = document.getElementById("activity-body");
        var events = data.events || [];
        document.getElementById("activity-log-count").textContent =
          events.length + " events (live)";
        if (!events.length) {
          tbody.innerHTML = '<tr><td colspan="4" class="text-muted">No activity yet — waiting for events...</td></tr>';
        } else {
          tbody.innerHTML = events.map(function (e) {
            return self._renderActivityRow(e);
          }).join("");
        }
      }).catch(function () {
        document.getElementById("activity-body").innerHTML =
          '<tr><td colspan="4" class="text-muted">Load failed</td></tr>';
      });
    },

    loadStats: function () {
      fetch("/api/stats").then(function (r) { return r.json(); }).then(function (data) {
        var el = document.getElementById("overview-stats-windows");
        var stats = data.stats || [];
        el.innerHTML = stats.map(function (s) {
          var label = s.window_minutes < 60
            ? s.window_minutes + "m"
            : (s.window_minutes / 60) + "h";
          var kb = s.bytes >= 1024
            ? (s.bytes / 1024).toFixed(1) + " KB"
            : s.bytes + " B";
          return '<div class="stat">' +
            '<div class="stat-label">Last ' + label + '</div>' +
            '<div class="stat-value">' + s.messages + '</div>' +
            '<div class="stat-hint">' + kb + '</div></div>';
        }).join("");
      });
    },
  },

  // -- Broadcast Control section --------------------------------------------

  broadcast: {
    _jobsLoaded: false,
    _metaLoaded: false,
    _refreshTimer: null,
    _meta: null,

    onEnter: function () {
      if (!this._metaLoaded) this.loadMeta();
      if (!this._jobsLoaded) this.loadJobs();
      this._startAutoRefresh();
    },

    switchTab: function (tab) {
      document.querySelectorAll("#broadcast-tabs .sub-tab").forEach(function (btn) {
        btn.classList.toggle("active", btn.dataset.tab === tab);
      });
      document.getElementById("broadcast-tab-jobs").classList.toggle("active", tab === "jobs");
      document.getElementById("broadcast-tab-products").classList.toggle("active", tab === "products");
      if (tab === "products") Portal.products.onEnter();
    },

    _startAutoRefresh: function () {
      this._stopAutoRefresh();
      var self = this;
      this._refreshTimer = setInterval(function () { self.loadJobs(); }, 30000);
    },

    _stopAutoRefresh: function () {
      if (this._refreshTimer) { clearInterval(this._refreshTimer); this._refreshTimer = null; }
    },

    loadMeta: function () {
      var self = this;
      fetch("/api/schedule/meta").then(function (r) { return r.json(); }).then(function (data) {
        self._meta = data;
        self._metaLoaded = true;
      });
    },

    _productLabel: function (key) {
      var meta = this._meta || {};
      var info = (meta.product_info || {})[key];
      return info ? info.label : key;
    },

    _locationLabel: function (key) {
      var meta = this._meta || {};
      var info = (meta.location_info || {})[key];
      return info ? info.label : key;
    },

    // Parse radar location_id "3:64" → { locId: "3", gridSize: 64 }
    _parseRadarLoc: function (locId) {
      if (!locId || locId.indexOf(":") === -1) return { locId: locId || "", gridSize: 32 };
      var parts = locId.split(":");
      return { locId: parts[0].trim(), gridSize: parseInt(parts[1], 10) || 32 };
    },

    loadJobs: function () {
      var self = this;
      fetch("/api/schedule/jobs").then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.json();
      }).then(function (data) {
        self._jobsLoaded = true;
        var tbody = document.getElementById("jobs-body");
        if (!data.jobs || data.jobs.length === 0) {
          tbody.innerHTML = '<tr><td colspan="9" class="text-muted">No broadcast jobs configured.</td></tr>';
          return;
        }
        tbody.innerHTML = data.jobs.map(function (j) {
          var prodLabel = self._productLabel(j.product);
          var locLabel = self._locationLabel(j.location_type);
          var locDetail = j.location_id ? ": " + escapeHtml(j.location_id) : "";
          // For radar, show grid size if non-default
          if (j.product === "radar" && j.location_id && j.location_id.indexOf(":") !== -1) {
            var parsed = self._parseRadarLoc(j.location_id);
            locDetail = parsed.locId ? ": region " + escapeHtml(parsed.locId) : "";
            locDetail += ' <span class="badge badge-muted">' + parsed.gridSize + 'x' + parsed.gridSize + '</span>';
          } else if (j.product === "radar" && !j.location_id) {
            locDetail = ' <span class="badge badge-muted">32x32</span>';
          }
          return '<tr>' +
            '<td><strong>' + escapeHtml(j.name) + '</strong><br><code class="text-muted">' + escapeHtml(j.id) + '</code></td>' +
            '<td>' + escapeHtml(prodLabel) + '</td>' +
            '<td>' + escapeHtml(locLabel) + locDetail + '</td>' +
            '<td>' + j.interval_minutes + 'm</td>' +
            '<td>' + (j.last_run_seconds_ago != null ? Portal.ui.formatAgo(j.last_run_seconds_ago) + " ago" : "never") + '</td>' +
            '<td>' + (j.next_run_in_seconds != null ? "in " + Portal.ui.formatAgo(j.next_run_in_seconds) : "next tick") + '</td>' +
            '<td>' + j.last_bytes + ' <span class="text-muted">(' + j.last_msg_count + ' msg)</span></td>' +
            '<td><button class="btn-mini" onclick="Portal.broadcast.toggleJob(\'' + j.id + '\')">' +
              (j.enabled ? "on" : "off") + '</button></td>' +
            '<td class="actions">' +
              '<button class="btn-mini" onclick="Portal.broadcast.runNow(\'' + j.id + '\')">Run now</button> ' +
              '<button class="btn-mini" onclick="Portal.broadcast.editJob(\'' + j.id + '\')">Edit</button> ' +
              '<button class="btn-mini danger" onclick="Portal.broadcast.deleteJob(\'' + j.id + '\')">Delete</button>' +
            '</td></tr>';
        }).join("");
      }).catch(function (e) {
        Portal.ui.showToast("Failed to load jobs: " + e.message, false);
      });
    },

    openJobModal: function (mode, job) {
      var meta = this._meta || {};
      var pInfo = meta.product_info || {};
      var lInfo = meta.location_info || {};
      var isEdit = mode === "edit" && job;

      // For radar edit, split location_id into locId + gridSize
      var editLocId = isEdit ? (job.location_id || "") : "";
      var editGridSize = 32;
      if (isEdit && job.product === "radar") {
        var parsed = this._parseRadarLoc(job.location_id);
        editLocId = parsed.locId;
        editGridSize = parsed.gridSize;
      }

      var selectedProduct = isEdit ? job.product : (meta.products || [])[0] || "";
      var productOpts = (meta.products || []).map(function (p) {
        var info = pInfo[p] || {};
        var label = info.label || p;
        return '<option value="' + p + '"' + (p === selectedProduct ? ' selected' : '') +
          '>' + escapeHtml(label) + '</option>';
      }).join("");

      // Location options filtered by selected product
      var validLocs = (pInfo[selectedProduct] || {}).locations || meta.location_types || [];
      var selectedLoc = isEdit ? job.location_type : validLocs[0] || "";
      var locOpts = validLocs.map(function (t) {
        var info = lInfo[t] || {};
        return '<option value="' + t + '"' + (t === selectedLoc ? ' selected' : '') +
          '>' + escapeHtml(info.label || t) + '</option>';
      }).join("");

      var locPlaceholder = (lInfo[selectedLoc] || {}).placeholder || "";
      var showLocId = selectedLoc !== "coverage";
      var showRadarGrid = selectedProduct === "radar";

      // Radar grid size options
      var gridOpts = (meta.radar_grid_sizes || []).map(function (g) {
        return '<option value="' + g.value + '"' + (g.value === editGridSize ? ' selected' : '') +
          '>' + escapeHtml(g.label) + '</option>';
      }).join("");

      var html =
        '<h2>' + (isEdit ? "Edit broadcast job" : "New broadcast job") + '</h2>' +
        '<form onsubmit="Portal.broadcast.saveJob(event)">' +
          '<input type="hidden" id="jf-mode" value="' + mode + '">' +
          '<input type="hidden" id="jf-original-id" value="' + (isEdit ? job.id : "") + '">' +

          '<label>ID (slug)' +
            '<input type="text" id="jf-id" required pattern="[a-z0-9_-]+" maxlength="64"' +
            ' value="' + (isEdit ? escapeHtml(job.id) : "") + '"' + (isEdit ? ' readonly' : '') + '>' +
          '</label>' +

          '<label>Display name' +
            '<input type="text" id="jf-name" required maxlength="120"' +
            ' value="' + (isEdit ? escapeHtml(job.name) : "") + '">' +
          '</label>' +

          '<label>Product' +
            '<select id="jf-product" required onchange="Portal.broadcast._onProductChange()">' + productOpts + '</select>' +
            '<span class="form-hint" id="jf-product-desc">' + escapeHtml((pInfo[selectedProduct] || {}).desc || "") + '</span>' +
          '</label>' +

          '<label>Location' +
            '<select id="jf-loctype" required onchange="Portal.broadcast._onLocTypeChange()">' + locOpts + '</select>' +
          '</label>' +

          '<div id="jf-locid-group"' + (showLocId ? '' : ' style="display:none"') + '>' +
            '<label>Location ID' +
              '<input type="text" id="jf-locid" placeholder="' + escapeHtml(locPlaceholder) + '"' +
              ' value="' + escapeHtml(editLocId) + '">' +
            '</label>' +
          '</div>' +

          '<div id="jf-grid-group"' + (showRadarGrid ? '' : ' style="display:none"') + '>' +
            '<label>Radar Resolution' +
              '<select id="jf-grid">' + gridOpts + '</select>' +
              '<span class="form-hint">Higher resolution uses more airtime per broadcast</span>' +
            '</label>' +
          '</div>' +

          '<label>Interval (minutes)' +
            '<input type="number" id="jf-interval" required min="1" max="10080"' +
            ' value="' + (isEdit ? job.interval_minutes : 60) + '">' +
          '</label>' +

          '<label class="checkbox"><input type="checkbox" id="jf-enabled"' +
            (isEdit ? (job.enabled ? " checked" : "") : " checked") + '> Enabled</label>' +

          '<div class="flex gap-2 mt-4">' +
            '<button type="submit" class="btn btn-primary">Save</button>' +
            '<button type="button" class="btn" onclick="Portal.ui.closeModal()">Cancel</button>' +
          '</div>' +
        '</form>';

      Portal.ui.openModal(html);
    },

    _onProductChange: function () {
      var meta = this._meta || {};
      var pInfo = meta.product_info || {};
      var lInfo = meta.location_info || {};
      var product = document.getElementById("jf-product").value;
      var info = pInfo[product] || {};

      // Update product description
      document.getElementById("jf-product-desc").textContent = info.desc || "";

      // Rebuild location dropdown with valid options for this product
      var validLocs = info.locations || meta.location_types || [];
      var locSel = document.getElementById("jf-loctype");
      var currentLoc = locSel.value;
      locSel.innerHTML = validLocs.map(function (t) {
        var li = lInfo[t] || {};
        return '<option value="' + t + '">' + escapeHtml(li.label || t) + '</option>';
      }).join("");
      // Keep current selection if still valid
      if (validLocs.indexOf(currentLoc) !== -1) {
        locSel.value = currentLoc;
      }

      // Show/hide radar grid size
      var gridGroup = document.getElementById("jf-grid-group");
      gridGroup.style.display = product === "radar" ? "" : "none";

      this._onLocTypeChange();
    },

    _onLocTypeChange: function () {
      var meta = this._meta || {};
      var lInfo = meta.location_info || {};
      var locType = document.getElementById("jf-loctype").value;
      var info = lInfo[locType] || {};

      // Show/hide location ID field
      var locGroup = document.getElementById("jf-locid-group");
      locGroup.style.display = locType === "coverage" ? "none" : "";

      // Update placeholder
      var locInput = document.getElementById("jf-locid");
      locInput.placeholder = info.placeholder || "";
    },

    saveJob: function (ev) {
      ev.preventDefault();
      var mode = document.getElementById("jf-mode").value;
      var product = document.getElementById("jf-product").value;
      var locId = document.getElementById("jf-locid").value.trim();

      // For radar jobs, encode grid size into location_id
      if (product === "radar") {
        var gridSize = parseInt(document.getElementById("jf-grid").value, 10) || 32;
        if (gridSize !== 32 && locId) {
          locId = locId + ":" + gridSize;
        } else if (gridSize !== 32 && !locId) {
          // Coverage mode with non-default grid: store as ":64" — executor
          // handles this (empty region part + grid suffix)
          locId = ":" + gridSize;
        }
        // If gridSize === 32 (default), don't append — keep location_id clean
      }

      var body = {
        id: document.getElementById("jf-id").value.trim(),
        name: document.getElementById("jf-name").value.trim(),
        product: product,
        location_type: document.getElementById("jf-loctype").value,
        location_id: locId,
        interval_minutes: parseInt(document.getElementById("jf-interval").value, 10),
        enabled: document.getElementById("jf-enabled").checked,
      };
      var origId = document.getElementById("jf-original-id").value;
      var url = mode === "edit"
        ? "/api/schedule/jobs/" + encodeURIComponent(origId)
        : "/api/schedule/jobs";
      var method = mode === "edit" ? "PUT" : "POST";
      var self = this;

      fetch(url, {
        method: method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then(function (r) {
        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || r.status); });
        return r.json();
      }).then(function () {
        Portal.ui.showToast("Job saved");
        Portal.ui.closeModal();
        self.loadJobs();
      }).catch(function (e) {
        Portal.ui.showToast("Save failed: " + e.message, false);
      });
    },

    toggleJob: function (id) {
      var self = this;
      fetch("/api/schedule/jobs/" + encodeURIComponent(id) + "/toggle", { method: "POST" })
        .then(function (r) { if (!r.ok) throw new Error(r.status); self.loadJobs(); })
        .catch(function () { Portal.ui.showToast("Toggle failed", false); });
    },

    runNow: function (id) {
      var self = this;
      fetch("/api/schedule/jobs/" + encodeURIComponent(id) + "/run-now", { method: "POST" })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (data) {
          Portal.ui.showToast("Job ran: " + data.messages_sent + " message(s) sent");
          setTimeout(function () { self.loadJobs(); }, 500);
        })
        .catch(function () { Portal.ui.showToast("Run failed", false); });
    },

    editJob: function (id) {
      var self = this;
      fetch("/api/schedule/jobs").then(function (r) { return r.json(); }).then(function (data) {
        var job = data.jobs.find(function (j) { return j.id === id; });
        if (!job) { Portal.ui.showToast("Job not found", false); return; }
        self.openJobModal("edit", job);
      });
    },

    deleteJob: function (id) {
      if (!confirm("Delete job " + id + "?")) return;
      var self = this;
      fetch("/api/schedule/jobs/" + encodeURIComponent(id), { method: "DELETE" })
        .then(function (r) { if (!r.ok) throw new Error(r.status); Portal.ui.showToast("Job deleted"); self.loadJobs(); })
        .catch(function () { Portal.ui.showToast("Delete failed", false); });
    },
  },

  // -- Products sub-tab -----------------------------------------------------

  products: {
    _filtersLoaded: false,
    _loadTimer: null,

    onEnter: function () {
      if (!this._filtersLoaded) this.loadFilters();
      this.load();
    },

    loadFilters: function () {
      var self = this;
      fetch("/api/products/filters").then(function (r) { return r.json(); }).then(function (data) {
        self._populateSelect("filter-type", data.types);
        self._populateSelect("filter-office", data.offices);
        self._populateSelect("filter-state", data.states);
        self._filtersLoaded = true;
      });
    },

    _populateSelect: function (id, items) {
      var sel = document.getElementById(id);
      var current = sel.value;
      // Keep the "All" option, replace the rest
      sel.innerHTML = '<option value="">' + sel.options[0].textContent + '</option>';
      items.forEach(function (item) {
        var opt = document.createElement("option");
        opt.value = item;
        opt.textContent = item;
        sel.appendChild(opt);
      });
      sel.value = current;
    },

    debouncedLoad: function () {
      clearTimeout(this._loadTimer);
      var self = this;
      this._loadTimer = setTimeout(function () { self.load(); }, 300);
    },

    load: function () {
      var params = new URLSearchParams({
        type: document.getElementById("filter-type").value,
        office: document.getElementById("filter-office").value,
        state: document.getElementById("filter-state").value,
        q: document.getElementById("filter-q").value,
      });
      var tbody = document.getElementById("products-tbody");
      fetch("/api/products?" + params).then(function (r) { return r.json(); }).then(function (data) {
        if (!data.products.length) {
          tbody.innerHTML = '<tr><td colspan="5"><div class="empty-state">No products match the filters.</div></td></tr>';
          document.getElementById("products-summary").textContent = "";
          return;
        }
        tbody.innerHTML = data.products.map(function (p) {
          var ts = new Date(p.timestamp);
          var tsStr = ts.toISOString().slice(0, 16).replace("T", " ");
          return '<tr onclick="Portal.products.openProduct(\'' + escapeHtml(p.filename) + '\')">' +
            '<td class="text-mono"><strong>' + escapeHtml(p.product_type) + '</strong></td>' +
            '<td class="text-mono">' + escapeHtml(p.office || "") + '</td>' +
            '<td class="text-mono">' + escapeHtml(p.state || "") + '</td>' +
            '<td class="text-small text-muted">' + tsStr + ' UTC</td>' +
            '<td class="text-small">' + escapeHtml(p.preview || "") + '</td></tr>';
        }).join("");
        document.getElementById("products-summary").textContent =
          "Showing " + data.products.length + " of possibly more (limit 100). Apply filters to narrow.";
      }).catch(function (e) {
        tbody.innerHTML = '<tr><td colspan="5">Failed: ' + escapeHtml(e.message) + '</td></tr>';
      });
    },

    openProduct: function (filename) {
      fetch("/api/products/" + encodeURIComponent(filename))
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var html =
            '<div class="card-header"><div class="card-title">' +
              escapeHtml(data.emwin_id) + ' \u2014 ' + escapeHtml(data.product_type) +
            '</div><button class="btn" onclick="Portal.ui.closeModal()">Close</button></div>' +
            '<pre class="text-mono" style="white-space:pre-wrap;background:var(--color-bg);padding:var(--space-4);border-radius:var(--radius-sm);max-height:60vh;overflow:auto;">' +
              escapeHtml(data.raw_text) + '</pre>';
          Portal.ui.openModal(html, true);
        })
        .catch(function (e) { Portal.ui.showToast("Failed: " + e.message, false); });
    },
  },

  // -- Weather Map section --------------------------------------------------

  weatherMap: {
    _map: null,
    _mapReady: false,
    _refreshTimer: null,
    _firstLoad: true,

    init: function () {
      // Lazy — MapLibre throws if container has 0 dimensions (hidden section).
      // Actual creation deferred to first onEnter().
    },

    _ensureMap: function () {
      if (this._map) return;
      this._map = createWeatherMap("map");
      var self = this;
      this._map.on("load", function () {
        self._mapReady = true;
        var boot = window.__BOOT__;
        if (boot._coverageBbox) addCoverageLayer(self._map, boot._coverageBbox);
        self.loadAndRender();
      });
    },

    onEnter: function () {
      this._ensureMap();
      if (this._map) {
        requestAnimationFrame(function () {
          Portal.weatherMap._map.resize();
          // Load data after resize if map was already ready
          if (Portal.weatherMap._mapReady) Portal.weatherMap.loadAndRender();
        });
      }
      this._startAutoRefresh();
    },

    onLeave: function () {
      this._stopAutoRefresh();
    },

    _startAutoRefresh: function () {
      this._stopAutoRefresh();
      var self = this;
      this._refreshTimer = setInterval(function () { self.loadAndRender(); }, 60000);
    },

    _stopAutoRefresh: function () {
      if (this._refreshTimer) { clearInterval(this._refreshTimer); this._refreshTimer = null; }
    },

    loadAndRender: function () {
      if (!this._mapReady) return;
      var self = this;
      fetch("/api/warnings").then(function (r) { return r.json(); }).then(function (data) {
        addWarningsLayer(self._map, data.warnings);

        // Build legend
        var typesPresent = [];
        var seen = {};
        data.warnings.forEach(function (w) {
          if (!seen[w.warning_type]) { seen[w.warning_type] = true; typesPresent.push(w.warning_type); }
        });
        typesPresent.sort(function (a, b) { return a - b; });

        var legendHtml = '<div class="legend-title">Active Warnings</div>';
        if (typesPresent.length === 0) {
          legendHtml = '<div class="legend-title">No Active Warnings</div>';
        } else {
          typesPresent.forEach(function (t) {
            legendHtml += '<div class="legend-item">' +
              '<span class="legend-swatch" style="background:' + (WARNING_COLORS[t] || "#9ca3af") + '"></span>' +
              (WARNING_TYPE_NAMES[t] || "Unknown") + '</div>';
          });
        }
        document.getElementById("map-legend").innerHTML = legendHtml;

        var inCov = data.warnings.filter(function (w) { return w.in_coverage; }).length;
        document.getElementById("map-status").innerHTML =
          data.count + " active &middot; <strong>" + inCov + "</strong> in coverage";

        // On first load, fit to coverage bbox if available
        if (self._firstLoad) {
          self._firstLoad = false;
          var boot = window.__BOOT__;
          if (boot._coverageBbox) {
            var b = boot._coverageBbox;
            self._map.fitBounds([[b[2], b[1]], [b[3], b[0]]], { padding: 40, maxZoom: 6, duration: 0 });
          }
        }
      }).catch(function (e) {
        document.getElementById("map-status").textContent = "Load failed";
        console.error(e);
      });
    },
  },

  // -- System section -------------------------------------------------------

  system: {
    _previewMap: null,
    _previewReady: false,
    _initialized: false,

    onEnter: function () {
      if (!this._initialized) {
        this._initialized = true;
        this.render(window.__BOOT__);
        this.initPreviewMap();
        this.loadRadarGrid();
        this.loadChannels();
      }
    },

    render: function (boot) {
      var src = boot.coverage_sources || {};
      this._renderTags("sys-cities", src.cities || []);
      this._renderTags("sys-states", src.states || []);
      this._renderTags("sys-wfos", src.wfos || []);
      document.getElementById("sys-coverage-summary").textContent =
        boot.coverage_summary || "No coverage filter set \u2014 broadcasting for the entire CONUS.";
    },

    _renderTags: function (elId, items) {
      var el = document.getElementById(elId);
      if (!items.length) {
        el.innerHTML = '<span class="text-muted text-small" style="padding:4px 8px;">None</span>';
        return;
      }
      el.innerHTML = items.map(function (item) {
        return '<span class="tag">' + escapeHtml(item) + '</span>';
      }).join("");
    },

    initPreviewMap: function () {
      this._previewMap = createWeatherMap("sys-preview-map", { zoom: 3, center: [-96, 38] });
      var self = this;
      this._previewMap.on("load", function () {
        self._previewReady = true;
        self.loadCoveragePreview();
      });
    },

    loadCoveragePreview: function () {
      if (!this._previewReady) return;
      var boot = window.__BOOT__;
      var src = boot.coverage_sources || {};
      var params = new URLSearchParams({
        cities: (src.cities || []).join(","),
        states: (src.states || []).join(","),
        wfos: (src.wfos || []).join(","),
      });
      var self = this;
      fetch("/api/coverage/preview?" + params).then(function (r) { return r.json(); }).then(function (data) {
        document.getElementById("sys-preview-summary").textContent = data.summary;
        if (data.bbox) {
          addCoverageLayer(self._previewMap, data.bbox);
          var b = data.bbox;
          self._previewMap.fitBounds([[b[2], b[1]], [b[3], b[0]]], { padding: 40, maxZoom: 6, duration: 400 });
          // Store for weather map too
          window.__BOOT__._coverageBbox = data.bbox;
        }
      }).catch(function () {
        document.getElementById("sys-preview-summary").textContent = "Preview failed";
      });
    },

    loadRadarGrid: function () {
      fetch("/api/status").then(function (r) { return r.json(); }).then(function (data) {
        var gridSize = (data.settings || {}).radar_grid_size || 32;
        document.getElementById("sys-radar-grid").value = String(gridSize);
        document.getElementById("sys-radar-status").textContent =
          "Current: " + gridSize + "x" + gridSize;
      });
    },

    saveRadarGrid: function () {
      var gridSize = parseInt(document.getElementById("sys-radar-grid").value, 10);
      var statusEl = document.getElementById("sys-radar-status");
      statusEl.textContent = "Saving\u2026";
      fetch("/api/settings/radar-grid-size", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ radar_grid_size: gridSize }),
      }).then(function (r) {
        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || r.status); });
        return r.json();
      }).then(function () {
        statusEl.innerHTML = '<span style="color:var(--color-success);">Saved: ' + gridSize + 'x' + gridSize + '</span>';
        Portal.ui.showToast("Radar resolution updated to " + gridSize + "x" + gridSize);
      }).catch(function (e) {
        statusEl.innerHTML = '<span style="color:var(--color-danger);">' + escapeHtml(e.message) + '</span>';
      });
    },

    loadChannels: function () {
      var boot = window.__BOOT__;
      var cfg = boot.channel_config || {};
      document.getElementById("sys-ch-text").value = cfg.text_channel || "";
      document.getElementById("sys-ch-data").value = cfg.data_channel || "";
      document.getElementById("sys-ch-v4").value = cfg.v4_channel || "";
      // Show active channel indices
      var parts = [];
      if (boot.channel_idx != null) parts.push("text: ch" + boot.channel_idx);
      if (boot.data_channel != null) parts.push("v3 data: ch" + boot.data_channel);
      if (boot.v4_channel != null) parts.push("v4: ch" + boot.v4_channel);
      document.getElementById("sys-ch-status").textContent = parts.length
        ? "Active: " + parts.join(", ")
        : "No data channels active";
    },

    saveChannels: function (btn) {
      var statusEl = document.getElementById("sys-ch-status");
      statusEl.textContent = "Saving\u2026";
      btn.disabled = true;
      var body = {
        text_channel: document.getElementById("sys-ch-text").value.trim(),
        data_channel: document.getElementById("sys-ch-data").value.trim(),
        v4_channel: document.getElementById("sys-ch-v4").value.trim(),
      };
      fetch("/api/settings/channels", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then(function (r) {
        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || r.status); });
        return r.json();
      }).then(function () {
        statusEl.innerHTML = '<span style="color:var(--color-success);">Saved. Restart bot to apply.</span>';
        Portal.ui.showToast("Channel config saved. Restart the bot to apply changes.");
      }).catch(function (e) {
        statusEl.innerHTML = '<span style="color:var(--color-danger);">' + escapeHtml(e.message) + '</span>';
      }).finally(function () { btn.disabled = false; });
    },
  },

  // -- Shared actions -------------------------------------------------------

  actions: {
    broadcast: function (btn) {
      btn.disabled = true;
      var resultEls = [
        document.getElementById("overview-action-result"),
        document.getElementById("sys-action-result"),
      ].filter(Boolean);

      resultEls.forEach(function (el) { el.textContent = "Running\u2026"; });

      fetch("/api/actions/broadcast", { method: "POST" }).then(function (r) {
        if (r.ok) {
          resultEls.forEach(function (el) {
            el.innerHTML = '<span style="color:var(--color-success);">Done</span>';
          });
          Portal.ui.showToast("Broadcast triggered");
        } else {
          return r.json().then(function (d) { throw new Error(d.detail || r.statusText); });
        }
      }).catch(function (e) {
        resultEls.forEach(function (el) {
          el.innerHTML = '<span style="color:var(--color-danger);">' + escapeHtml(e.message) + '</span>';
        });
      }).finally(function () { btn.disabled = false; });
    },

    v2Request: function (btn) {
      btn.disabled = true;
      var resultEl = document.getElementById("sys-action-result");
      resultEl.textContent = "Sending\u2026";

      fetch("/api/actions/v2-request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          data_type: document.getElementById("v2-data-type").value,
          location: document.getElementById("v2-location").value.trim(),
        }),
      }).then(function (r) {
        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || r.status); });
        return r.json();
      }).then(function (data) {
        resultEl.innerHTML = '<span style="color:var(--color-success);">Sent ' +
          escapeHtml(data.data_type) + ' for ' + escapeHtml(JSON.stringify(data.location)) + '</span>';
      }).catch(function (e) {
        resultEl.innerHTML = '<span style="color:var(--color-danger);">' + escapeHtml(e.message) + '</span>';
      }).finally(function () { btn.disabled = false; });
    },
  },

  // -- Init -----------------------------------------------------------------

  // -- Persistent Activity Panel (visible on all pages) ----------------------

  activityPanel: {
    _sse: null,
    _count: 0,
    _MAX_ROWS: 150,

    init: function () {
      var self = this;
      // Load backlog then start SSE
      fetch("/api/activity?limit=50").then(function (r) { return r.json(); }).then(function (data) {
        var events = data.events || [];
        var tbody = document.getElementById("panel-activity-body");
        self._count = events.length;
        if (!events.length) {
          tbody.innerHTML = '<tr><td colspan="4" class="text-muted">Waiting for events...</td></tr>';
        } else {
          tbody.innerHTML = events.map(function (e) { return self._row(e); }).join("");
        }
        self._updateCount();
        self._startSSE();
      }).catch(function () {});
    },

    toggle: function () {
      var panel = document.getElementById("activity-panel");
      if (panel.classList.contains("expanded")) {
        panel.classList.remove("expanded");
        panel.classList.add("collapsed");
      } else {
        panel.classList.remove("collapsed");
        panel.classList.add("expanded");
      }
    },

    _row: function (e) {
      var ts = new Date((e.ts || 0) * 1000);
      var time = ts.toLocaleTimeString();
      var dir = e.direction === "in"
        ? '<span class="badge badge-success">IN</span>'
        : '<span class="badge badge-muted">OUT</span>';
      var labels = {
        v2_request: "Request", v2_response: "Response", v1_refresh: "Refresh",
        broadcast: "Broadcast", throttled: "Throttled", send_fail: "Send Fail",
      };
      var type = labels[e.event_type] || e.event_type;
      return '<tr><td class="text-muted">' + time + '</td><td>' + dir +
        '</td><td>' + escapeHtml(type) + '</td><td>' + escapeHtml(e.summary) + '</td></tr>';
    },

    _updateCount: function () {
      var el = document.getElementById("panel-event-count");
      if (el) el.textContent = "(" + this._count + " events)";
    },

    _startSSE: function () {
      if (this._sse) { this._sse.close(); this._sse = null; }
      var self = this;
      var es = new EventSource("/api/activity/stream");
      this._sse = es;

      es.onmessage = function (msg) {
        try {
          var e = JSON.parse(msg.data);
          var tbody = document.getElementById("panel-activity-body");
          if (!tbody) return;
          // Remove placeholder
          var ph = tbody.querySelector("td[colspan]");
          if (ph) tbody.innerHTML = "";
          // Prepend
          var tmp = document.createElement("div");
          tmp.innerHTML = '<table><tbody>' + self._row(e) + '</tbody></table>';
          var tr = tmp.querySelector("tr");
          if (tr) {
            tr.classList.add("row-new");
            tbody.insertBefore(tr, tbody.firstChild);
          }
          self._count++;
          self._updateCount();
          // Trim
          while (tbody.children.length > self._MAX_ROWS) tbody.removeChild(tbody.lastChild);

          // Also update the Overview page's activity log if it exists
          var overviewBody = document.getElementById("activity-body");
          if (overviewBody && overviewBody !== tbody) {
            var tmp2 = document.createElement("div");
            tmp2.innerHTML = '<table><tbody>' + Portal.overview._renderActivityRow(e) + '</tbody></table>';
            var tr2 = tmp2.querySelector("tr");
            if (tr2) {
              tr2.style.backgroundColor = "rgba(0, 150, 255, 0.15)";
              overviewBody.insertBefore(tr2, overviewBody.firstChild);
              setTimeout(function () { tr2.style.backgroundColor = ""; }, 1500);
            }
            while (overviewBody.children.length > 200) overviewBody.removeChild(overviewBody.lastChild);
            var countEl = document.getElementById("activity-log-count");
            if (countEl) countEl.textContent = overviewBody.children.length + " events (live)";
          }
        } catch (err) {}
      };

      es.onerror = function () {
        var el = document.getElementById("panel-event-count");
        if (el) el.textContent = "(reconnecting...)";
      };
      es.onopen = function () { self._updateCount(); };
    },
  },

  init: function () {
    var boot = window.__BOOT__ || {};

    // Render overview from boot data immediately
    this.overview.render(boot);

    // Start the persistent activity panel SSE stream
    this.activityPanel.init();

    // Map init is lazy — created on first visit to #map section
    // (MapLibre throws if container has 0 dimensions while hidden)

    // Keyboard shortcut: Escape closes modal
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") Portal.ui.closeModal();
    });

    // Start router (reads hash, activates correct section)
    this.router.init();
  },
};

// Boot
document.addEventListener("DOMContentLoaded", function () { Portal.init(); });
