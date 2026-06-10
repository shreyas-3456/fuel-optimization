const API_URL = 'http://127.0.0.1:8000/api/route-fuel/';

const startInput = document.querySelector('#start-input');
const finishInput = document.querySelector('#finish-input');
const form = document.querySelector('#route-form');
const statusEl = document.querySelector('#status');
const distanceEl = document.querySelector('#distance');
const costEl = document.querySelector('#cost');
const stopsEl = document.querySelector('#stops');
const fuelStopList = document.querySelector('#fuel-stop-list');
const resultJson = document.querySelector('#result-json');
const submitButton = form.querySelector('button');
const debugToggle = document.querySelector('#debug-toggle');

const state = {
    startDragged: false,
    finishDragged: false,
    debug: false,
    start: { lng: -87.6298, lat: 41.8781, label: 'Chicago, IL' },
    finish: { lng: -96.7970, lat: 32.7767, label: 'Dallas, TX' },
    fuelStopMarkers: [],
    nearbyStationMarkers: [],
    fuelStops: [],
    nearbyStations: [],
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
    renderFuelStops(state.fuelStops, state.nearbyStations);
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

    if (!startLocation || !finishLocation) {
        setStatus('Source and destination are required.', true);
        return;
    }

    setLoading(true);
    setStatus('Planning route...');

    try {
        const response = await fetch(API_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                start_location: startLocation,
                finish_location: finishLocation,
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

    // Filter out stops at or very near the start (route_mile <= 10)
    const validStops = payload.fuel_stops.filter((stop) => stop.route_mile > 10);

    stopsEl.textContent = String(validStops.length);
    const nearbyStations = (payload.nearby_stations || []).filter((station) => station.route_mile > 10);
    state.fuelStops = validStops;
    state.nearbyStations = nearbyStations;

    renderFuelStops(validStops, nearbyStations);
    resultJson.textContent = JSON.stringify({
        start: payload.start,
        finish: payload.finish,
        total_fuel_cost: payload.total_fuel_cost,
        fuel_stops: validStops,
        nearby_stations: nearbyStations,
    }, null, 2);
    setStatus(routeStatusMessage(payload, validStops, nearbyStations));
}

function renderFuelStops(fuelStops, nearbyStations = []) {
    clearFuelStopMarkers();
    fuelStopList.innerHTML = '';

    const visibleNearbyStations = state.debug ? nearbyStations : [];

    if (!fuelStops.length && !visibleNearbyStations.length) {
        fuelStopList.innerHTML = '<p class="empty-stops">No refueling stations returned.</p>';
        return;
    }

    fuelStops.forEach((stop) => {
        const marker = createFuelStopMarker(stop, 'selected')
            .setLngLat([stop.map_marker.lng, stop.map_marker.lat])
            .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(fuelStopPopupHtml(stop)))
            .addTo(map);

        state.fuelStopMarkers.push(marker);
        fuelStopList.appendChild(createFuelStopListItem(stop, marker));
    });

    visibleNearbyStations.forEach((stationOption) => {
        const marker = createFuelStopMarker(stationOption, 'nearby')
            .setLngLat([stationOption.map_marker.lng, stationOption.map_marker.lat])
            .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(nearbyStationPopupHtml(stationOption)))
            .addTo(map);

        state.nearbyStationMarkers.push(marker);
        fuelStopList.appendChild(createNearbyStationListItem(stationOption, marker));
    });
}

function routeStatusMessage(payload, fuelStops, nearbyStations) {
    const debugDetails = state.debug
        ? ` Debug shows ${nearbyStations.length} nearby alternatives.`
        : ' Toggle Debug to show nearby alternatives.';
    return `Route ready: ${payload.start.name} to ${payload.finish.name}. Showing ${fuelStops.length} selected stops.${debugDetails}`;
}

function createFuelStopMarker(stop, variant) {
    const el = document.createElement('div');
    el.className = `fuel-marker fuel-marker--${variant}`;
    el.textContent = variant === 'selected' ? stop.sequence : '+';
    el.title = variant === 'selected'
        ? `Fuel stop ${stop.sequence}: ${stop.station.name}`
        : `Nearby station not chosen: ${stop.station.name}`;
    return new maplibregl.Marker({ element: el });
}

function createFuelStopListItem(stop, marker) {
    const station = stop.station;
    const routePlace = formatRouteLocation(stop.route_location);
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'fuel-stop';
    item.innerHTML = `
        <strong>${escapeHtml(stop.sequence + '. ' + station.name)}</strong>
        <span>Refuel near ${escapeHtml(routePlace)}</span>
        <span>${escapeHtml(station.city)}, ${escapeHtml(station.state)} - $${station.price_per_gallon.toFixed(3)}/gal</span>
        <span>${escapeHtml(station.address)} · mile ${stop.route_mile.toLocaleString()} · ${stop.gallons} gal · $${stop.fuel_cost.toFixed(2)}</span>
    `;
    item.addEventListener('click', () => {
        map.flyTo({
            center: [stop.map_marker.lng, stop.map_marker.lat],
            zoom: Math.max(map.getZoom(), 8),
            duration: 700,
        });
        marker.togglePopup();
    });
    return item;
}

function createNearbyStationListItem(stationOption, marker) {
    const station = stationOption.station;
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'fuel-stop fuel-stop--nearby';
    item.innerHTML = `
        <strong>${escapeHtml(station.name)}</strong>
        <span>Nearby option near mile ${stationOption.route_mile.toLocaleString()}</span>
        <span>${escapeHtml(station.city)}, ${escapeHtml(station.state)} - $${station.price_per_gallon.toFixed(3)}/gal</span>
        <span>${escapeHtml(station.address)}</span>
        <span class="reason">${escapeHtml(stationOption.reason)}</span>
    `;
    item.addEventListener('click', () => {
        map.flyTo({
            center: [stationOption.map_marker.lng, stationOption.map_marker.lat],
            zoom: Math.max(map.getZoom(), 9),
            duration: 700,
        });
        marker.togglePopup();
    });
    return item;
}

function fuelStopPopupHtml(stop) {
    const station = stop.station;
    const routePlace = formatRouteLocation(stop.route_location);
    return `
        <strong>Refuel near ${escapeHtml(routePlace)}</strong><br>
        <strong>${escapeHtml(station.name)}</strong><br>
        ${escapeHtml(station.address)}<br>
        ${escapeHtml(station.city)}, ${escapeHtml(station.state)}<br>
        $${station.price_per_gallon.toFixed(3)}/gal<br>
        Route mile ${stop.route_mile.toLocaleString()}
    `;
}

function nearbyStationPopupHtml(stationOption) {
    const station = stationOption.station;
    return `
        <strong>Nearby station not chosen</strong><br>
        <strong>${escapeHtml(station.name)}</strong><br>
        ${escapeHtml(station.address)}<br>
        ${escapeHtml(station.city)}, ${escapeHtml(station.state)}<br>
        $${station.price_per_gallon.toFixed(3)}/gal<br>
        Route mile ${stationOption.route_mile.toLocaleString()}<br>
        <span>${escapeHtml(stationOption.reason)}</span>
    `;
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

function roundCoord(value) {
    return Math.round(value * 1000000) / 1000000;
}

function emptyFeatureCollection() {
    return {
        type: 'FeatureCollection',
        features: [],
    };
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
