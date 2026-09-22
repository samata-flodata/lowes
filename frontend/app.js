/* ==========================================================================
 * Lowe's Interactive Store Map - frontend application logic.
 *
 * Talks to the FastAPI backend for the store directory and renders the raw
 * Lowes_*.geojson floor plan on a Leaflet map over OpenStreetMap tiles.
 * ========================================================================== */

const DEFAULT_CENTER = [39.5, -98.35];
const DEFAULT_ZOOM = 18;

// Zoom thresholds driving progressive level-of-detail (see spec).
const ZOOM = {
    STORE_LEVEL: 18,
    DEPT_LABELS: 18,
    DEPT_LABELS_ALL: 20,
    AISLE_LABELS: 18,
    RACK_DETAIL: 22,
    MAX_DETAIL: 23,
};

const BASIC_INFO_ICONS = {
    returns: "↩",              // ↩
    restrooms: "🚻",       // 🚻
    checkouts: "💳",       // 💳
    store_pickup: "📦",    // 📦
    entrance_exit: "🚪",   // 🚪
    pickup_lockers: "🔐",  // 🔐
    pro_service_desk: "👷", // 👷
    customer_service_desk: "ℹ", // ℹ
};

const STORE_SERVICE_ICONS = {
    key_copying: "🔑",       // 🔑
    millwork_desk: "🛋",     // 🛋
    wood_cutting: "🪚",       // 🪚
    wire_cutting: "⚡",            // ⚡
    flooring_desk: "📐",      // 📐
    blind_cutting: "🪟",      // 🪟
    glass_cutting: "🕳",      // 🕳ish - pane
    carpet_cutting: "✂",          // ✂
    appliance_desk: "🔌",     // 🔌
    home_decor_desk: "🏠",    // 🏠
    chain_rope_cutting: "⛓",       // ⛓
    kitchen_design_desk: "🍽", // 🍽
};

const FLOORPLAN_LAYER_KEYS = [
    "departments", "departmentLines", "departmentPoints", "aisles",
    "aisleLines", "aislePoints", "racks", "rackLines",
];

function iconFor(category, markerType) {
    if (category === "basic_information") return BASIC_INFO_ICONS[markerType] || "ℹ";
    if (category === "store_service") return STORE_SERVICE_ICONS[markerType] || "🔧";
    return "•";
}

