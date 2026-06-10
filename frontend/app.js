const API_URL = 'http://127.0.0.1:8000/api/route-fuel/';

const startInput = document.querySelector('#start-input');
const finishInput = document.querySelector('#finish-input');
const startFuelInput = document.querySelector('#start-fuel-input');
const form = document.querySelector('#route-form');
const statusEl = document.querySelector('#status');
const distanceEl = document.querySelector('#distance');
const costEl = document.querySelector('#cost');
const stopsEl = document.querySelector('#stops');
const mapWrap = document.querySelector('.map-wrap');
const fuelStopList = document.querySelector('#fuel-stop-list');
const stationDrawer = document.querySelector('#station-drawer');
const stationDrawerCount = document.querySelector('#station-drawer-count');
const stationDrawerToggle = document.querySelector('#station-drawer-toggle');
const stationDrawerToggleCount = document.querySelector('#station-drawer-toggle-count');
const resultJson = document.querySelector('#result-json');
const debugJsonDrawer = document.querySelector('.debug-drawer');
const debugJsonClose = document.querySelector('#debug-json-close');
const submitButton = form.querySelector('button');
const debugToggle = document.querySelector('#debug-toggle');
const stationBreakdownModal = document.querySelector('#station-breakdown-modal');
const stationBreakdownContent = document.querySelector('#station-breakdown-content');
const stationBreakdownClose = document.querySelector('#station-breakdown-close');

const state = {
    startDragged: false,
    finishDragged: false,
    debug: false,
    start: { lng: -87.6298, lat: 41.8781, label: 'Chicago, IL' },
    finish: { lng: -96.7970, lat: 32.7767, label: 'Dallas, TX' },
    startFuelGallons: 5,
    fuelStopMarkers: [],
    nearbyStationMarkers: [],
    fuelStops: [],
    nearbyStations: [],
    debugGasStations: [],
    detourSummary: null,
    totalFuelCost: 0,
    routePayload: null,
    selectedStationKey: null,
    stationDrawerOpen: false,
};

const map = new maplibregl.Map({
    container: 'map',
    style: {
        version: 8,
        sources: {
            osm: {
                type: 'raster',
                tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
                tileSize: 256,
                attribution: '&copy; OpenStreetMap contributors',
            },
        },
        layers: [{
            id: 'osm',
            type: 'raster',
            source: 'osm',
        }],
    },
    center: [-92.1, 37.5],
    zoom: 4,
});

map.addControl(new maplibregl.NavigationControl(), 'top-right');

const startMarker = new maplibregl.Marker({ color: '#24745d', draggable: true })
    .setLngLat([state.start.lng, state.start.lat])
    .addTo(map);

const finishMarker = new maplibregl.Marker({ color: '#c2410c', draggable: true })
    .setLngLat([state.finish.lng, state.finish.lat])
    .addTo(map);

startMarker.on('dragend', () => {
    const lngLat = startMarker.getLngLat();
    state.start = {
        lng: roundCoord(lngLat.lng),
        lat: roundCoord(lngLat.lat),
        label: startInput.value.trim() || 'Dragged source',
    };
    state.startDragged = true;
    startInput.value = `${state.start.lat}, ${state.start.lng}`;
    planRoute();
});

finishMarker.on('dragend', () => {
    const lngLat = finishMarker.getLngLat();
    state.finish = {
        lng: roundCoord(lngLat.lng),
        lat: roundCoord(lngLat.lat),
        label: finishInput.value.trim() || 'Dragged destination',
    };
    state.finishDragged = true;
    finishInput.value = `${state.finish.lat}, ${state.finish.lng}`;
    planRoute();
});

startInput.addEventListener('input', () => {
    state.startDragged = false;
});

finishInput.addEventListener('input', () => {
    state.finishDragged = false;
});

form.addEventListener('submit', (event) => {
    event.preventDefault();
    planRoute();
});

debugToggle.addEventListener('click', () => {
    state.debug = !state.debug;
    debugToggle.setAttribute('aria-pressed', String(state.debug));
    debugToggle.classList.toggle('is-active', state.debug);
    renderFuelStops(state.fuelStops, state.debugGasStations);
    if (state.routePayload) {
        setStatus(routeStatusMessage(state.routePayload, state.fuelStops, state.debugGasStations));
    }
});

