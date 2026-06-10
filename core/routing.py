import csv
import math
import re
from dataclasses import dataclass
from functools import lru_cache

import requests
from django.conf import settings
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D

from core.models import FuelStation as FuelStationModel


MILES_PER_METER = 0.000621371
MILES_PER_GALLON = 10
MAX_RANGE_MILES = 500
SAMPLE_INTERVAL_MILES = 50    # how often we sample the polyline for candidates
SEARCH_RADIUS_MILES = 40      # ST_DWithin radius around each sample point
CANDIDATES_PER_SAMPLE = 6     # cheapest stations surfaced per sample

STATE_RE = re.compile(r"\b(A[LKZR]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AIT]|W[AIVY])\b")
USA_COUNTRY_NAMES = {'united states', 'united states of america', 'usa', 'us'}
US_STATES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'IA': 'Iowa',
    'ID': 'Idaho', 'IL': 'Illinois', 'IN': 'Indiana', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'MA': 'Massachusetts', 'MD': 'Maryland',
    'ME': 'Maine', 'MI': 'Michigan', 'MN': 'Minnesota', 'MO': 'Missouri',
    'MS': 'Mississippi', 'MT': 'Montana', 'NC': 'North Carolina', 'ND': 'North Dakota',
    'NE': 'Nebraska', 'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico',
    'NV': 'Nevada', 'NY': 'New York', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VA': 'Virginia', 'VT': 'Vermont', 'WA': 'Washington', 'WI': 'Wisconsin',
    'WV': 'West Virginia', 'WY': 'Wyoming',
}
COORDINATE_RE = re.compile(
    r"^\s*(?P<lat>-?\d+(?:\.\d+)?)\s*,\s*(?P<lng>-?\d+(?:\.\d+)?)\s*$"
)


class RouteError(Exception):
    pass


@dataclass(frozen=True)
class FuelStation:
    opis_id: str
    name: str
    address: str
    city: str
    state: str
    price: float


def _require_api_key():
    if not settings.GRAPHHOPPER_API_KEY:
        raise RouteError('GRAPHHOPPER_API_KEY is required.')


def _location_from_coordinates(location, lat, lng):
    state_code = _first_state_code(location)
    return {
        'input': location,
        'name': location,
        'country': 'United States',
        'state': US_STATES.get(state_code),
        'state_code': state_code,
        'lat': lat,
        'lng': lng,
    }


def _parse_coordinate_location(location):
    if isinstance(location, dict):
        if 'lat' not in location or 'lng' not in location:
            return None
        lat = float(location['lat'])
        lng = float(location['lng'])
        label = location.get('label') or f'{lat},{lng}'
        return _location_from_coordinates(label, lat, lng)

    match = COORDINATE_RE.match(str(location))
    if not match:
        return None
    lat = float(match.group('lat'))
    lng = float(match.group('lng'))
    return _location_from_coordinates(str(location), lat, lng)


@lru_cache(maxsize=512)
def _geocode_text_location(location):
    response = requests.get(
        'https://nominatim.openstreetmap.org/search',
        params={
            'q': location,
            'format': 'jsonv2',
            'addressdetails': 1,
            'limit': 1,
        },
        headers={'User-Agent': settings.NOMINATIM_USER_AGENT},
        timeout=8,
    )
    response.raise_for_status()
    hits = response.json()
    if not hits:
        raise RouteError(f'Could not geocode location: {location}')
    hit = hits[0]
    address = hit.get('address', {})
    return {
        'input': location,
        'name': hit.get('display_name') or location,
        'country': address.get('country'),
        'state': address.get('state'),
        'state_code': address.get('ISO3166-2-lvl4', '').split('-')[-1],
        'lat': float(hit['lat']),
        'lng': float(hit['lon']),
    }


def geocode_location(location):
    coordinate_location = _parse_coordinate_location(location)
    if coordinate_location:
        return coordinate_location, 0

    before = _geocode_text_location.cache_info().hits
    resolved = _geocode_text_location(str(location).strip())
    calls = 0 if _geocode_text_location.cache_info().hits > before else 1
    return resolved, calls


def get_route(start, finish):
    _require_api_key()
    response = requests.get(
        'https://graphhopper.com/api/1/route',
        params=[
            ('point', f"{start['lat']},{start['lng']}"),
            ('point', f"{finish['lat']},{finish['lng']}"),
            ('vehicle', 'car'),
            ('locale', 'en'),
            ('points_encoded', 'false'),
            ('instructions', 'true'),
            ('calc_points', 'true'),
            ('key', settings.GRAPHHOPPER_API_KEY),
        ],
        timeout=12,
    )
    response.raise_for_status()
    paths = response.json().get('paths', [])
    if not paths:
        raise RouteError('GraphHopper did not return a route.')
    path = paths[0]
    coordinates = path.get('points', {}).get('coordinates', [])
    if len(coordinates) < 2:
        raise RouteError('GraphHopper route did not include a usable geometry.')
    return {
        'distance_miles': round(path['distance'] * MILES_PER_METER, 2),
        'time_ms': path.get('time'),
        'coordinates': coordinates,
        'instructions': path.get('instructions', []),
    }