function humanize(s) {
    if (!s) return "";
    return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/* ---------------------------------------------------------------------- */
/* App state                                                              */
/* ---------------------------------------------------------------------- */

const state = {
    map: null,
    stores: [],
    currentStoreId: null,
    currentStore: null,
    targetMarker: null,
    menardsOverlay: null,
    menardsState: null,

    layers: {
        departments: null,
        deptLabels: null,
        aisles: null,
        aisleLabels: null,
        racks: null,
        departmentLines: null,
        aisleLines: null,
        rackLines: null,
        rackLabels: null,
        markers: null,   // array of {layer, minZoom}
        storePin: null,
        osmTile: null,
        satelliteTile: null,
        menards: null,
    },
};

const TARGET_LOCATION = {
    lat: 46.37224090211963,
    lng: -94.24146083062027,
};

const MENARDS_STORAGE_KEY = "lowes-menards-overlay-v1";

function getDefaultMenardsState() {
    return {
        visible: true,
        offset_x: 0,
        offset_y: 0,
        width_meters: 125,
        height_meters: 90,
        scale: 1,
        rotation: 0,
    };
}

function loadMenardsState() {
    try {
        const raw = localStorage.getItem(MENARDS_STORAGE_KEY);
        if (!raw) return getDefaultMenardsState();
        const parsed = JSON.parse(raw);
        return { ...getDefaultMenardsState(), ...parsed };
    } catch (error) {
        console.warn("Menards overlay state could not be loaded:", error);
        return getDefaultMenardsState();
    }
}

function saveMenardsState() {
    if (!state.menardsState) return;
    try {
        localStorage.setItem(MENARDS_STORAGE_KEY, JSON.stringify(state.menardsState));
    } catch (error) {
        console.warn("Menards overlay state could not be saved:", error);
    }
}

function getMenardsBounds() {
    if (!state.menardsState) return null;

    const metersPerLat = 111_320;
    const metersPerLon = 111_320 * Math.cos(TARGET_LOCATION.lat * Math.PI / 180);
    const centerLat = TARGET_LOCATION.lat + (state.menardsState.offset_y || 0) / metersPerLat;
    const centerLng = TARGET_LOCATION.lng + (state.menardsState.offset_x || 0) / metersPerLon;
    const widthMeters = (state.menardsState.width_meters || 120) * (state.menardsState.scale || 1);
    const heightMeters = (state.menardsState.height_meters || 90) * (state.menardsState.scale || 1);
    const halfWidthDeg = widthMeters / (2 * metersPerLon);
    const halfHeightDeg = heightMeters / (2 * metersPerLat);

    return L.latLngBounds(
        [centerLat - halfHeightDeg, centerLng - halfWidthDeg],
        [centerLat + halfHeightDeg, centerLng + halfWidthDeg],
    );
}

function updateMenardsOverlayState() {
    if (!state.map || !state.menardsOverlay || !state.menardsState) return;

    const bounds = getMenardsBounds();
    if (bounds) state.menardsOverlay.setBounds(bounds);

    const svg = state.menardsOverlay.getElement();
    if (!svg) return;

    const root = svg.querySelector("svg") || svg;
    root.setAttribute("preserveAspectRatio", "xMidYMid meet");
    root.style.pointerEvents = "none";

    const svgWidth = 2277.64;
    const svgHeight = 1728;
    const cx = svgWidth / 2;
    const cy = svgHeight / 2;
    const rotation = Number(state.menardsState.rotation || 0);
    const scale = Number(state.menardsState.scale || 1);

    const g = svg.querySelector("g") || root;
    g.setAttribute("transform", `rotate(${rotation} ${cx} ${cy}) scale(${scale} ${scale})`);
}

function setMenardsVisible(visible) {
    if (!state.map || !state.menardsOverlay) return;
    if (visible && !state.map.hasLayer(state.menardsOverlay)) state.map.addLayer(state.menardsOverlay);
    if (!visible && state.map.hasLayer(state.menardsOverlay)) state.map.removeLayer(state.menardsOverlay);
}

function applyMenardsAdjustment(action) {
    const stateValue = state.menardsState || getDefaultMenardsState();
    state.menardsState = stateValue;
    const step = Number(document.getElementById("menards-step")?.value || 5);

    if (action === "north") stateValue.offset_y = Number(stateValue.offset_y || 0) + step;
    if (action === "south") stateValue.offset_y = Number(stateValue.offset_y || 0) - step;
    if (action === "east") stateValue.offset_x = Number(stateValue.offset_x || 0) + step;
    if (action === "west") stateValue.offset_x = Number(stateValue.offset_x || 0) - step;
    if (action === "width-up") stateValue.width_meters = Number(stateValue.width_meters || 120) + 5;
    if (action === "width-down") stateValue.width_meters = Math.max(20, Number(stateValue.width_meters || 120) - 5);
    if (action === "height-up") stateValue.height_meters = Number(stateValue.height_meters || 90) + 5;
    if (action === "height-down") stateValue.height_meters = Math.max(20, Number(stateValue.height_meters || 90) - 5);
    if (action === "rotate-cw") stateValue.rotation = Number(stateValue.rotation || 0) + step;
    if (action === "rotate-ccw") stateValue.rotation = Number(stateValue.rotation || 0) - step;
    if (action === "scale-up") stateValue.scale = Number(stateValue.scale || 1) + 0.05;
    if (action === "scale-down") stateValue.scale = Math.max(0.25, Number(stateValue.scale || 1) - 0.05);
    if (action === "reset") state.menardsState = getDefaultMenardsState();

    updateMenardsOverlayState();
    saveMenardsState();
    const panel = document.getElementById("menards-status");
    if (panel) panel.textContent = `Offset ${state.menardsState.offset_x.toFixed(0)}, ${state.menardsState.offset_y.toFixed(0)} m · scale ${state.menardsState.scale.toFixed(2)} · rotation ${state.menardsState.rotation.toFixed(0)}°`;
}

async function loadMenardsFloorPlanOverlay() {
    if (!state.map) return;

    state.menardsState = loadMenardsState();
    const storeUrl = "https://www.menards.com/store-details/store.html?store=3065";

    try {
        let metadata = null;
        try {
            metadata = await fetchJSON("/api/menards/3065", { cache: "no-store" });
        } catch (error) {
            metadata = await fetchJSON("/api/menards/fetch", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ store_url: storeUrl, force_refresh: false }),
                cache: "no-store",
            });
        }

        if (!metadata || !metadata.store_id) {
            throw new Error("Menards store metadata not available");
        }

        const svgResponse = await fetch(`/api/menards/${encodeURIComponent(metadata.store_id)}/svg`, { cache: "no-store" });
        if (!svgResponse.ok) {
            throw new Error(`SVG endpoint returned ${svgResponse.status}`);
        }

        const svgText = await svgResponse.text();
        const parser = new DOMParser();
        const doc = parser.parseFromString(svgText, "image/svg+xml");
        const svg = doc.documentElement;
        if (!svg || svg.nodeName.toLowerCase() !== "svg") {
            throw new Error("Downloaded Menards SVG is invalid");
        }

        const bgPath = svg.querySelector('path[d="M0 0h2277.64v1728H0z"]');
        if (bgPath) bgPath.remove();
        svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");

        const metersPerLat = 111_320;
        const metersPerLon = 111_320 * Math.cos((metadata.latitude || TARGET_LOCATION.lat) * Math.PI / 180);
        const widthMeters = Number(metadata.width_meters || state.menardsState.width_meters || 125);
        const heightMeters = Number(metadata.height_meters || state.menardsState.height_meters || 90);
        const halfWidthDeg = widthMeters / (2 * metersPerLon);
        const halfHeightDeg = heightMeters / (2 * metersPerLat);
        const centerLat = Number(metadata.latitude || TARGET_LOCATION.lat);
        const centerLng = Number(metadata.longitude || TARGET_LOCATION.lng);

        const bounds = L.latLngBounds(
            [centerLat - halfHeightDeg, centerLng - halfWidthDeg],
            [centerLat + halfHeightDeg, centerLng + halfWidthDeg],
        );

        const overlay = L.svgOverlay(svg, bounds, {
            opacity: 0.9,
            interactive: false,
            className: "menards-floorplan-overlay",
        }).addTo(state.map);

        state.map.setView([centerLat, centerLng], 18, { animate: true });
        state.menardsOverlay = overlay;
        state.menardsState.visible = state.menardsState.visible !== false;
        setMenardsVisible(state.menardsState.visible);
        updateMenardsOverlayState();
    } catch (error) {
        console.error("Menards floor-plan overlay could not be created:", error);
    }
}

