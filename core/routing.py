import csv
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

import requests
from django.conf import settings
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D

from core.models import FuelStation as FuelStationModel
from django.contrib.gis.db.models.functions import Distance
import logging
log = logging.getLogger(__name__)
log.info("hit")
# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MILES_PER_METER       = 0.000621371
MILES_PER_GALLON      = 10
MAX_RANGE_MILES       = 500          # full-tank range
TANK_CAPACITY         = MAX_RANGE_MILES / MILES_PER_GALLON   # gallons

SAMPLE_INTERVAL_MILES = 50           # corridor sampling pitch
SEARCH_RADIUS_MILES   = 40           # on-route candidate radius
CANDIDATES_PER_SAMPLE = 5            # candidates kept per sample point

# ── Detour road-factor constants ──────────────────────────────────────────────
# Road distance is always ≥ straight-line (haversine). Empirically, the US
# road network averages ~1.30× haversine for regional trips. For conservative
# worst-case we use 1.60× (captures rural, winding, or indirect roads).
# The blended estimate weights the conservative side more heavily so we never
# recommend a detour that turns out to cost more than expected.
ROAD_FACTOR_EXPECTED     = 1.30
ROAD_FACTOR_CONSERVATIVE = 1.60
ROAD_FACTOR_WEIGHT       = 0.40      # weight on expected; (1 - w) on conservative

DETOUR_SEARCH_RADIUS     = 75        # miles, wider radius for detour candidates
MAX_DETOUR_ONE_WAY_MILES = 50        # hard cap on one-way detour distance

# ── Start-fuel modes ─────────────────────────────────────────────────────────
START_FUEL_FIXED_GALLONS = 5         # gallons when using the "partial tank" mode
START_MODE_NEAREST       = 'nearest_station'   # go to nearest station first
START_MODE_PARTIAL       = 'partial_tank'      # begin with START_FUEL_FIXED_GALLONS

# ── DP discretisation ────────────────────────────────────────────────────────
# Fuel state is discretised into steps so the DP state space stays manageable.
# Finer steps = more accurate, larger graph. 0.5-gallon steps are a good balance.
FUEL_STEP_GALLONS        = 0.5
FUEL_LEVELS              = [
    round(i * FUEL_STEP_GALLONS, 1)
    for i in range(int(TANK_CAPACITY / FUEL_STEP_GALLONS) + 1)
]   # [0.0, 0.5, 1.0, …, 50.0]


# ─────────────────────────────────────────────────────────────────────────────
# Geo / regex helpers (unchanged from original)
# ─────────────────────────────────────────────────────────────────────────────