def _haversine_miles(a, b):
    lon1, lat1 = a
    lon2, lat2 = b
    radius = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    lat1 = math.radians(lat1)
    lat2 = math.radians(lat2)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def _point_at_mile(coordinates, target_mile):
    if target_mile <= 0:
        return coordinates[0]
    traveled = 0.0
    for previous, current in zip(coordinates, coordinates[1:]):
        segment = _haversine_miles(previous, current)
        if traveled + segment >= target_mile:
            ratio = (target_mile - traveled) / segment if segment else 0
            lon = previous[0] + (current[0] - previous[0]) * ratio
            lat = previous[1] + (current[1] - previous[1]) * ratio
            return [round(lon, 6), round(lat, 6)]
        traveled += segment
    return coordinates[-1]


def _candidate_stations_from_postgis(coordinates, distance_miles):
    """
    Walk the polyline every SAMPLE_INTERVAL_MILES, query PostGIS ST_DWithin
    at each point, and return a dict keyed by route_mile ->
    [cheapest FuelStationModel instances].

    One DB round-trip per sample point; GIST index makes each fast.
    """
    candidates: dict[float, list] = {}
    mile = 0.0
    while mile < distance_miles:
        lon, lat = _point_at_mile(coordinates, mile)
        pt = Point(lon, lat, srid=4326)
        nearby = (
            FuelStationModel.objects
            .filter(location__distance_lte=(pt, D(mi=SEARCH_RADIUS_MILES)))
            .order_by('price')[:CANDIDATES_PER_SAMPLE]
        )
        hits = list(nearby)
        if hits:
            candidates[mile] = hits
        mile += SAMPLE_INTERVAL_MILES
    return candidates


def _station_payload(db_station):
    payload = {
        'opis_id': db_station.opis_id,
        'name': db_station.name,
        'address': db_station.address,
        'city': db_station.city,
        'state': db_station.state,
        'price_per_gallon': float(db_station.price),
    }
    if db_station.location:
        payload['location'] = {
            'lat': round(float(db_station.location.y), 6),
            'lng': round(float(db_station.location.x), 6),
        }
    return payload


def _nearby_station_reason(sample_mile, db_station, selected_stops, distance):
    if sample_mile <= 10:
        return 'Skipped because this station is too close to the start of the trip.'

    nearby_selected = min(
        selected_stops,
        key=lambda stop: abs(stop['route_mile'] - sample_mile),
        default=None,
    )
    if nearby_selected:
        selected_station = nearby_selected['station']
        if db_station.opis_id == selected_station['opis_id']:
            return f"Selected as fuel stop #{nearby_selected['sequence']}."

        station_price = float(db_station.price)
        selected_price = float(selected_station['price_per_gallon'])
        if selected_price <= station_price:
            return (
                f"Not chosen because stop #{nearby_selected['sequence']} "
                f"({selected_station['name']}) was cheaper at "
                f"${selected_price:.3f}/gal versus ${station_price:.3f}/gal."
            )

        return (
            'Not chosen because the route planner did not need a refuel at this '
            'sample point to stay within the 500 mile range.'
        )

    if sample_mile >= distance - MAX_RANGE_MILES:
        return 'Skipped because the vehicle can reach the destination without refueling here.'

    return 'Not chosen because another reachable station produced the lower-cost fuel plan.'


def _nearby_station_options(candidate_map, selected_stops, distance):
    selected_ids = {stop['station']['opis_id'] for stop in selected_stops}
    options = []
    seen_ids = set()

    for sample_mile in sorted(candidate_map.keys()):
        for db_station in candidate_map[sample_mile]:
            if db_station.opis_id in selected_ids or db_station.opis_id in seen_ids:
                continue
            seen_ids.add(db_station.opis_id)
            station = _station_payload(db_station)
            marker = station.get('location')
            if not marker:
                continue
            options.append({
                'route_mile': round(float(sample_mile), 2),
                'map_marker': marker,
                'station': station,
                'reason': _nearby_station_reason(sample_mile, db_station, selected_stops, distance),
            })

    return options