function setupMenardsControls() {
    const checkbox = document.getElementById("layer-menards");
    const status = document.getElementById("menards-status");
    if (checkbox) {
        checkbox.checked = true;
        checkbox.addEventListener("change", () => {
            if (!state.menardsState) state.menardsState = getDefaultMenardsState();
            state.menardsState.visible = checkbox.checked;
            setMenardsVisible(checkbox.checked);
            saveMenardsState();
            if (status) status.textContent = checkbox.checked ? "Menards floor plan enabled" : "Menards floor plan disabled";
        });
    }

    document.querySelectorAll("[data-menards-action]").forEach((button) => {
        button.addEventListener("click", () => applyMenardsAdjustment(button.dataset.menardsAction));
    });

    const saveButton = document.getElementById("save-menards-alignment");
    if (saveButton) {
        saveButton.addEventListener("click", () => {
            if (!state.menardsState) state.menardsState = getDefaultMenardsState();
            saveMenardsState();
            if (status) status.textContent = "Menards alignment saved";
        });
    }
}

function createTargetMarkerIcon() {
    return L.icon({
        iconUrl: "./custom-target-pin.svg",
        iconSize: [42, 42],
        iconAnchor: [21, 41],
        popupAnchor: [0, -38],
        tooltipAnchor: [0, -36],
        shadowUrl: "",
    });
}