STATE_RE = re.compile(
    r"\b(A[LKZR]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]"
    r"|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AIT]|W[AIVY])\b"
)
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
        params={'q': location, 'format': 'jsonv2', 'addressdetails': 1, 'limit': 1},
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
    coord = _parse_coordinate_location(location)
    if coord:
        return coord, 0
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
            ('vehicle', 'car'), ('locale', 'en'),
            ('points_encoded', 'false'), ('instructions', 'true'),
            ('calc_points', 'true'), ('key', settings.GRAPHHOPPER_API_KEY),
        ],
        timeout=12,
    )
    response.raise_for_status()
    paths = response.json().get('paths', [])
    if not paths:
        raise RouteError('GraphHopper did not return a route.')
    path = paths[0]
    coords = path.get('points', {}).get('coordinates', [])
    if len(coords) < 2:
        raise RouteError('GraphHopper route did not include a usable geometry.')
    return {
        'distance_miles': round(path['distance'] * MILES_PER_METER, 2),
        'time_ms':        path.get('time'),
        'coordinates':    coords,
        'instructions':   path.get('instructions', []),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def _haversine_miles(a, b):
    """Straight-line distance in miles between two [lon, lat] points."""
    lon1, lat1 = a
    lon2, lat2 = b
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


def _point_at_mile(coordinates, target_mile):
    """Interpolate a [lon, lat] point at exactly target_mile along a polyline."""
    if target_mile <= 0:
        return coordinates[0]
    traveled = 0.0
    for prev, curr in zip(coordinates, coordinates[1:]):
        seg = _haversine_miles(prev, curr)
        if traveled + seg >= target_mile:
            ratio = (target_mile - traveled) / seg if seg else 0
            return [
                round(prev[0] + (curr[0] - prev[0]) * ratio, 6),
                round(prev[1] + (curr[1] - prev[1]) * ratio, 6),
            ]
        traveled += seg
    return coordinates[-1]


def _estimate_road_miles(straight_line_miles: float) -> float:
    """
    Estimate real road distance from a straight-line (haversine) distance,
    without making any additional API calls.

    Two bounds:
      - Expected   = straight × ROAD_FACTOR_EXPECTED     (1.30) — US empirical average
      - Conservative = straight × ROAD_FACTOR_CONSERVATIVE (1.60) — rural/winding worst-case

    We blend them weighted toward the conservative side so the optimizer never
    recommends a detour that turns out to cost more than modelled.

        estimate = w × expected + (1−w) × conservative
                 = straight × (w×1.30 + (1−w)×1.60)
                 = straight × 1.48   (at w=0.40)

    For a detour the round-trip is 2 × one-way road estimate.
    """
    expected     = straight_line_miles * ROAD_FACTOR_EXPECTED
    conservative = straight_line_miles * ROAD_FACTOR_CONSERVATIVE
    return (ROAD_FACTOR_WEIGHT * expected
            + (1 - ROAD_FACTOR_WEIGHT) * conservative)


# ─────────────────────────────────────────────────────────────────────────────
# Nearest station helper (for START_MODE_NEAREST)
# ─────────────────────────────────────────────────────────────────────────────

def _nearest_station(lon: float, lat: float) -> Optional[FuelStationModel]:
    pt = Point(lon, lat, srid=4326)
    return (
        FuelStationModel.objects
        .filter(location__distance_lte=(pt, D(mi=SEARCH_RADIUS_MILES)))
        .annotate(dist=Distance('location', pt))
        .order_by('dist')
        .first()
    )


# ─────────────────────────────────────────────────────────────────────────────
# Corridor candidate builder
# ─────────────────────────────────────────────────────────────────────────────

def _candidate_stations_from_postgis(
    coordinates: list,
    distance_miles: float,
) -> dict[float, list]:
    """
    Walk the polyline every SAMPLE_INTERVAL_MILES, query PostGIS ST_DWithin
    at each sample point.  Returns {route_mile: [FuelStationModel, ...]} with
    candidates sorted cheapest-first at each milepost.
    """
    candidates: dict[float, list] = {}
    mile = 0.0
    while mile <= distance_miles:
        lon, lat = _point_at_mile(coordinates, mile)
        pt = Point(lon, lat, srid=4326)
        hits = list(
            FuelStationModel.objects
            .filter(location__distance_lte=(pt, D(mi=SEARCH_RADIUS_MILES)))
            .order_by('price')[:CANDIDATES_PER_SAMPLE]
        )
        if hits:
            candidates[round(mile, 1)] = hits
        mile += SAMPLE_INTERVAL_MILES
    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# Detour candidates  (wider radius, price-sorted)
# ─────────────────────────────────────────────────────────────────────────────

def _detour_candidates_near(
    lon: float, lat: float, exclude_ids: frozenset
) -> list:
    """Stations within DETOUR_SEARCH_RADIUS sorted by price, excluding used ones."""
    pt = Point(lon, lat, srid=4326)
    return list(
        FuelStationModel.objects
        .filter(location__distance_lte=(pt, D(mi=DETOUR_SEARCH_RADIUS)))
        .exclude(opis_id__in=exclude_ids)
        .order_by('price')[:15]
    )


# ─────────────────────────────────────────────────────────────────────────────
# DP node  (a candidate refuelling station on the route)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DPNode:
    """Represents a potential refuel point in the DP graph."""
    node_id:     str           # unique within graph  (opis_id or 'start' / 'end')
    route_mile:  float         # position on the main route
    point:       list          # [lon, lat] on the main route (not the station itself)
    station:     Optional[FuelStationModel]  # None for start / end nodes
    price:       float         # price per gallon  (0.0 for start/end sentinel nodes)
    is_detour:   bool = False   # station is off-route
    detour_miles: float = 0.0  # estimated road round-trip if is_detour
    detour_fuel_cost: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# DP graph builder
# ─────────────────────────────────────────────────────────────────────────────
def _build_dp_nodes(
    coordinates: list,
    distance_miles: float,
    candidate_map: dict,
    start_fuel: float,
) -> list[DPNode]:
    """
    Construct the ordered list of DPNodes:

      [start_node]  +  [on-route stations]  +  [detour stations]  +  [end_node]

    On-route stations come from the SEARCH_RADIUS_MILES corridor.
    Detour stations come from a DETOUR_SEARCH_RADIUS sweep at every sample point.
    We deduplicate: the same opis_id at an earlier sample point wins.

    NOTE: "on-route" candidates are only found via a SEARCH_RADIUS_MILES (40 mi)
    radius search around sampled route points — that does NOT mean they're
    actually close to the route line. Anything beyond ON_ROUTE_THRESHOLD_MILES
    from the sample point still gets treated as a detour, with detour
    miles/cost computed the same way as the wider detour sweep.
    """
    ON_ROUTE_THRESHOLD_MILES = 5  # beyond this, treat as a detour even if found "on-route"

    seen_ids: set[str] = set()
    nodes: list[DPNode] = []

    # ── Start sentinel ────────────────────────────────────────────────────────
    nodes.append(DPNode(
        node_id='__start__',
        route_mile=0.0,
        point=list(coordinates[0]),
        station=None,
        price=0.0,
    ))

    sorted_miles = sorted(candidate_map.keys())

    for mile in sorted_miles:
        route_pt = _point_at_mile(coordinates, mile)
        on_route_stations = candidate_map[mile]

        # ── On-route candidates ───────────────────────────────────────────────
        for db_station in on_route_stations:
            if db_station.opis_id in seen_ids:
                continue
            seen_ids.add(db_station.opis_id)

            s_lon = db_station.location.x
            s_lat = db_station.location.y
            one_way_haversine = _haversine_miles(
                (route_pt[0], route_pt[1]), (s_lon, s_lat)
            )

            if one_way_haversine > ON_ROUTE_THRESHOLD_MILES:
                # Found within SEARCH_RADIUS_MILES of the sample point, but
                # still meaningfully off the route — treat as a detour.
                one_way_road = _estimate_road_miles(one_way_haversine)
                round_trip_road = one_way_road * 2
                detour_fuel_cost = (round_trip_road / MILES_PER_GALLON) * float(db_station.price)

                nodes.append(DPNode(
                    node_id=db_station.opis_id,
                    route_mile=mile,
                    point=route_pt,
                    station=db_station,
                    price=float(db_station.price),
                    is_detour=True,
                    detour_miles=round(round_trip_road, 2),
                    detour_fuel_cost=round(detour_fuel_cost, 2),
                ))
            else:
                nodes.append(DPNode(
                    node_id=db_station.opis_id,
                    route_mile=mile,
                    point=route_pt,
                    station=db_station,
                    price=float(db_station.price),
                    is_detour=False,
                ))

        # ── Detour candidates (wider sweep) ───────────────────────────────────
        best_on_route_price = (
            min(float(s.price) for s in on_route_stations)
            if on_route_stations else float('inf')
        )

        for db_station in _detour_candidates_near(
            route_pt[0], route_pt[1], frozenset(seen_ids)
        ):
            if db_station.opis_id in seen_ids:
                continue
            price = float(db_station.price)
            if price >= best_on_route_price:
                # No point paying for a detour to a station that's not cheaper
                continue

            s_lon = db_station.location.x
            s_lat = db_station.location.y
            one_way_haversine = _haversine_miles(
                (route_pt[0], route_pt[1]), (s_lon, s_lat)
            )
            if one_way_haversine > MAX_DETOUR_ONE_WAY_MILES:
                continue

            # Round-trip road estimate (no extra API calls)
            one_way_road = _estimate_road_miles(one_way_haversine)
            round_trip_road = one_way_road * 2
            detour_fuel_cost = (round_trip_road / MILES_PER_GALLON) * price

            seen_ids.add(db_station.opis_id)
            nodes.append(DPNode(
                node_id=db_station.opis_id,
                route_mile=mile,          # anchored at the sample point where found
                point=route_pt,
                station=db_station,
                price=price,
                is_detour=True,
                detour_miles=round(round_trip_road, 2),
                detour_fuel_cost=round(detour_fuel_cost, 2),
            ))

    # ── End sentinel ──────────────────────────────────────────────────────────
    nodes.append(DPNode(
        node_id='__end__',
        route_mile=distance_miles,
        point=list(coordinates[-1]),
        station=None,
        price=0.0,
    ))

    # Sort by route_mile so DP forward pass visits nodes in order
    nodes.sort(key=lambda n: n.route_mile)
    return nodes
# ─────────────────────────────────────────────────────────────────────────────
# Dijkstra / DP over (node, fuel) states
# ─────────────────────────────────────────────────────────────────────────────

def _snap_fuel(gallons: float) -> float:
    """Round fuel to nearest FUEL_STEP_GALLONS to keep state space discrete."""
    return round(round(gallons / FUEL_STEP_GALLONS) * FUEL_STEP_GALLONS, 1)

def _dp_optimal_stops(nodes: list[DPNode], start_fuel: float) -> list[dict]:
    import heapq

    N   = len(nodes)
    INF = float('inf')

    dist: dict[tuple, float] = {}
    pred: dict[tuple, tuple] = {}

    start_f     = _snap_fuel(start_fuel)
    start_state = (0, start_f)
    dist[start_state] = 0.0
    heap = [(0.0, 0, start_f)]
    end_idx = N - 1

    while heap:
        cost, i, f = heapq.heappop(heap)

        if cost > dist.get((i, f), INF):
            continue

        if i == end_idx:
            break

        node_i           = nodes[i]
        max_reach_miles  = node_i.route_mile + f * MILES_PER_GALLON  # ← hard range limit

        for j in range(i + 1, N):
            node_j = nodes[j]

            # ── Hard reachability gate ────────────────────────────────────────
            # route_segment is the along-route distance to this node's anchor.
            # If even getting to this anchor exhausts the tank, nothing further
            # can be reached either — break the inner loop entirely.
            route_segment = node_j.route_mile - node_i.route_mile
            if route_segment > max_reach_miles - node_i.route_mile:
                break   # sorted by mile, so all subsequent nodes are also unreachable

            total_drive   = route_segment + node_j.detour_miles
            fuel_consumed = total_drive / MILES_PER_GALLON

            if fuel_consumed > f:
                continue  # detour pushes it over — skip this node, keep scanning

            fuel_after = f - fuel_consumed

            # ── End node — no purchase needed ─────────────────────────────────
            if node_j.node_id == '__end__':
                new_f  = _snap_fuel(fuel_after)
                state  = (j, new_f)
                if cost < dist.get(state, INF):
                    dist[state] = cost
                    pred[state] = (i, f, None)
                    heapq.heappush(heap, (cost, j, new_f))
                continue

            # ── Refuel stop — fill to max ─────────────────────────────────────
            gallons_bought  = max(0.0, TANK_CAPACITY - fuel_after)
            edge_cost       = gallons_bought * node_j.price + node_j.detour_fuel_cost
            new_cost        = cost + edge_cost
            new_f           = _snap_fuel(TANK_CAPACITY)
            state           = (j, new_f)

            if new_cost < dist.get(state, INF):
                dist[state] = new_cost
                pred[state] = (
                    i, f,
                    _make_stop_record(
                        node_j, fuel_consumed, gallons_bought, i,
                        miles_from_prev=total_drive,   # ← route_segment + detour_miles
                    )
                )
                heapq.heappush(heap, (new_cost, j, new_f))

    # ── Reconstruct ───────────────────────────────────────────────────────────
    best_cost, best_state = INF, None
    for state, c in dist.items():
        if state[0] == end_idx and c < best_cost:
            best_cost, best_state = c, state

    if best_state is None:
        return []

    stops = []
    state = best_state
    while state in pred:
        prev_i, prev_f, stop_record = pred[state]
        if stop_record is not None:
            stops.append(stop_record)
        state = (prev_i, prev_f)

    stops.reverse()
    for seq, stop in enumerate(stops, start=1):
        stop['sequence'] = seq
    return stops

# ── In _make_stop_record, add segment_miles as a parameter ──────────────────

def _make_stop_record(
    node: DPNode,
    fuel_consumed_to_reach: float,
    gallons_bought: float,
    prev_node_idx: int,
    miles_from_prev: float,      # ← new: actual drive miles from previous stop
) -> dict:
    lon, lat = node.point
    gallons_bought   = round(gallons_bought, 4)
    fuel_cost        = round(gallons_bought * node.price + node.detour_fuel_cost, 2)

    stop = {
        'sequence':          0,
        'route_mile':        round(node.route_mile, 2),
        'miles_from_prev':   round(miles_from_prev, 2),   # ← actual drive distance
        'gallons':           round(gallons_bought, 4),
        'fuel_cost':         fuel_cost,
        'map_marker':        {'lat': lat, 'lng': lon},
        'is_detour':         node.is_detour,
        'station': {
            'opis_id':          node.station.opis_id,
            'name':             node.station.name,
            'address':          node.station.address,
            'city':             node.station.city,
            'state':            node.station.state,
            'price_per_gallon': node.price,
        },
    }
    if node.is_detour:
        stop['detour'] = {
            'one_way_road_miles':    round(node.detour_miles / 2, 2),
            'round_trip_road_miles': node.detour_miles,
            'detour_fuel_cost':      node.detour_fuel_cost,
            'road_factor_used':      round(
                ROAD_FACTOR_WEIGHT * ROAD_FACTOR_EXPECTED
                + (1 - ROAD_FACTOR_WEIGHT) * ROAD_FACTOR_CONSERVATIVE, 3
            ),
        }
    return stop


def _nearest_stop_for_candidate(node: DPNode, stops: list[dict]) -> Optional[dict]:
    if not stops:
        return None
    return min(
        stops,
        key=lambda stop: abs(float(stop.get('route_mile', 0.0)) - node.route_mile),
    )


def _debug_station_record(node: DPNode, stops: list[dict], total_cost: float) -> Optional[dict]:
    if node.station is None:
        return None

    selected_stop = _nearest_stop_for_candidate(node, stops)
    selected_ids = {stop.get('station', {}).get('opis_id') for stop in stops}
    is_selected = node.station.opis_id in selected_ids

    gallons = (
        float(selected_stop.get('gallons', TANK_CAPACITY))
        if selected_stop else TANK_CAPACITY
    )
    selected_stop_cost = (
        float(selected_stop.get('fuel_cost', 0.0))
        if selected_stop else 0.0
    )
    candidate_stop_cost = round((gallons * node.price) + node.detour_fuel_cost, 2)
    total_if_chosen = (
        total_cost if is_selected
        else round(total_cost - selected_stop_cost + candidate_stop_cost, 2)
    )

    lon, lat = node.point
    station_lon = node.station.location.x
    station_lat = node.station.location.y
    record = {
        'route_mile': round(node.route_mile, 2),
        'map_marker': {'lat': station_lat, 'lng': station_lon},
        'route_anchor': {'lat': lat, 'lng': lon},
        'is_selected': is_selected,
        'is_detour': node.is_detour,
        'gallons_if_chosen': round(gallons, 4),
        'fuel_cost_if_chosen': candidate_stop_cost,
        'total_fuel_cost_if_chosen': total_if_chosen,
        'delta_total_fuel_cost': round(total_if_chosen - total_cost, 2),
        'compared_stop_sequence': (
            selected_stop.get('sequence') if selected_stop else None
        ),
        'compared_stop_cost': round(selected_stop_cost, 2),
        'station': {
            'opis_id': node.station.opis_id,
            'name': node.station.name,
            'address': node.station.address,
            'city': node.station.city,
            'state': node.station.state,
            'price_per_gallon': node.price,
        },
    }
    if node.is_detour:
        record['detour'] = {
            'one_way_road_miles': round(node.detour_miles / 2, 2),
            'round_trip_road_miles': node.detour_miles,
            'detour_fuel_cost': node.detour_fuel_cost,
            'road_factor_used': round(
                ROAD_FACTOR_WEIGHT * ROAD_FACTOR_EXPECTED
                + (1 - ROAD_FACTOR_WEIGHT) * ROAD_FACTOR_CONSERVATIVE, 3
            ),
        }
    return record


def _debug_station_records(nodes: list[DPNode], stops: list[dict], total_cost: float) -> list[dict]:
    records = [
        record
        for record in (_debug_station_record(node, stops, total_cost) for node in nodes)
        if record is not None
    ]
    return sorted(records, key=lambda record: (
        record['route_mile'],
        record['station']['price_per_gallon'],
        record['station']['opis_id'],
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point — select_fuel_stops
# ─────────────────────────────────────────────────────────────────────────────
def select_fuel_stops(
    route: dict,
    start: dict,
    finish: dict,
    start_mode: str = START_MODE_NEAREST,
    start_fuel: float = START_FUEL_FIXED_GALLONS,   # ← accept it as a parameter
) -> tuple[list, int, list]:
    distance  = route['distance_miles']
    coords    = route['coordinates']
    extra_api = 0

    # ── Determine opening fuel level ─────────────────────────────────────────
    if start_mode == START_MODE_NEAREST:
        first_station = _nearest_station(start['lng'], start['lat'])
        if first_station is None:
            # No nearby station — fall back to whatever fuel the user has
            prefix_stop = None
        else:
            start_fuel = TANK_CAPACITY   # fill up at the nearest station first
            s_lon = first_station.location.x
            s_lat = first_station.location.y
            haversine_to_first = _haversine_miles(
                (start['lng'], start['lat']), (s_lon, s_lat)
            )
            road_to_first     = _estimate_road_miles(haversine_to_first)
            gallons_for_first = TANK_CAPACITY
            prefix_stop = {
                'sequence':        1,
                'route_mile':      0.0,
                'miles_from_prev': round(road_to_first, 2),
                'gallons':         round(gallons_for_first, 4),
                'fuel_cost':       round(gallons_for_first * float(first_station.price), 2),
                'map_marker':      {'lat': s_lat, 'lng': s_lon},
                'is_detour':       False,
                'note':            'nearest station at trip start',
                'station': {
                    'opis_id':          first_station.opis_id,
                    'name':             first_station.name,
                    'address':          first_station.address,
                    'city':             first_station.city,
                    'state':            first_station.state,
                    'price_per_gallon': float(first_station.price),
                },
            }
    else:
        # START_MODE_PARTIAL — use exactly what was passed in (user-supplied gallons)
        prefix_stop = None

    # ── Build candidate graph ─────────────────────────────────────────────────
    candidate_map = _candidate_stations_from_postgis(coords, distance)
    dp_nodes      = _build_dp_nodes(coords, distance, candidate_map, start_fuel)

    # ── Run DP optimizer ──────────────────────────────────────────────────────
    stops = _dp_optimal_stops(dp_nodes, start_fuel)

    log.info(
        "start_mode=%s  start_fuel=%.2f gal  max_first_stop_mile=%.0f  first_stop_mile=%s",
        start_mode,
        start_fuel,                                          # ← actual value, not constant
        start_fuel * MILES_PER_GALLON,                       # ← shows the range limit clearly
        stops[0]['route_mile'] if stops else 'NO STOPS',
    )

    # ── Prepend the nearest-station prefix stop if applicable ─────────────────
    if prefix_stop is not None:
        for stop in stops:
            stop['sequence'] += 1
        stops.insert(0, prefix_stop)

    return stops, extra_api, dp_nodes

def _extract_state_codes(*values):
    codes = set()
    for value in values:
        if not value:
            continue
        value = str(value).upper()
        codes.update(m.group(0) for m in STATE_RE.finditer(value))
    return codes


def _first_state_code(*values):
    return next(iter(_extract_state_codes(*values)), None)


def _is_usa(location):
    country = location.get('country')
    return country is None or str(country).strip().lower() in USA_COUNTRY_NAMES


# ─────────────────────────────────────────────────────────────────────────────
# plan_trip  — public API
# ─────────────────────────────────────────────────────────────────────────────

def plan_trip(
    start_location,
    finish_location,
    start_mode: str = START_MODE_NEAREST,
    start_fuel_gallons: float | None = None,
):
    """
    Plan a fuel-optimised road trip.

    start_mode:
      'nearest_station'  — driver starts with low fuel; go fill up nearby first.
      'partial_tank'     — driver starts with exactly START_FUEL_FIXED_GALLONS (5 gal).
    """
    start,  start_gc  = geocode_location(start_location)
    finish, finish_gc = geocode_location(finish_location)

    if not _is_usa(start) or not _is_usa(finish):
        raise RouteError('Start and finish must both be within the USA.')
    
    if start_mode == START_MODE_PARTIAL:
        opening_fuel = start_fuel_gallons if start_fuel_gallons is not None \
                       else START_FUEL_FIXED_GALLONS
    else:
        opening_fuel = TANK_CAPACITY

    route = get_route(start, finish)
    stops, extra_gc, debug_nodes = select_fuel_stops(
        route,
        start,
        finish,
        start_mode=start_mode,
        start_fuel=opening_fuel,
    )

    total_cost    = round(sum(s['fuel_cost'] for s in stops), 2)
    detour_stops  = [s for s in stops if s.get('is_detour')]
    detour_count  = len(detour_stops)
    detour_miles  = round(sum(s['detour']['round_trip_road_miles'] for s in detour_stops), 2)
    detour_saving = 0.0   # net saving is implicit in the DP optimality — no separate field needed
    debug_stations = _debug_station_records(debug_nodes, stops, total_cost)

    return {
        'start':  start,
        'finish': finish,
        'vehicle': {
            'max_range_miles':  MAX_RANGE_MILES,
            'miles_per_gallon': MILES_PER_GALLON,
            'tank_capacity_gal': TANK_CAPACITY,
        },
        'start_config': {
            'mode':              start_mode,
            'start_fuel_gallons': opening_fuel,
            'max_range_miles':   round(opening_fuel * MILES_PER_GALLON, 1),
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
        'fuel_stops':      stops,
        'debug_gas_stations': debug_stations,
        'nearby_stations': debug_stations,
        'total_fuel_cost': total_cost,
        'detour_summary': {
            'detours_taken':             detour_count,
            'total_detour_road_miles':   detour_miles,
            'road_factor_expected':      ROAD_FACTOR_EXPECTED,
            'road_factor_conservative':  ROAD_FACTOR_CONSERVATIVE,
            'road_factor_blend_weight':  ROAD_FACTOR_WEIGHT,
            'effective_road_factor':     round(
                ROAD_FACTOR_WEIGHT * ROAD_FACTOR_EXPECTED
                + (1 - ROAD_FACTOR_WEIGHT) * ROAD_FACTOR_CONSERVATIVE, 3
            ),
        },
        'external_api_calls': {
            'openstreetmap_geocoding':         start_gc + finish_gc + extra_gc,
            'openstreetmap_reverse_geocoding': 0,
            'graphhopper_routing':             1,
            'graphhopper_total':               1,
        },
    }