stationDrawerToggle.addEventListener('click', () => {
    setStationDrawerOpen(!state.stationDrawerOpen);
});

stationBreakdownClose.addEventListener('click', () => {
    stationBreakdownModal.close();
});

stationBreakdownModal.addEventListener('click', (event) => {
    if (event.target === stationBreakdownModal) {
        stationBreakdownModal.close();
    }
});

resultJson.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {
        event.preventDefault();
        selectElementText(resultJson);
    }
});

debugJsonClose.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopPropagation();
    debugJsonDrawer.open = false;
});

map.on('load', () => {
    map.addSource('route', {
        type: 'geojson',
        data: emptyFeatureCollection(),
    });
    map.addLayer({
        id: 'route-line',
        type: 'line',
        source: 'route',
        paint: {
            'line-color': '#24745d',
            'line-width': 5,
            'line-opacity': 0.86,
        },
    });
    planRoute();
});

async function planRoute() {
    const startLocation = getLocationPayload(startInput, state.startDragged, state.start);
    const finishLocation = getLocationPayload(finishInput, state.finishDragged, state.finish);
    const startFuelGallons = getStartFuelGallons();

    if (!startLocation || !finishLocation) {
        setStatus('Source and destination are required.', true);
        return;
    }
    if (startFuelGallons === null) {
        setStatus('Starting fuel must be between 0 and 50 gallons.', true);
        return;
    }

    setLoading(true);
    setStatus('Planning route...');
    state.startFuelGallons = startFuelGallons;

    try {
        const response = await fetch(API_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                start_location: startLocation,
                finish_location: finishLocation,
                start_mode: 'partial_tank',
                start_fuel: startFuelGallons,
                start_fuel_gallons: startFuelGallons,
            }),
        });
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.error || 'Route request failed.');
        }
        renderRoute(payload);
    } catch (error) {
        setStatus(error.message, true);
    } finally {
        setLoading(false);
    }
}

function getLocationPayload(input, useCoordinates, point) {
    const value = input.value.trim();
    if (!value) {
        return null;
    }
    if (!useCoordinates) {
        return value;
    }
    return {
        lat: point.lat,
        lng: point.lng,
        label: value,
    };
}

function getStartFuelGallons() {
    const value = Number(startFuelInput.value);
    if (!Number.isFinite(value) || value < 0 || value > 50) {
        return null;
    }
    return Math.round(value * 10) / 10;
}

function renderRoute(payload) {
    const routeFeature = {
        type: 'Feature',
        geometry: payload.route.geojson,
        properties: {},
    };
    map.getSource('route').setData({
        type: 'FeatureCollection',
        features: [routeFeature],
    });

    startMarker.setLngLat([payload.start.lng, payload.start.lat]);
    finishMarker.setLngLat([payload.finish.lng, payload.finish.lat]);
    state.start = { lng: payload.start.lng, lat: payload.start.lat, label: payload.start.input };
    state.finish = { lng: payload.finish.lng, lat: payload.finish.lat, label: payload.finish.input };

    const bounds = new maplibregl.LngLatBounds();
    payload.route.geojson.coordinates.forEach((coord) => bounds.extend(coord));
    map.fitBounds(bounds, { padding: 70, maxZoom: 9, duration: 700 });

    distanceEl.textContent = `${payload.route.distance_miles.toLocaleString()} mi`;
    costEl.textContent = `$${payload.total_fuel_cost.toFixed(2)}`;

    const validStops = payload.fuel_stops || [];

    stopsEl.textContent = String(validStops.length);
    const debugGasStations = collectDebugGasStations(payload);
    state.fuelStops = validStops;
    state.nearbyStations = debugGasStations;
    state.debugGasStations = debugGasStations;
    state.detourSummary = payload.detour_summary || null;
    state.totalFuelCost = payload.total_fuel_cost || 0;
    state.routePayload = payload;

    renderFuelStops(validStops, debugGasStations);
    resultJson.textContent = JSON.stringify({
        start: payload.start,
        finish: payload.finish,
        start_config: payload.start_config || {
            mode: 'partial_tank',
            start_fuel: state.startFuelGallons,
        },
        vehicle: payload.vehicle,
        total_fuel_cost: payload.total_fuel_cost,
        detour_summary: payload.detour_summary,
        fuel_stops: validStops,
        debug_gas_stations: debugGasStations,
    }, null, 2);
    setStatus(routeStatusMessage(payload, validStops, debugGasStations));
}