function addTargetMarker() {
    if (!state.map) return;

    if (state.targetMarker) {
        state.map.removeLayer(state.targetMarker);
        state.targetMarker = null;
    }

    const pin = L.marker([TARGET_LOCATION.lat, TARGET_LOCATION.lng], {
        icon: createTargetMarkerIcon(),
        title: "Target location",
        keyboard: true,
    }).addTo(state.map);

    pin.bindPopup(`<strong>Target location</strong><br>${TARGET_LOCATION.lat}, ${TARGET_LOCATION.lng}`);
    pin.bindTooltip("Target location", { direction: "top", offset: [0, -18] });

    state.targetMarker = pin;
    state.map.setView([TARGET_LOCATION.lat, TARGET_LOCATION.lng], 18, { animate: true });
    return pin;
}

function applyPanelCollapseState(panelId, collapsed) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    panel.classList.toggle('collapsed', collapsed);
    const button = panel.querySelector('.panel-toggle');
    if (button) {
        button.textContent = collapsed ? '+' : '−';
        button.setAttribute('aria-label', collapsed ? `Expand ${panelId} panel` : `Collapse ${panelId} panel`);
        button.setAttribute('aria-expanded', String(!collapsed));
    }
    try {
        sessionStorage.setItem(`map-panel:${panelId}:collapsed`, String(collapsed));
    } catch (error) {
        console.warn('Could not save panel collapse state:', error);
    }
}

function wireCollapsiblePanels() {
    document.querySelectorAll('.panel-toggle').forEach((button) => {
        const panelId = button.dataset.panel;
        const panel = document.getElementById(panelId);
        if (!panel) return;

        let collapsed = false;
        try {
            collapsed = sessionStorage.getItem(`map-panel:${panelId}:collapsed`) === 'true';
        } catch (error) {
            console.warn('Could not read panel collapse state:', error);
        }
        applyPanelCollapseState(panelId, collapsed);

        button.addEventListener('click', () => {
            const nextCollapsed = !panel.classList.contains('collapsed');
            applyPanelCollapseState(panelId, nextCollapsed);
            if (state.map) {
                window.setTimeout(() => state.map.invalidateSize(), 50);
            }
        });
    });
}

/* ---------------------------------------------------------------------- */
/* Map bootstrap                                                          */
/* ---------------------------------------------------------------------- */

function initMap() {
    const map = L.map("map", { zoomControl: true }).setView(DEFAULT_CENTER, 4);

    // Base layers - OpenStreetMap and Esri World Imagery satellite, both
    // public tile services requiring no API key. Only one is shown at a
    // time; toggled via the "Satellite basemap" checkbox in the layer panel,
    // which is checked by default, so satellite is the initial basemap.
    state.layers.osmTile = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 23,
        maxNativeZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    });

    state.layers.satelliteTile = L.tileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        {
            maxZoom: 23,
            maxNativeZoom: 19,
            attribution: "Tiles &copy; Esri",
        }
    ).addTo(map);

    state.layers.departments = L.geoJSON(null, { style: departmentStyle, onEachFeature: onEachDepartment }).addTo(map);
    state.layers.deptLabels = L.layerGroup().addTo(map);
    state.layers.aisles = L.geoJSON(null, { style: aisleStyle, onEachFeature: onEachAisle }).addTo(map);
    state.layers.aisleLabels = L.layerGroup().addTo(map);
    state.layers.racks = L.geoJSON(null, { style: rackStyle, onEachFeature: onEachRack }).addTo(map);
    state.layers.departmentLines = L.geoJSON(null, { style: departmentLineStyle }).addTo(map);
    state.layers.aisleLines = L.geoJSON(null, { style: aisleLineStyle }).addTo(map);
    state.layers.rackLines = L.geoJSON(null, { style: rackLineStyle }).addTo(map);
    state.layers.rackLabels = L.layerGroup().addTo(map);
    state.layers.markers = [];
    state.layers.markerGroup = L.layerGroup().addTo(map);
    state.layers.menards = L.layerGroup();

    map.on("zoomend", updateZoomVisibility);
    state.map = map;
}