def select_fuel_stops(route, start, finish):
    """
    Greedy forward pass over PostGIS candidates.

    State:
        current_mile  — where the truck is right now
        fuel_remaining — gallons still in tank (starts full)

    At each step we look ahead up to MAX_RANGE_MILES and pick the
    cheapest reachable station. We always fill the tank completely.
    """
    distance   = route['distance_miles']
    coords     = route['coordinates']

    # Build candidate map from PostGIS (no reverse-geocoding needed)
    candidate_map = _candidate_stations_from_postgis(coords, distance)
    sorted_miles  = sorted(candidate_map.keys())

    selected       = []
    used_ids       = set()
    current_mile   = 0.0
    fuel_remaining = MAX_RANGE_MILES / MILES_PER_GALLON   # start full

    sequence = 1
    while True:
        max_reachable_mile = current_mile + fuel_remaining * MILES_PER_GALLON

        # Are we close enough to finish that we don't need another stop?
        if max_reachable_mile >= distance:
            break

        # Find cheapest unused station reachable from current position
        best_station  = None
        best_stop_mile = None
        best_price    = float('inf')

        for sample_mile in sorted_miles:
            if sample_mile <= current_mile:
                continue
            if sample_mile > max_reachable_mile:
                break
            for db_station in candidate_map[sample_mile]:
                if db_station.opis_id in used_ids:
                    continue
                if float(db_station.price) < best_price:
                    best_price     = float(db_station.price)
                    best_station   = db_station
                    best_stop_mile = sample_mile

        if best_station is None:
            # No PostGIS candidate found — fallback: cheapest DB station overall
            fallback = (
                FuelStationModel.objects
                .exclude(opis_id__in=used_ids)
                .order_by('price')
                .first()
            )
            if fallback is None:
                break
            best_station   = fallback
            best_stop_mile = min(
                current_mile + MAX_RANGE_MILES * 0.8,
                distance - 1,
            )
            best_price = float(fallback.price)

        # Miles driven to reach this stop
        miles_to_stop  = best_stop_mile - current_mile
        fuel_used      = miles_to_stop / MILES_PER_GALLON
        gallons_bought = (MAX_RANGE_MILES / MILES_PER_GALLON) - (fuel_remaining - fuel_used)
        gallons_bought = max(0.0, round(gallons_bought, 4))

        lon, lat = _point_at_mile(coords, best_stop_mile)
        used_ids.add(best_station.opis_id)

        selected.append({
            'sequence':   sequence,
            'route_mile': round(float(best_stop_mile), 2),
            'gallons':    gallons_bought,
            'fuel_cost':  round(gallons_bought * best_price, 2),
            'map_marker': {'lat': lat, 'lng': lon},
            'station': _station_payload(best_station),
        })

        fuel_remaining  = MAX_RANGE_MILES / MILES_PER_GALLON   # filled up
        current_mile    = best_stop_mile
        sequence       += 1

    nearby_options = _nearby_station_options(candidate_map, selected, distance)
    return selected, nearby_options


def _extract_state_codes(*values):
    codes = set()
    for value in values:
        if not value:
            continue
        value = str(value).upper()
        codes.update(match.group(0).replace(' ', '') for match in STATE_RE.finditer(value))
    return codes


def _first_state_code(*values):
    codes = _extract_state_codes(*values)
    return next(iter(codes), None)


def _is_usa(location):
    country = location.get('country')
    return country is None or str(country).strip().lower() in USA_COUNTRY_NAMES


def plan_trip(start_location, finish_location):
    start,  start_geocode_calls  = geocode_location(start_location)
    finish, finish_geocode_calls = geocode_location(finish_location)
    if not _is_usa(start) or not _is_usa(finish):
        raise RouteError('Start and finish must both be within the USA.')
    route = get_route(start, finish)
    stops, nearby_options = select_fuel_stops(route, start, finish)
    total_cost = round(sum(stop['fuel_cost'] for stop in stops), 2)
    return {
        'start':  start,
        'finish': finish,
        'vehicle': {
            'max_range_miles':   MAX_RANGE_MILES,
            'miles_per_gallon':  MILES_PER_GALLON,
        },
        'route': {
            'distance_miles': route['distance_miles'],
            'time_ms':        route['time_ms'],
            'geojson': {
                'type':        'LineString',
                'coordinates': route['coordinates'],
            },
            'instructions': route['instructions'],
        },
        'fuel_stops':       stops,
        'nearby_stations':  nearby_options,
        'total_fuel_cost':  total_cost,
        'external_api_calls': {
            'openstreetmap_geocoding':          start_geocode_calls + finish_geocode_calls,
            'openstreetmap_reverse_geocoding':  0,   # eliminated by PostGIS
            'graphhopper_routing':              1,
            'graphhopper_total':                1,
        },
    }