function collectDebugGasStations(payload) {
    return payload.debug_gas_stations
        || payload.debug_stations
        || payload.candidate_stations
        || payload.nearby_stations
        || [];
}

function renderFuelStops(fuelStops, debugGasStations = []) {
    clearFuelStopMarkers();
    state.selectedStationKey = null;
    fuelStopList.innerHTML = '';

    const visibleDebugStations = state.debug ? debugGasStations : [];
    const visibleStationCount = fuelStops.length + visibleDebugStations.length;
    stationDrawerCount.textContent = String(visibleStationCount);
    stationDrawerToggleCount.textContent = String(visibleStationCount);

    if (!fuelStops.length && !visibleDebugStations.length) {
        fuelStopList.innerHTML = '<p class="empty-stops">No refueling stations returned.</p>';
        return;
    }

    if (state.debug) {
        fuelStopList.appendChild(createDebugSummaryItem(fuelStops, debugGasStations));
    }

    fuelStops.forEach((stop) => {
        const stationKey = selectedStopKey(stop);
        const marker = createFuelStopMarker(stop, 'selected')
            .setLngLat([stop.map_marker.lng, stop.map_marker.lat])
            .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(fuelStopPopupHtml(stop)))
            .addTo(map);
        const item = createFuelStopListItem(stop, marker, stationKey);

        marker.getElement().dataset.stationKey = stationKey;
        marker.getElement().addEventListener('click', () => {
            selectFuelStation(stationKey, marker, [stop.map_marker.lng, stop.map_marker.lat], item, 8, { scrollCard: true });
        });
        state.fuelStopMarkers.push(marker);
        fuelStopList.appendChild(item);
    });

    visibleDebugStations.forEach((stationOption) => {
        const markerLngLat = getMarkerLngLat(stationOption);
        if (!markerLngLat) {
            return;
        }
        const stationKey = debugStationKey(stationOption);
        const marker = createFuelStopMarker(stationOption, stationOption.is_selected ? 'debug-selected' : 'nearby')
            .setLngLat(markerLngLat)
            .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(debugGasStationPopupHtml(stationOption)))
            .addTo(map);
        const item = createDebugGasStationListItem(stationOption, marker, stationKey);

        marker.getElement().dataset.stationKey = stationKey;
        marker.getElement().addEventListener('click', () => {
            selectFuelStation(stationKey, marker, markerLngLat, item, 9, { scrollCard: true });
        });
        state.nearbyStationMarkers.push(marker);
        fuelStopList.appendChild(item);
    });
}

function routeStatusMessage(payload, fuelStops, debugGasStations) {
    const startFuel = payload.start_config?.start_fuel_gallons
        ?? payload.start_config?.start_fuel
        ?? state.startFuelGallons;
    const debugDetails = state.debug
        ? ` Debug maps ${debugGasStations.length} gas stations with computed cost details.`
        : ' Toggle Debug to map all gas station candidates and computed costs.';
    return `Route ready: ${payload.start.name} to ${payload.finish.name}. Starting with ${formatGallons(startFuel)} gal and showing ${fuelStops.length} selected stops.${debugDetails}`;
}

function createFuelStopMarker(stop, variant) {
    const el = document.createElement('div');
    el.className = `fuel-marker fuel-marker--${variant}`;
    el.textContent = markerText(stop, variant);
    el.title = markerTitle(stop, variant);
    return new maplibregl.Marker({ element: el });
}

function markerText(stop, variant) {
    if (variant === 'selected') {
        return stop.sequence;
    }
    if (variant === 'debug-selected') {
        return '*';
    }
    return '$';
}