/* ---------------------------------------------------------------------- */
/* Feature styling                                                        */
/* ---------------------------------------------------------------------- */

function departmentStyle(feature) {
    return geoJsonStyle({ color: "#96A3BD", weight: 1, fillColor: "#D9E2F6", fillOpacity: 0.75 }, feature);
}

function aisleStyle(feature) {
    return geoJsonStyle({ color: "#BBBBBB", weight: 1, fillColor: "#DCDEE2", fillOpacity: 0.7 }, feature);
}

function rackStyle(feature) {
    return geoJsonStyle({ color: "#9ca3af", weight: 1, fillColor: "#DCDEE2", fillOpacity: 0.8 }, feature);
}

function departmentLineStyle(feature) {
    return geoJsonStyle({ color: "#6b7280", weight: 1.5, opacity: 0.95 }, feature);
}

function aisleLineStyle(feature) {
    return geoJsonStyle({ color: "#8b949e", weight: 1, opacity: 0.9 }, feature);
}

function rackLineStyle(feature) {
    return geoJsonStyle({ color: "#4b5563", weight: 0.8, opacity: 0.85 }, feature);
}

function geoJsonStyle(defaults, feature = null) {
    const props = feature?.properties || {};
    const fillOpacity = props.opacity != null ? Number(props.opacity) : defaults.fillOpacity;
    return {
        color: props.stroke || defaults.color,
        weight: Number(props["stroke-width"]) || defaults.weight,
        opacity: defaults.opacity != null ? defaults.opacity : 1,
        fillColor: props.fill || defaults.fillColor,
        fillOpacity: Number.isFinite(fillOpacity) ? fillOpacity : defaults.fillOpacity,
    };
}

function onEachDepartment(feature, layer) {
    const name = feature.properties.label || feature.properties.name || feature.properties.poi_name || "Department";
    layer.bindPopup(`<b>${name}</b>${feature.properties.category ? humanize(feature.properties.category) : ""}`);
    if (!layer.getBounds().isValid()) return;
    const center = layer.getBounds().getCenter();
    const label = L.marker(center, {
        interactive: false,
        icon: L.divIcon({ className: "dept-label", html: name, iconSize: null }),
    });
    label._minZoom = ZOOM.DEPT_LABELS;
    state.layers.deptLabels.addLayer(label);
}

function onEachAisle(feature, layer) {
    const name = feature.properties.label || feature.properties.name || "Aisle";
    layer.bindPopup(`<b>${name}</b>`);
    if (!layer.getBounds().isValid()) return;
    const center = layer.getBounds().getCenter();
    const label = L.marker(center, {
        interactive: false,
        icon: L.divIcon({ className: "aisle-label", html: name, iconSize: null }),
    });
    label._minZoom = ZOOM.AISLE_LABELS;
    state.layers.aisleLabels.addLayer(label);
}

function onEachRack(feature, layer) {
    const name = feature.properties.label || feature.properties.name || "Rack";
    const info = feature.properties.info || "";
    layer.bindPopup(`<b>${name}</b>${info}`);
    if (!layer.getBounds().isValid()) return;
    const center = layer.getBounds().getCenter();
    const label = L.marker(center, {
        interactive: false,
        icon: L.divIcon({ className: "rack-label", html: info || name, iconSize: null }),
    });
    label._minZoom = ZOOM.RACK_DETAIL;
    state.layers.rackLabels.addLayer(label);
}

