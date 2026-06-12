# Fuel Route API

Django API that plans a USA road trip, returns a map-ready route, chooses fuel stops for a vehicle with a 500-mile range, and estimates trip fuel cost at 10 MPG.

Geocoding uses OpenStreetMap Nominatim and routing uses GraphHopper. The API makes exactly one GraphHopper call per request — the route between the two geocoded coordinates.

## Requirements

- Python 3.12 or newer. Pinned dependencies are tested with Python 3.12.
- Docker Desktop or Docker Engine with Docker Compose v2.
- System GDAL 3.8.x headers/tools for the `GDAL==3.8.4` Python package.
- A GraphHopper API key for live route planning.

On Ubuntu/Debian:

```bash
sudo apt update
sudo apt install python3.12 python3.12-venv gdal-bin libgdal-dev docker.io docker-compose-plugin
```

On macOS with Homebrew:

```bash
brew install python@3.12 gdal docker
```

## Setup
```bash
docker compose up -d
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py import_fuel_stations "fuel-prices-for-be-assessment (1).csv"
python manage.py runserver 0.0.0.0:8000
```

In a second terminal:

```bash
cd frontend
python -m http.server 3000 --bind 127.0.0.1
```

## Endpoint

`POST /api/route-fuel/`

Request:

```json
{
  "start_location": "Chicago, IL",
  "finish_location": "Dallas, TX"
}
```

Coordinates can be supplied for either endpoint to skip a Nominatim geocoding call:

```json
{
  "start_location": {"lat": 41.8781, "lng": -87.6298, "label": "Chicago, IL"},
  "finish_location": {"lat": 32.7767, "lng": -96.7970, "label": "Dallas, TX"}
}
```

Response includes:

- `route.geojson` — GeoJSON `LineString` using `[lng, lat]` coordinates for OpenStreetMap clients.
- `fuel_stops` — ordered fuel stops with route mile, gallons, estimated cost, selected station, and marker coordinates.
- `total_fuel_cost` — total dollars spent on fuel for the trip.
- `external_api_calls` — actual external call count, including `graphhopper_total: 1`. Two fresh text locations require two Nominatim geocode calls; cached locations and coordinate inputs reduce that count.

Example:

```bash
curl -X POST http://127.0.0.1:8000/api/route-fuel/ \
  -H "Content-Type: application/json" \
  -d '{"start_location":"Chicago, IL","finish_location":"Dallas, TX"}'
```

## Fuel Data

The service reads `fuel-prices-for-be-assessment (1).csv` at import time and stores the data in PostGIS. The CSV does not include station latitude/longitude, so selected station records are optimised by available price data and route-state hints, while marker coordinates are placed on the GraphHopper route at the required refuel mile. With latitude/longitude in the fuel data the same service layer can be tightened to filter stations by true route-corridor distance.

## Geocoding Strategy

`import_fuel_stations` resolves each row's coordinates through three layers:

1. **Nominatim free-form queries** — street number + road, highway/exit intersections, cleaned address, name + city, and the full raw address, tried in order.
2. **Overpass 15 km radius search** — queries all `amenity=fuel` / `highway=services` nodes within 15 km of the city centre and fuzzy-matches against the normalised brand name (threshold 0.60).
3. **City centre fallback** — if both layers fail the station is placed at the municipality centroid so no row is silently dropped.

Results are cached in `geocode_cache.json` and reused on subsequent imports.

