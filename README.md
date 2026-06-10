# Fuel Route API

Django API that plans a USA road trip, returns a map-ready route, chooses fuel stops for a vehicle with a 500 mile range, and estimates trip fuel cost at 10 MPG.

Geocoding uses OpenStreetMap Nominatim and routing uses GraphHopper. The API makes exactly one GraphHopper call per request: the route between the two geocoded coordinates.

## Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
set GRAPHHOPPER_API_KEY=your-graphhopper-api-key
set NOMINATIM_USER_AGENT=spotter-ai-fuel-route-api/1.0 your-email@example.com
python manage.py migrate
python manage.py runserver
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

Coordinates can be supplied for either endpoint to avoid a geocoding call:

```json
{
  "start_location": {"lat": 41.8781, "lng": -87.6298, "label": "Chicago, IL"},
  "finish_location": {"lat": 32.7767, "lng": -96.7970, "label": "Dallas, TX"}
}
```

Response includes:

- `route.geojson`: GeoJSON `LineString` using `[lng, lat]` coordinates for OpenStreetMap clients.
- `fuel_stops`: ordered fuel stops with route mile, gallons, estimated cost, selected station, and marker coordinates.
- `total_fuel_cost`: total dollars spent on fuel for the trip.
- `external_api_calls`: actual external call count, including `graphhopper_total: 1`. Two fresh text locations require two Nominatim geocode calls because Nominatim does not provide a batch geocoding endpoint. Cached text locations and coordinate inputs reduce that count.

Example:

```bash
curl -X POST http://127.0.0.1:8000/api/route-fuel/ ^
  -H "Content-Type: application/json" ^
  -d "{\"start_location\":\"Chicago, IL\",\"finish_location\":\"Dallas, TX\"}"
```

## Fuel Data

The service reads `fuel-prices-for-be-assessment (1).csv` at runtime and caches it in memory. The CSV does not include station latitude/longitude, so selected station records are optimized by available price data and route-state hints, while marker coordinates are placed on the GraphHopper route at the required refuel mile. With latitude/longitude in the fuel data, the same service layer can be tightened to filter stations by true route corridor distance.

## Tests

```bash
python manage.py test
```