function buildMarkerLayer(feature) {
    const [lon, lat] = feature.geometry.coordinates;
    const props = feature.properties;
    const emoji = iconFor(props.category, props.marker_type);
    const marker = L.marker([lat, lon], {
        icon: L.divIcon({
            className: "",
            html: `<div class="marker-icon ${props.category}">${emoji}</div>`,
            iconSize: [26, 26],
            iconAnchor: [13, 13],
        }),
    });
    marker.bindPopup(`<b>${props.label || humanize(props.marker_type)}</b>${humanize(props.category)}`);
    return { layer: marker, minZoom: props.min_zoom || ZOOM.STORE_LEVEL };
}

function buildGeoJsonPointLayer(feature) {
    const [lon, lat] = feature.geometry.coordinates;
    const props = feature.properties || {};
    const label = props.poi_name || props.name || props.label || "";
    if (props.icon_id || props.poi_name) {
        const category = props.profile === "STORE SERVICES" ? "store_service" : "basic_information";
        const markerType = normalizeIconId(props.icon_id || props.sub_category);
        const emoji = iconFor(category, markerType);
        const marker = L.marker([lat, lon], {
            icon: L.divIcon({
                className: "",
                html: `<div class="marker-icon ${category}">${emoji}</div>`,
                iconSize: [26, 26],
                iconAnchor: [13, 13],
            }),
        });
        marker.bindPopup(`<b>${label || humanize(markerType)}</b>${props.profile ? humanize(props.profile.toLowerCase()) : ""}`);
        return { layer: marker, minZoom: ZOOM.STORE_LEVEL };
    }

    return {
        layer: L.marker([lat, lon], {
            interactive: false,
            icon: L.divIcon({ className: "aisle-label", html: label, iconSize: null }),
        }),
        minZoom: ZOOM.DEPT_LABELS,
    };
}

function normalizeIconId(value) {
    return String(value || "")
        .toLowerCase()
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "")
        .replace(/pick_up/g, "pickup")
        .replace(/[^a-z0-9]+/g, "_")
        .replace(/^_+|_+$/g, "");
}

/* ---------------------------------------------------------------------- */
/* Zoom-driven level of detail                                            */
/* ---------------------------------------------------------------------- */

function updateZoomVisibility() {
    if (!state.map) return;
    const zoom = state.map.getZoom();
    const map = state.map;
    const deptVisible = document.getElementById("layer-department").checked;
    const aisleVisible = document.getElementById("layer-aisle").checked;
    const rackVisible = document.getElementById("layer-rack").checked;

    setLayerGroupVisible(state.layers.deptLabels, map, deptVisible && zoom >= ZOOM.DEPT_LABELS);
    setLayerGroupVisible(state.layers.aisleLabels, map, aisleVisible && zoom >= ZOOM.AISLE_LABELS);
    setLayerGroupVisible(state.layers.rackLabels, map, rackVisible && zoom >= ZOOM.RACK_DETAIL);

    // Aisle geometry itself becomes visible a level before its labels do,
    // and rack geometry a level before rack labels/info.
    setLayerVisible(state.layers.departments, map, deptVisible);
    setLayerVisible(state.layers.departmentLines, map, deptVisible);
    setLayerVisible(state.layers.aisles, map, aisleVisible && zoom >= ZOOM.DEPT_LABELS_ALL);
    setLayerVisible(state.layers.aisleLines, map, aisleVisible && zoom >= ZOOM.DEPT_LABELS_ALL);
    setLayerVisible(state.layers.racks, map, rackVisible && zoom >= ZOOM.AISLE_LABELS);
    setLayerVisible(state.layers.rackLines, map, rackVisible && zoom >= ZOOM.AISLE_LABELS);

    setLayerGroupVisible(state.layers.markerGroup, map, deptVisible);
    for (const { layer, minZoom } of state.layers.markers) {
        setLayerVisible(layer, map, deptVisible && zoom >= minZoom);
    }
}

function setLayerGroupVisible(group, map, visible) {
    if (visible && !map.hasLayer(group)) map.addLayer(group);
    if (!visible && map.hasLayer(group)) map.removeLayer(group);
}