function markerTitle(stop, variant) {
    if (variant === 'selected') {
        return `Fuel stop ${stop.sequence}: ${stop.station.name}`;
    }
    const total = stop.total_fuel_cost_if_chosen !== undefined
        ? ` - total if chosen ${formatMoney(stop.total_fuel_cost_if_chosen)}`
        : '';
    const prefix = variant === 'debug-selected' ? 'Selected gas station' : 'Gas station candidate';
    return `${prefix}: ${stop.station.name}${total}`;
}

function createFuelStopListItem(stop, marker, stationKey) {
    const station = stop.station;
    const routePlace = formatRouteLocation(stop.route_location);
    const detourHtml = state.debug ? detourStopHtml(stop) : '';
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'fuel-stop';
    item.dataset.stationKey = stationKey;
    item.innerHTML = `
        <strong>${escapeHtml(stop.sequence + '. ' + station.name)}</strong>
        <span>Refuel near ${escapeHtml(routePlace)}</span>
        <span>${escapeHtml(station.city)}, ${escapeHtml(station.state)} - $${station.price_per_gallon.toFixed(3)}/gal</span>
        <span>${escapeHtml(station.address)} · mile ${stop.route_mile.toLocaleString()} · ${stop.gallons} gal · $${stop.fuel_cost.toFixed(2)}</span>
        ${detourHtml}
    `;
    item.addEventListener('click', () => {
        selectFuelStation(stationKey, marker, [stop.map_marker.lng, stop.map_marker.lat], item, 8);
    });
    return item;
}

function createDebugGasStationListItem(stationOption, marker, stationKey) {
    const station = stationOption.station;
    const computedHtml = debugGasStationSummaryHtml(stationOption);
    const item = document.createElement('div');
    item.className = `fuel-stop fuel-stop--nearby${stationOption.is_selected ? ' fuel-stop--debug-selected' : ''}`;
    item.dataset.stationKey = stationKey;
    item.innerHTML = `
        <button class="fuel-stop__main" type="button">
            <span class="fuel-stop__kicker">${escapeHtml(stationOption.is_selected ? 'Selected station' : 'Candidate station')} · route mile ${formatNumber(stationOption.route_mile)}</span>
            <strong>${escapeHtml(station.name)}</strong>
            <span>${escapeHtml(station.address)}</span>
            <span>${escapeHtml(station.city)}, ${escapeHtml(station.state)} · $${station.price_per_gallon.toFixed(3)}/gal</span>
        </button>
        ${computedHtml}
        ${stationOption.reason ? `<span class="reason">${escapeHtml(stationOption.reason)}</span>` : ''}
        <button class="station-breakdown-button" type="button">Complete breakdown</button>
    `;
    item.querySelector('.fuel-stop__main').addEventListener('click', () => {
        const markerLngLat = getMarkerLngLat(stationOption);
        selectFuelStation(stationKey, marker, markerLngLat, item, 9);
    });
    item.querySelector('.station-breakdown-button').addEventListener('click', () => {
        openStationBreakdown(stationOption);
    });
    return item;
}