function setLayerVisible(layer, map, visible) {
    if (!layer) return;
    if (visible && !map.hasLayer(layer)) map.addLayer(layer);
    if (!visible && map.hasLayer(layer)) map.removeLayer(layer);
}

/* ---------------------------------------------------------------------- */
/* Loading store data                                                     */
/* ---------------------------------------------------------------------- */

async function fetchJSON(url, options) {
    const res = await fetch(url, options);
    if (!res.ok) {
        const body = await res.text().catch(() => "");
        throw new Error(`${res.status} ${res.statusText}: ${body}`);
    }
    return res.json();
}

function clearFeatureLayers() {
    state.layers.departments.clearLayers();
    state.layers.deptLabels.clearLayers();
    state.layers.aisles.clearLayers();
    state.layers.aisleLabels.clearLayers();
    state.layers.racks.clearLayers();
    state.layers.departmentLines.clearLayers();
    state.layers.aisleLines.clearLayers();
    state.layers.rackLines.clearLayers();
    state.layers.rackLabels.clearLayers();
    state.layers.markerGroup.clearLayers();
    state.layers.markers = [];
}

function renderGeoJsonFloorplanData(data) {
    state.layers.departments.clearLayers();
    state.layers.departmentLines.clearLayers();
    state.layers.aisles.clearLayers();
    state.layers.aisleLines.clearLayers();
    state.layers.racks.clearLayers();
    state.layers.rackLines.clearLayers();
    state.layers.deptLabels.clearLayers();
    state.layers.aisleLabels.clearLayers();
    state.layers.markerGroup.clearLayers();
    state.layers.markers = [];

    state.layers.departments.addData(data.departments);
    state.layers.departmentLines.addData(data.departmentLines);
    state.layers.aisles.addData(data.aisles);
    state.layers.aisleLines.addData(data.aisleLines);
    state.layers.racks.addData(data.racks);
    state.layers.rackLines.addData(data.rackLines);

    for (const feature of data.departmentPoints.features || []) {
        const point = buildGeoJsonPointLayer(feature);
        if (feature.properties?.poi_name || feature.properties?.icon_id) {
            state.layers.markers.push(point);
            state.layers.markerGroup.addLayer(point.layer);
        } else {
            point.layer._minZoom = ZOOM.DEPT_LABELS;
            state.layers.deptLabels.addLayer(point.layer);
        }
    }

    for (const feature of data.aislePoints.features || []) {
        const point = buildGeoJsonPointLayer(feature);
        point.layer._minZoom = ZOOM.AISLE_LABELS;
        state.layers.aisleLabels.addLayer(point.layer);
    }
}

/* ---------------------------------------------------------------------- */
/* Layer panel checkboxes                                                 */
/* ---------------------------------------------------------------------- */

function wireLayerControls() {
    document.getElementById("layer-satellite").addEventListener("change", (e) => {
        setLayerVisible(state.layers.osmTile, state.map, !e.target.checked);
        setLayerVisible(state.layers.satelliteTile, state.map, e.target.checked);
    });

    for (const id of ["layer-department", "layer-aisle", "layer-rack"]) {
        document.getElementById(id).addEventListener("change", updateZoomVisibility);
    }
}

/* ---------------------------------------------------------------------- */
/* Boot                                                                    */
/* ---------------------------------------------------------------------- */

document.addEventListener("DOMContentLoaded", async () => {
    initMap();
    wireLayerControls();
    wireCollapsiblePanels();
    setupMenardsControls();
    try {
        await loadStores();
    } catch (err) {
        console.error("Failed to load stores:", err);
    }

    addTargetMarker();
    await loadMenardsFloorPlanOverlay();
    const checkbox = document.getElementById("layer-menards");
    if (checkbox && state.menardsState) checkbox.checked = state.menardsState.visible !== false;
    setMenardsVisible(checkbox ? checkbox.checked : true);
    if (state.menardsState) {
        const status = document.getElementById("menards-status");
        if (status) status.textContent = `Menards floor plan enabled · ${state.menardsState.width_meters}m x ${state.menardsState.height_meters}m`;
    }
});