function selectFuelStation(stationKey, marker, center, item, zoom, options = {}) {
    state.selectedStationKey = stationKey;
    setStationDrawerOpen(true);
    setActiveStation(stationKey);

    if (center) {
        map.flyTo({
            center,
            zoom: Math.max(map.getZoom(), zoom),
            duration: 700,
        });
    }

    openMarkerPopup(marker);

    if (options.scrollCard && item) {
        item.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
}

function setActiveStation(stationKey) {
    fuelStopList.querySelectorAll('.fuel-stop').forEach((item) => {
        item.classList.toggle('is-active', item.dataset.stationKey === stationKey);
    });
    [...state.fuelStopMarkers, ...state.nearbyStationMarkers].forEach((marker) => {
        marker.getElement().classList.toggle('is-active', marker.getElement().dataset.stationKey === stationKey);
    });
}

function openMarkerPopup(marker) {
    const popup = marker.getPopup();
    closeStationPopups(marker);
    if (!popup || popup.isOpen()) {
        return;
    }
    window.setTimeout(() => {
        if (!popup.isOpen()) {
            marker.togglePopup();
        }
    }, 0);
}

function closeStationPopups(exceptMarker = null) {
    [...state.fuelStopMarkers, ...state.nearbyStationMarkers].forEach((marker) => {
        if (marker === exceptMarker) {
            return;
        }
        const popup = marker.getPopup();
        if (popup?.isOpen()) {
            popup.remove();
        }
    });
}

function setStationDrawerOpen(isOpen) {
    state.stationDrawerOpen = isOpen;
    mapWrap.classList.toggle('station-drawer-open', isOpen);
    stationDrawer.classList.toggle('is-open', isOpen);
    stationDrawerToggle.setAttribute('aria-expanded', String(isOpen));
    stationDrawerToggle.classList.toggle('is-active', isOpen);
    window.setTimeout(() => map.resize(), 180);
}

function selectedStopKey(stop) {
    return `selected-${stop.sequence}-${stop.station.name}-${stop.route_mile}`;
}

function debugStationKey(stationOption) {
    return `debug-${stationOption.station.name}-${stationOption.route_mile}-${stationOption.station.address}`;
}

function fuelStopPopupHtml(stop) {
    const station = stop.station;
    const routePlace = formatRouteLocation(stop.route_location);
    const detourHtml = stop.detour ? `<br>${detourPopupHtml(stop)}` : '';
    return `
        <strong>Refuel near ${escapeHtml(routePlace)}</strong><br>
        <strong>${escapeHtml(station.name)}</strong><br>
        ${escapeHtml(station.address)}<br>
        ${escapeHtml(station.city)}, ${escapeHtml(station.state)}<br>
        $${station.price_per_gallon.toFixed(3)}/gal<br>
        Route mile ${stop.route_mile.toLocaleString()}
        ${detourHtml}
    `;
}

function debugGasStationPopupHtml(stationOption) {
    const station = stationOption.station;
    return `
        <div class="station-popup">
            <strong>${escapeHtml(stationOption.is_selected ? 'Selected gas station' : 'Gas station candidate')}</strong>
            <strong>${escapeHtml(station.name)}</strong>
            <span>${escapeHtml(station.address)}</span>
            <span>${escapeHtml(station.city)}, ${escapeHtml(station.state)}</span>
            <span><bold>$${station.price_per_gallon.toFixed(3)}/gal · route mile ${formatNumber(stationOption.route_mile)}</bold></span>
            ${debugGasStationPopupComputedHtml(stationOption)}
            ${stationOption.reason ? `<span class="station-popup__note">${escapeHtml(stationOption.reason)}</span>` : ''}
        </div>
    `;
}

function createDebugSummaryItem(fuelStops, debugGasStations) {
    const details = summarizeDetours(fuelStops);
    const selectedStations = debugGasStations.filter((station) => station.is_selected).length;
    const detourCandidates = debugGasStations.filter((station) => station.is_detour).length;
    const item = document.createElement('div');
    item.className = 'debug-summary';
    item.innerHTML = `
        <strong>Debug details</strong>
        <span>Starting fuel: ${formatGallons(state.startFuelGallons)} gal</span>
        <span>Gas stations mapped: ${debugGasStations.length}</span>
        <span>Selected gas stations in debug set: ${selectedStations}</span>
        <span>Detour candidates in debug set: ${detourCandidates}</span>
        <span>Detours selected: ${details.count}</span>
        <span>Total detour road miles: ${formatMiles(details.roadMiles)}</span>
        <span>Detour fuel cost added to total: ${formatMoney(details.fuelCost)}</span>
        <span>Fuel purchase cost before detour fuel: ${formatMoney(details.baseFuelCost)}</span>
        <span>Total fuel cost: ${formatMoney(state.totalFuelCost)}</span>
        ${state.detourSummary ? detourModelHtml(state.detourSummary) : ''}
    `;
    return item;
}

function summarizeDetours(fuelStops) {
    const detourStops = fuelStops.filter((stop) => stop.detour);
    const fuelCost = detourStops.reduce((total, stop) => total + numberOrZero(stop.detour.detour_fuel_cost), 0);
    const roadMiles = detourStops.reduce((total, stop) => total + numberOrZero(stop.detour.round_trip_road_miles), 0);
    return {
        count: detourStops.length,
        fuelCost,
        roadMiles,
        baseFuelCost: Math.max(0, numberOrZero(state.totalFuelCost) - fuelCost),
    };
}

function detourStopHtml(stop) {
    if (!stop.detour) {
        return '<span class="detour-detail">Detour: no extra off-route cost for this selected stop.</span>';
    }
    const detourCost = numberOrZero(stop.detour.detour_fuel_cost);
    const totalShare = state.totalFuelCost ? ` · ${((detourCost / state.totalFuelCost) * 100).toFixed(1)}% of total` : '';
    return `
        <span class="detour-detail">Detour round trip: ${formatMiles(stop.detour.round_trip_road_miles)} (${formatMiles(stop.detour.one_way_road_miles)} one way)</span>
        <span class="detour-detail">Detour fuel adds ${formatMoney(detourCost)} to this stop${totalShare}</span>
    `;
}

function debugGasStationSummaryHtml(stationOption) {
    const rows = debugGasStationDetailRows(stationOption).slice(0, 4);
    if (!rows.length) {
        return '';
    }
    return `
        <dl class="station-cost-grid">
            ${rows.map((row) => `
                <div>
                    <dt>${escapeHtml(row.label)}</dt>
                    <dd>${escapeHtml(row.value)}</dd>
                </div>
            `).join('')}
        </dl>
    `;
}

function debugGasStationDetailRows(stationOption) {
    const rows = [];
    if (stationOption.gallons_if_chosen !== undefined) {
        rows.push({ label: 'Gallons if chosen', value: `${formatGallons(stationOption.gallons_if_chosen)} gal` });
    }
    if (stationOption.fuel_cost_if_chosen !== undefined) {
        rows.push({ label: 'Stop cost', value: formatMoney(stationOption.fuel_cost_if_chosen) });
    }
    if (stationOption.total_fuel_cost_if_chosen !== undefined) {
        rows.push({
            label: 'Trip total',
            value: `${formatMoney(stationOption.total_fuel_cost_if_chosen)} (${formatDeltaMoney(stationOption.delta_total_fuel_cost)})`,
        });
    }
    if (stationOption.compared_stop_sequence !== undefined && stationOption.compared_stop_sequence !== null) {
        rows.push({
            label: `Compared with stop ${stationOption.compared_stop_sequence}`,
            value: formatMoney(stationOption.compared_stop_cost),
        });
    }
    const detour = nearbyDetourText(stationOption);
    if (detour) {
        rows.push({ label: 'Detour impact', value: detour });
    }
    return rows;
}

function debugGasStationDetourRows(stationOption) {
    const detour = stationOption.detour || {};
    const rows = [];
    const roundTripMiles = stationOption.detour_miles
        ?? stationOption.round_trip_road_miles
        ?? detour.round_trip_road_miles;
    const oneWayMiles = stationOption.one_way_road_miles ?? detour.one_way_road_miles;
    const detourFuelCost = stationOption.detour_fuel_cost ?? detour.detour_fuel_cost;

    if (roundTripMiles !== undefined) {
        rows.push({ label: 'Detour round trip', value: formatMiles(roundTripMiles) });
    }
    if (oneWayMiles !== undefined) {
        rows.push({ label: 'Detour one way', value: formatMiles(oneWayMiles) });
    }
    if (detourFuelCost !== undefined) {
        rows.push({ label: 'Detour fuel cost', value: formatMoney(detourFuelCost) });
    }
    return rows;
}

function debugGasStationPopupComputedHtml(stationOption) {
    const rows = debugGasStationDetailRows(stationOption);
    if (!rows.length) {
        return '';
    }
    return `
        <dl class="station-popup__costs">
            ${rows.map((row) => `
                <div>
                    <dt>${escapeHtml(row.label)}</dt>
                    <dd>${escapeHtml(row.value)}</dd>
                </div>
            `).join('')}
        </dl>
    `;
}

function openStationBreakdown(stationOption) {
    const station = stationOption.station;
    const rows = [
        { label: 'Station', value: station.name },
        { label: 'Address', value: `${station.address}, ${station.city}, ${station.state}` },
        { label: 'Fuel price', value: `$${station.price_per_gallon.toFixed(3)}/gal` },
        { label: 'Route mile', value: formatNumber(stationOption.route_mile) },
        ...debugGasStationDetailRows(stationOption),
        ...debugGasStationDetourRows(stationOption),
    ];

    stationBreakdownContent.innerHTML = `
        <section class="station-modal__summary">
            <strong>${escapeHtml(stationOption.is_selected ? 'Selected gas station' : 'Gas station candidate')}</strong>
            <span>${escapeHtml(station.name)}</span>
        </section>
        <dl class="station-breakdown-list">
            ${rows.map((row) => `
                <div>
                    <dt>${escapeHtml(row.label)}</dt>
                    <dd>${escapeHtml(row.value)}</dd>
                </div>
            `).join('')}
        </dl>
        ${stationOption.reason ? `<p class="station-modal__note">${escapeHtml(stationOption.reason)}</p>` : ''}
    `;
    stationBreakdownModal.showModal();
}

function nearbyDetourText(stationOption) {
    const parts = [];
    const detour = stationOption.detour || {};
    const detourMiles = stationOption.detour_miles
        ?? stationOption.round_trip_road_miles
        ?? detour.round_trip_road_miles;
    const detourFuelCost = stationOption.detour_fuel_cost ?? detour.detour_fuel_cost;
    if (detourMiles !== undefined) {
        parts.push(`detour ${formatMiles(detourMiles)}`);
    }
    if (detourFuelCost !== undefined) {
        parts.push(`adds ${formatMoney(detourFuelCost)}`);
    }
    return parts.length ? parts.join(' - ') : '';
}

function detourPopupHtml(stop) {
    return [
        `Detour round trip ${formatMiles(stop.detour.round_trip_road_miles)}`,
        `Detour fuel ${formatMoney(stop.detour.detour_fuel_cost)}`,
    ].map(escapeHtml).join('<br>');
}

function getMarkerLngLat(item) {
    const marker = item.map_marker || item.station_marker || item.marker;
    if (!marker || marker.lng === undefined || marker.lat === undefined) {
        return null;
    }
    return [marker.lng, marker.lat];
}

function detourModelHtml(summary) {
    const lines = [];
    if (summary.effective_road_factor !== undefined) {
        lines.push(`Effective road factor: ${summary.effective_road_factor}`);
    }
    if (summary.road_factor_expected !== undefined && summary.road_factor_conservative !== undefined) {
        lines.push(`Road factor range: ${summary.road_factor_expected} expected / ${summary.road_factor_conservative} conservative`);
    }
    return lines.map((line) => `<span>${escapeHtml(line)}</span>`).join('');
}

function formatRouteLocation(routeLocation) {
    if (!routeLocation) {
        return 'route point';
    }
    return [
        routeLocation.city,
        routeLocation.state_code || routeLocation.state,
    ].filter(Boolean).join(', ') || 'route point';
}

function clearFuelStopMarkers() {
    state.fuelStopMarkers.forEach((marker) => marker.remove());
    state.fuelStopMarkers = [];
    state.nearbyStationMarkers.forEach((marker) => marker.remove());
    state.nearbyStationMarkers = [];
}

function setStatus(message, isError = false) {
    statusEl.textContent = message;
    statusEl.classList.toggle('error', isError);
}

function setLoading(isLoading) {
    submitButton.disabled = isLoading;
}

function selectElementText(element) {
    const range = document.createRange();
    range.selectNodeContents(element);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
}

function roundCoord(value) {
    return Math.round(value * 1000000) / 1000000;
}

function emptyFeatureCollection() {
    return {
        type: 'FeatureCollection',
        features: [],
    };
}

function numberOrZero(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
}

function formatMoney(value) {
    return `$${numberOrZero(value).toFixed(2)}`;
}

function formatMiles(value) {
    return `${numberOrZero(value).toLocaleString(undefined, {
        maximumFractionDigits: 2,
    })} mi`;
}

function formatGallons(value) {
    return numberOrZero(value).toLocaleString(undefined, {
        maximumFractionDigits: 1,
    });
}

function formatNumber(value) {
    return numberOrZero(value).toLocaleString(undefined, {
        maximumFractionDigits: 2,
    });
}

function formatDeltaMoney(value) {
    const amount = numberOrZero(value);
    if (amount === 0) {
        return 'no change';
    }
    return `${amount > 0 ? '+' : '-'}${formatMoney(Math.abs(amount))}`;
}

function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
    })[char]);
}
