import bisect
from collections import deque
import csv
import math
import re
import time
import zlib
import json
import hashlib
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional

import httpx
import numpy as np
import requests
from shapely.geometry import LineString as ShapelyLineString
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from django.conf import settings
from django.contrib.gis.geos import Point, LineString
from django.contrib.gis.measure import D
from django.core.cache import cache

from core.models import FuelStation as FuelStationModel
from core.profiling import profiled, profile_step
from django.contrib.gis.db.models.functions import Distance
from shapely.geometry import Point as ShapelyPoint
from shapely import wkb as shapely_wkb

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_in_memory_route_cache: dict[str, dict] = {}

MILES_PER_METER       = 0.000621371  # unit conversion: meters → miles (GH returns meters)
MILES_PER_GALLON      = 10           # assumed vehicle fuel efficiency
MAX_RANGE_MILES       = 500          # max distance on a full tank
TANK_CAPACITY         = MAX_RANGE_MILES / MILES_PER_GALLON  # = 50 gallons

SAMPLE_INTERVAL_MILES = 50           # probe the route every 50 mi to find nearby stations
SEARCH_RADIUS_MILES   = 40           # only consider stations within 40 mi of each sample point
CANDIDATES_PER_SAMPLE = 5            # keep the 5 cheapest stations per sample bucket

ROAD_FACTOR_EXPECTED     = 1.30      # optimistic road-distance multiplier (straight-line × 1.30)
ROAD_FACTOR_CONSERVATIVE = 1.60      # pessimistic multiplier for winding/rural roads
ROAD_FACTOR_WEIGHT       = 0.40      # blend: 40% expected + 60% conservative → used for detour cost estimate

DETOUR_SEARCH_RADIUS     = 75        # cast a wider net for the single corridor PostGIS query
MAX_DETOUR_ONE_WAY_MILES = 50        # hard cap: never suggest a detour longer than this one-way

START_FUEL_FIXED_GALLONS = 5         # default opening fuel in partial_tank mode
START_MODE_NEAREST       = 'nearest_station'  # fill up at the closest station before departing
START_MODE_PARTIAL       = 'partial_tank'     # start with whatever fuel the user declares

FUEL_STEP_GALLONS = 0.5  # DP state granularity — fuel levels snap to 0.5-gal increments

FUEL_LEVELS = [
    round(i * FUEL_STEP_GALLONS, 1)
    for i in range(int(TANK_CAPACITY / FUEL_STEP_GALLONS) + 1)
]

# ─────────────────────────────────────────────────────────────────────────────
# GraphHopper HTTP client
# ─────────────────────────────────────────────────────────────────────────────

_GH_CLIENT = httpx.Client(
    http2=True,
    headers={
        "Accept-Encoding": "gzip",
        "User-Agent": "TripOptimizer/1.0",
    },
    timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
)

# ─────────────────────────────────────────────────────────────────────────────
# Route geometry cache
# ─────────────────────────────────────────────────────────────────────────────

ROUTE_CACHE_TTL     = 60 * 60 * 24 * 7
ROUTE_CACHE_VERSION = "v1"


def _route_cache_key(start: dict, finish: dict) -> str:
    origin = f"{round(start['lat'], 4)},{round(start['lng'], 4)}"
    dest   = f"{round(finish['lat'], 4)},{round(finish['lng'], 4)}"
    token  = hashlib.sha256(f"{origin}→{dest}".encode()).hexdigest()[:24]
    return f"route:{ROUTE_CACHE_VERSION}:{token}"


def _cache_get_route(key: str) -> Optional[dict]:
    log.info("cache_get_route key=%s in_memory=%s", key, key in _in_memory_route_cache)
    if key in _in_memory_route_cache:
        return _in_memory_route_cache[key]
    blob = cache.get(key)
    if blob is None:
        return None
    payload = json.loads(zlib.decompress(blob).decode())
    _in_memory_route_cache[key] = payload
    return payload


def _cache_set_route(key: str, payload: dict) -> None:
    _in_memory_route_cache[key] = payload
    blob = zlib.compress(json.dumps(payload).encode(), level=6)
    cache.set(key, blob, timeout=ROUTE_CACHE_TTL)




RDP_EPSILON = 0.01   # degrees


def _simplify_coordinates(coordinates: list) -> list:
    """
    Apply Ramer-Douglas-Peucker to a [[lon, lat], ...] list via Shapely/GEOS.
    Returns a simplified list; never fewer than 2 points.
    ~5 ms on 28,943 points vs ~8,000 ms with pure-Python rdp.
    """
    if len(coordinates) <= 2:
        return coordinates
    line = ShapelyLineString(coordinates)
    simplified = line.simplify(RDP_EPSILON, preserve_topology=False)
    return [list(pt) for pt in simplified.coords]


OPT_CACHE_VERSION = "v1"  # bump separately if epsilon or cumdist logic changes


def _opt_cache_key(raw_key: str) -> str:
    return f"{raw_key}:opt:{OPT_CACHE_VERSION}"


def _cache_get_opt(opt_key: str) -> Optional[dict]:
    if opt_key in _in_memory_route_cache:
        return _in_memory_route_cache[opt_key]
    blob = cache.get(opt_key)
    if blob is None:
        return None
    payload = json.loads(blob.decode() if isinstance(blob, bytes) else blob)
    _in_memory_route_cache[opt_key] = payload
    return payload


def _cache_set_opt(opt_key: str, payload: dict) -> None:
    _in_memory_route_cache[opt_key] = payload
    cache.set(opt_key, json.dumps(payload), timeout=ROUTE_CACHE_TTL)


# ─────────────────────────────────────────────────────────────────────────────
# Retry policy for GraphHopper calls
# ─────────────────────────────────────────────────────────────────────────────

_GH_RETRY = retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# GraphHopper fetch — raw, cached
# ─────────────────────────────────────────────────────────────────────────────

def _require_api_key():
    if not settings.GRAPHHOPPER_API_KEY:
        raise RouteError('GRAPHHOPPER_API_KEY is required.')


@_GH_RETRY
def _fetch_graphhopper(start: dict, finish: dict) -> dict:
    resp = _GH_CLIENT.get(
        "https://graphhopper.com/api/1/route",
        params=[
            ("point", f"{start['lat']},{start['lng']}"),
            ("point", f"{finish['lat']},{finish['lng']}"),
            ("vehicle",         "car"),
            ("locale",          "en"),
            ("points_encoded",  "false"),
            ("instructions",    "true"),
            ("calc_points",     "true"),
            ("key",             settings.GRAPHHOPPER_API_KEY),
        ],
    )

    if not resp.is_success:
        try:
            body = resp.json()
            gh_message = body.get("message") or body.get("hints") or body
        except Exception:
            gh_message = resp.text
        log.error(
            "GraphHopper %s | start=(%s,%s) finish=(%s,%s) | %s",
            resp.status_code,
            start['lat'], start['lng'],
            finish['lat'], finish['lng'],
            gh_message,
        )
        # 400 from GH is a bad request (out-of-bounds, unsupported route) --
        # no point retrying it, so raise RouteError directly instead of
        # letting tenacity retry a request that will never succeed.
        if resp.status_code == 400:
            raise RouteError(f"GraphHopper rejected the route request: {gh_message}")

    resp.raise_for_status()

    paths = resp.json().get("paths", [])
    if not paths:
        raise RouteError("GraphHopper did not return a route.")
    path = paths[0]
    coords = path.get("points", {}).get("coordinates", [])
    if len(coords) < 2:
        raise RouteError("GraphHopper route did not include usable geometry.")
    return path


def _raw_route(start: dict, finish: dict) -> tuple[dict, bool]:
    _require_api_key()
    key = _route_cache_key(start, finish)
    cached = _cache_get_route(key)
    if cached is not None:
        return cached, True
    with profile_step("graphhopper_route_request"):
        path = _fetch_graphhopper(start, finish)
    payload = {
        "distance_miles": round(path["distance"] * MILES_PER_METER, 2),
        "time_ms":        path.get("time"),
        "coordinates":    path["points"]["coordinates"],
        "instructions":   path.get("instructions", []),
    }
    _cache_set_route(key, payload)
    return payload, False


# ─────────────────────────────────────────────────────────────────────────────
# Public route accessors
# ─────────────────────────────────────────────────────────────────────────────

def get_route(start: dict, finish: dict) -> dict:
    """Full route for the frontend: full-resolution coordinates + instructions."""
    payload, _ = _raw_route(start, finish)
    return payload


def get_route_for_optimizer(start: dict, finish: dict) -> dict:
    """
    RDP-simplified route for the optimizer. Simplified coords and their
    cumulative distances are cached together in Redis so cold boots (dyno
    restart, Redis flush) don't re-run simplification on every request.

    Pipeline:
      1. L1 in-memory check
      2. Redis opt key check
      3. Raw payload → simplify → build cumdist → write to Redis + L1
    """
    raw_key = _route_cache_key(start, finish)
    opt_key = _opt_cache_key(raw_key)

    cached = _cache_get_opt(opt_key)
    if cached is not None:
        return cached

    # Need to compute — ensure raw payload is available first
    payload, _ = _raw_route(start, finish)

    t0 = time.perf_counter()
    simplified = _simplify_coordinates(payload["coordinates"])
    elapsed_simplify = (time.perf_counter() - t0) * 1000
    log.info(
        "simplify elapsed_ms=%.3f input=%d output=%d epsilon=%s",
        elapsed_simplify,
        len(payload["coordinates"]),
        len(simplified),
        RDP_EPSILON,
    )
    cumdist = _build_cumulative_distances(simplified)
    result = {
        "distance_miles": payload["distance_miles"],
        "time_ms":        payload["time_ms"],
        "coordinates":    simplified,
        "cumdist":        cumdist,   # pre-computed alongside coords; free to cache together
    }
    _cache_set_opt(opt_key, result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Geo / regex helpers
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
    with profile_step("geocode_location", location_type=type(location).__name__):
        coord = _parse_coordinate_location(location)
        if coord:
            return coord, 0
        before = _geocode_text_location.cache_info().hits
        resolved = _geocode_text_location(str(location).strip())
        calls = 0 if _geocode_text_location.cache_info().hits > before else 1
        return resolved, calls


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


def _build_cumulative_distances(coordinates: list) -> list[float]:
    """
    Build a cumulative distance array for a sequence of coordinates.

    Each element in the returned list represents the total distance
    traveled (in miles) from the first coordinate up to the coordinate
    at the same index. The first value is always 0.0.
    """
    cumdist = [0.0]

    # Add the distance between each consecutive coordinate pair
    # to maintain a running total of traveled distance.
    for prev, curr in zip(coordinates, coordinates[1:]):
        cumdist.append(cumdist[-1] + _haversine_miles(prev, curr))

    return cumdist

def _point_at_mile_indexed(coordinates: list, cumdist: list[float], target_mile: float):
    """
    Return the interpolated coordinate located at the specified cumulative
    distance along a route.

    Uses the precomputed cumulative distance array to locate the route
    segment containing the target mile marker, then linearly interpolates
    between the segment's start and end coordinates.
    """
    if target_mile <= 0:
        return coordinates[0]
    if target_mile >= cumdist[-1]:
        return coordinates[-1]

    # Find the segment whose cumulative distance range contains target_mile.
    idx = bisect.bisect_right(cumdist, target_mile) - 1
    idx = max(0, min(idx, len(coordinates) - 2))

    seg_start, seg_end = cumdist[idx], cumdist[idx + 1]
    seg = seg_end - seg_start

    # Calculate the relative position within the segment (0.0–1.0).
    ratio = (target_mile - seg_start) / seg if seg else 0.0

    prev, curr = coordinates[idx], coordinates[idx + 1]

    # Linearly interpolate latitude and longitude.
    return [
        round(prev[0] + (curr[0] - prev[0]) * ratio, 6),
        round(prev[1] + (curr[1] - prev[1]) * ratio, 6),
    ]


def _estimate_road_miles(straight_line_miles: float) -> float:
    expected     = straight_line_miles * ROAD_FACTOR_EXPECTED
    conservative = straight_line_miles * ROAD_FACTOR_CONSERVATIVE
    return (ROAD_FACTOR_WEIGHT * expected
            + (1 - ROAD_FACTOR_WEIGHT) * conservative)


# ─────────────────────────────────────────────────────────────────────────────
# NumPy haversine — vectorized over an array of (lon, lat) points
# ─────────────────────────────────────────────────────────────────────────────
# Called with a single query point and a (N, 2) station array.
# Returns a (N,) float64 array of distances in miles.
#
# Replaces the per-station Python loop in _candidate_stations_from_postgis and
# the full O(N) scan in _detour_candidates_near.  At N=1,404 stations the pure-
# Python loop costs ~3 ms per sample point × 61 sample points ≈ 183 ms just for
# haversine alone, before the sort.  The vectorized version processes all 1,404
# stations against one query point in ~0.05 ms — roughly 60× faster, collapsing
# the ~1,000 ms build_candidate_stations step to ~30 ms.

def _haversine_miles_vec(query_lonlat: np.ndarray, station_lonlats: np.ndarray) -> np.ndarray:
    """
    Vectorized haversine: query_lonlat is shape (2,), station_lonlats is (N, 2).
    Returns (N,) distances in miles.
    """
    R = 3958.8
    lon1 = np.radians(query_lonlat[0])
    lat1 = np.radians(query_lonlat[1])
    lon2 = np.radians(station_lonlats[:, 0])
    lat2 = np.radians(station_lonlats[:, 1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(h))


# ─────────────────────────────────────────────────────────────────────────────
# Station coordinate array — built once, reused for every sample point
# ─────────────────────────────────────────────────────────────────────────────

def _station_coords_array(all_stations: list) -> np.ndarray:
    """Return (N, 2) float64 array of [lon, lat] for each station."""
    if not all_stations:
        return np.empty((0, 2), dtype=np.float64)
    return np.array(
        [[s.location.x, s.location.y] for s in all_stations],
        dtype=np.float64,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Nearest station helper
# ─────────────────────────────────────────────────────────────────────────────

def _nearest_station(lon: float, lat: float) -> Optional[FuelStationModel]:
    pt = Point(lon, lat, srid=4326)
    with profile_step("db_nearest_station"):
        return (
            FuelStationModel.objects
            .filter(location__distance_lte=(pt, D(mi=SEARCH_RADIUS_MILES)))
            .annotate(dist=Distance('location', pt))
            .order_by('dist')
            .first()
        )
CORRIDOR_BUFFER_MILES   = 0.5                             # ← tightened: true on-route stations only
CORRIDOR_BUFFER_DEGREES = CORRIDOR_BUFFER_MILES / 69.0   # ~0.00725°
 
POLYGON_CACHE_VERSION = "v2"
 

# ─────────────────────────────────────────────────────────────────────────────
# Corridor candidate builder — vectorized
# ─────────────────────────────────────────────────────────────────────────────

def _all_corridor_stations(coordinates: list, distance_miles: float) -> list:
    """
    Single spatial query: fetch every station within DETOUR_SEARCH_RADIUS of
    the route's full geometry, instead of one query per sample point.
    Drops query count from O(distance / SAMPLE_INTERVAL_MILES) to 1.
    """
    with profile_step("db_all_corridor_stations", distance_miles=round(distance_miles, 2)):
        route_line = LineString(coordinates, srid=4326)
        qs = list(
            FuelStationModel.objects
            .filter(location__distance_lte=(route_line, D(mi=DETOUR_SEARCH_RADIUS)))
            .annotate(dist_to_route=Distance('location', route_line, spheroid=True))
            .order_by('price')
        )
        log.info("corridor station count: %d", len(qs))
        return qs


def _candidate_stations_from_postgis(
    coordinates: list,
    distance_miles: float,
    cumdist: list[float],
) -> tuple[dict[float, list], list, np.ndarray]:
    """
    Returns:
      candidates   – mile → [FuelStationModel, ...] (up to CANDIDATES_PER_SAMPLE) Sorted by price max 5 for each 50 miles
      all_stations – full corridor list (for detour scanning in _build_dp_nodes) all FuelStationModel list
      station_arr  – (N, 2) float64 [lon, lat] array pre-built for NumPy ops long, latitude parsed from the FuelStationModel
    """
    with profile_step("build_candidate_stations", distance_miles=round(distance_miles, 2)):
        all_stations = _all_corridor_stations(coordinates, distance_miles)
        station_arr  = _station_coords_array(all_stations)  # coverts the FuelStationModel model to numpy x y coordinates 

        candidates: dict[float, list] = {}

        if not all_stations:
            log.warning(
                "No corridor stations found within %d mi of route (%.0f mi)",
                DETOUR_SEARCH_RADIUS,
                distance_miles,
            )
            return candidates, all_stations, station_arr

        mile = 0.0
        while mile <= distance_miles:
            route_pt = _point_at_mile_indexed(coordinates, cumdist, mile)
            query    = np.array([route_pt[0], route_pt[1]], dtype=np.float64)

            dists = _haversine_miles_vec(query, station_arr)  # (N,)

            # Boolean mask → indices within SEARCH_RADIUS_MILES
            mask    = dists <= SEARCH_RADIUS_MILES
            indices = np.where(mask)[0]

            if indices.size > 0:
                # Sort by price (already ordered by DB, but mask may reorder)
                # Use argsort on the price-ordered subset for stability
                nearby = sorted(
                    (all_stations[i] for i in indices), # Only getting the ones which is greater than SEARCH_RADIUS_MILES
                    key=lambda s: s.price,
                )[:CANDIDATES_PER_SAMPLE] 
                candidates[round(mile, 1)] = nearby

            mile += SAMPLE_INTERVAL_MILES

        return candidates, all_stations, station_arr
    
# ─────────────────────────────────────────────────────────────────────────────
# Detour candidates — vectorized
# ─────────────────────────────────────────────────────────────────────────────
# Original: O(N) Python generator with per-station attribute access + two
# comparisons.  New: one vectorized haversine call, two NumPy boolean masks,
# then a tiny Python sort on the (usually <30) survivors.
# The exclude_ids check stays in Python — frozenset lookup is O(1) and the
# set is small, so it's not worth moving into NumPy.

def _detour_candidates_near(
    lon: float,
    lat: float,
    exclude_ids: frozenset,
    all_stations: list,
    station_arr: np.ndarray,
) -> list:
    """
    Return up to 15 non-excluded stations within DETOUR_SEARCH_RADIUS miles,
    sorted by price ascending.
    """
    query = np.array([lon, lat], dtype=np.float64)
    dists = _haversine_miles_vec(query, station_arr)   # (N,)

    mask    = dists <= DETOUR_SEARCH_RADIUS
    indices = np.where(mask)[0]

    return sorted(
        (
            all_stations[i]
            for i in indices
            if all_stations[i].opis_id not in exclude_ids
        ),
        key=lambda s: s.price,
    )[:15]


# ─────────────────────────────────────────────────────────────────────────────
# DP node
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DPNode:
    node_id:          str
    route_mile:       float
    point:            list
    station:          Optional[FuelStationModel]
    price:            float
    is_detour:        bool  = False
    detour_miles:     float = 0.0
    detour_fuel_cost: float = 0.0





def _safety_lookahead_indices(j: int, N: int) -> list[int]:
    """
    Returns node indices for safety-fill candidates.
    Always includes j+1 (keep-moving guarantee) and N-1 (__end__) so the DP
    can generate a fill-to-destination candidate even when intermediate nodes
    exist between j and the end.
    """
    indices = [j + 1] if j + 1 < N else []
    if N - 1 not in indices and N - 1 > j:
        indices.append(N - 1)
    return indices


def _nearest_point_on_simplified_coords(
    s_lon: float,
    s_lat: float,
    coordinates: list,
) -> tuple[list[float], float]:
    """
    Returns ([lon, lat], cumulative_mile) of the nearest point on the
    simplified polyline to station at (s_lon, s_lat).
 
    We don't need the cumulative mile for the anchor itself, but returning
    it lets _build_dp_nodes use it as the node's route_mile so the DP
    ordering is also correct (previously, route_mile was the sample-mile
    bucket, not the true nearest mile).
    """
    best_dist  = float("inf")
    best_point = coordinates[0]
    best_mile  = 0.0
    cum        = 0.0
 
    for i in range(len(coordinates) - 1):
        ax, ay = coordinates[i][0],     coordinates[i][1]
        bx, by = coordinates[i + 1][0], coordinates[i + 1][1]
        dx, dy = bx - ax, by - ay
        sq     = dx * dx + dy * dy
 
        if sq < 1e-14:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((s_lon - ax) * dx + (s_lat - ay) * dy) / sq))
 
        nx, ny = ax + t * dx, ay + t * dy
        d      = _haversine_miles((s_lon, s_lat), (nx, ny))
 
        seg_len = _haversine_miles((ax, ay), (bx, by))
 
        if d < best_dist:
            best_dist  = d
            best_point = [round(nx, 6), round(ny, 6)]
            best_mile  = cum + t * seg_len
 
        cum += seg_len
 
    return best_point, round(best_mile, 2)

def _polygon_cache_key(raw_key: str) -> str:
    return f"{raw_key}:polygon:{POLYGON_CACHE_VERSION}"
 
 
def _build_corridor_polygon(coordinates: list):
    """
    Buffer the route LineString by CORRIDOR_BUFFER_DEGREES to produce a
    corridor polygon.  Returns a Shapely Polygon.
 
    Uses Shapely's simplify after buffering to keep the polygon compact —
    the buffer of a 28k-point line produces ~56k boundary points which is
    slow to serialize.  After simplification it's typically ~800 points,
    fast to cache and fast to query.
    """
    line = ShapelyLineString(coordinates)
    polygon = line.buffer(
        CORRIDOR_BUFFER_DEGREES,
        cap_style=2,    # flat ends
        join_style=2,   # mitre joins
    )
    # Simplify boundary — epsilon same as RDP so corridor fidelity matches
    return polygon.simplify(RDP_EPSILON, preserve_topology=True)
 
 
def _cache_get_polygon(poly_key: str):
    """Returns a Shapely Polygon or None."""
    if poly_key in _in_memory_route_cache:
        return _in_memory_route_cache[poly_key]
    blob = cache.get(poly_key)
    if blob is None:
        return None
    polygon = shapely_wkb.loads(blob)
    _in_memory_route_cache[poly_key] = polygon
    return polygon
 
 
def _cache_set_polygon(poly_key: str, polygon) -> None:
    _in_memory_route_cache[poly_key] = polygon
    blob = shapely_wkb.dumps(polygon)
    cache.set(poly_key, blob, timeout=ROUTE_CACHE_TTL)
 
 
def get_corridor_polygon(raw_key: str, coordinates: list):
    """
    Returns the corridor polygon for this route, building and caching it
    if not already stored.
 
    raw_key: the same key used for the raw route (_route_cache_key output).
    coordinates: simplified coords from the opt cache (fewer points = faster buffer).
    """
    poly_key = _polygon_cache_key(raw_key)
    polygon  = _cache_get_polygon(poly_key)
    if polygon is not None:
        return polygon
 
    polygon = _build_corridor_polygon(coordinates)
    _cache_set_polygon(poly_key, polygon)
    log.info(
        "corridor polygon built and cached: %d boundary pts",
        len(polygon.exterior.coords),
    )
    return polygon
 
 
def station_in_corridor(polygon, lon: float, lat: float) -> bool:
    """
    Returns True if the station at (lon, lat) is inside the corridor polygon.
    Uses Shapely PreparedGeometry — effectively O(1) per point query once
    the polygon is loaded from cache.
    """
    return polygon.contains(ShapelyPoint(lon, lat))
 
@profiled("build_dp_nodes")
def _build_dp_nodes(
        coordinates: list,
        distance_miles: float,
        candidate_map: dict,
        all_stations: list,
        station_arr: np.ndarray,
        start_fuel: float,
        cumdist: list[float],
        corridor_polygon,           # Shapely Polygon — passed from select_fuel_stops
    ) -> list[DPNode]:

        assumed_fill    = TANK_CAPACITY / 2
        lookahead_miles = MAX_RANGE_MILES / 2
        sorted_miles    = sorted(candidate_map.keys())

        # ── Helper: compute detour cost fields for a station outside the corridor ──
        def _detour_entry(db_station, nearest_pt, nearest_mile, dist_to_nearest) -> dict:
            """Return a candidate entry dict for a station that requires a detour."""
            price           = float(db_station.price)
            one_way_road    = _estimate_road_miles(db_station.dist_to_route.mi)
            round_trip_road = one_way_road * 2
            detour_fuel_cost = (round_trip_road / MILES_PER_GALLON) * price
            return {
                'min_dist':         dist_to_nearest,
                'sample_mile':      nearest_mile,
                'route_pt':         nearest_pt,
                'db_station':       db_station,
                'is_detour':        True,
                'detour_miles':     round(round_trip_road, 2),
                'detour_fuel_cost': round(detour_fuel_cost, 2),
            }

        # ── Helper: build a no-detour entry for a station inside the corridor ──────
        def _corridor_entry(db_station, nearest_pt, nearest_mile, dist_to_nearest) -> dict:
            """Return a candidate entry dict for a station that needs no detour."""
            return {
                'min_dist':         dist_to_nearest,
                'sample_mile':      nearest_mile,
                'route_pt':         nearest_pt,
                'db_station':       db_station,
                'is_detour':        False,
                'detour_miles':     0.0,
                'detour_fuel_cost': 0.0,
            }

        # ── Helper: upsert into `best` keeping the closest snap-point per station ──
        def _keep_closest(best: dict, opis_id: str, entry: dict) -> None:
            if opis_id not in best or entry['min_dist'] < best[opis_id]['min_dist']:
                best[opis_id] = entry

        # ── Helper: nearest route snap for a station (lon, lat) ───────────────────
        def _snap(s_lon, s_lat):
            nearest_pt, nearest_mile = _nearest_point_on_simplified_coords(s_lon, s_lat, coordinates)
            dist = _haversine_miles((s_lon, s_lat), (nearest_pt[0], nearest_pt[1]))
            return nearest_pt, nearest_mile, dist

        def _effective_price(db_station) -> float:
            """Corridor stations cost face value; detour stations add per-gallon detour cost."""
            price = float(db_station.price)
            if station_in_corridor(corridor_polygon, db_station.location.x, db_station.location.y):
                return price
            one_way_road = _estimate_road_miles(db_station.dist_to_route.mi)
            return price + (one_way_road * 2 / MILES_PER_GALLON) * price / assumed_fill

        # ── Build best-snap dict in a single pass ─────────────────────────────────
        # A sliding deque holds (mile, station) pairs within the lookahead window so
        # the cheapest effective price ahead is O(1) to query rather than O(n) per mile.
        best: dict[str, dict] = {}
        seen_detour_ids: set[str] = set()   # avoid re-snapping detour candidates

        window: deque[tuple[float, Any]] = deque()   # (mile, db_station) within lookahead
        right = 0  # index into sorted_miles for the window's leading edge

        for i, mile in enumerate(sorted_miles):
            # Advance window right edge to include all stations within lookahead
            while right < len(sorted_miles) and sorted_miles[right] <= mile + lookahead_miles:
                for s in candidate_map[sorted_miles[right]]:
                    window.append((sorted_miles[right], s))
                right += 1

            # Evict stations that have fallen behind the current mile
            while window and window[0][0] < mile:
                window.popleft()

            # Cheapest effective price in the lookahead window — O(window size)
            # (could maintain a running min-heap here for O(log n) if window is large)
            best_reachable_price = min(
                (_effective_price(s) for _, s in window),
                default=float('inf')
            )

            route_pt = _point_at_mile_indexed(coordinates, cumdist, mile)

            # On-route candidates — always kept regardless of price
            for db_station in candidate_map[mile]:
                s_lon, s_lat = db_station.location.x, db_station.location.y
                nearest_pt, nearest_mile, dist = _snap(s_lon, s_lat)
                in_corridor = station_in_corridor(corridor_polygon, s_lon, s_lat)
                entry = (
                    _corridor_entry(db_station, nearest_pt, nearest_mile, dist)
                    if in_corridor else
                    _detour_entry(db_station, nearest_pt, nearest_mile, dist)
                )
                _keep_closest(best, db_station.opis_id, entry)

            # Detour sweep — gated on price and distance, deduped by opis_id
            for db_station in _detour_candidates_near(
                route_pt[0], route_pt[1], frozenset(), all_stations, station_arr
            ):
                opis_id = db_station.opis_id
                if opis_id in seen_detour_ids:
                    continue

                s_lon, s_lat = db_station.location.x, db_station.location.y
                in_corridor  = station_in_corridor(corridor_polygon, s_lon, s_lat)

                if in_corridor:
                    nearest_pt, nearest_mile, dist = _snap(s_lon, s_lat)
                    _keep_closest(best, opis_id, _corridor_entry(db_station, nearest_pt, nearest_mile, dist))
                    seen_detour_ids.add(opis_id)
                    continue

                if db_station.dist_to_route.mi > MAX_DETOUR_ONE_WAY_MILES:
                    seen_detour_ids.add(opis_id)
                    continue

                if _effective_price(db_station) >= best_reachable_price:
                    continue   # don't add to seen — price floor may drop at a later mile

                nearest_pt, nearest_mile, dist = _snap(s_lon, s_lat)
                _keep_closest(best, opis_id, _detour_entry(db_station, nearest_pt, nearest_mile, dist))
                seen_detour_ids.add(opis_id)

        # ── Materialise nodes ──────────────────────────────────────────────────────
        nodes = [DPNode(node_id='__start__', route_mile=0.0, point=list(coordinates[0]), station=None, price=0.0)]

        for opis_id, e in best.items():
            db_station = e['db_station']
            nodes.append(DPNode(
                node_id=opis_id,
                route_mile=e['sample_mile'],
                point=e['route_pt'],
                station=db_station,
                price=float(db_station.price),
                is_detour=e['is_detour'],
                detour_miles=e['detour_miles'],
                detour_fuel_cost=e['detour_fuel_cost'],
            ))

        nodes.append(DPNode(node_id='__end__', route_mile=distance_miles, point=list(coordinates[-1]), station=None, price=0.0))
        nodes.sort(key=lambda n: n.route_mile)
        return nodes
    
    
# ─────────────────────────────────────────────────────────────────────────────
# Dijkstra / DP 
# ─────────────────────────────────────────────────────────────────────────────

def _snap_fuel(gallons: float, mode: str = 'nearest') -> float:
    """
    Round fuel to FUEL_STEP_GALLONS increments.

    mode='nearest' — general bucketing (default; used for DP state keys and
                     exact quantities like TANK_CAPACITY)
    mode='ceil'    — for fuel CONSUMED / REQUIRED — never underestimate need
    mode='floor'   — for fuel REMAINING after a leg — never overestimate range

    The directional modes guard against the discretization stranding a driver:
    overestimating consumption is safe (may stop slightly earlier than needed),
    but underestimating range could leave the tank empty before the next stop.
    """
    steps = gallons / FUEL_STEP_GALLONS
    if mode == 'ceil':
        steps = math.ceil(steps - 1e-9)
    elif mode == 'floor':
        steps = math.floor(steps + 1e-9)
    else:
        steps = round(steps)
    return round(steps * FUEL_STEP_GALLONS, 1)

@profiled("dp_optimal_stops")
def _dp_optimal_stops(
    nodes: list[DPNode],
    start_fuel: float,
    distance_miles: float,
) -> list[dict]:
    import heapq

    N   = len(nodes)
    INF = float('inf')

    # Pre-snap route miles for faster comparisons (avoid repeated float attrs)
    route_miles = [n.route_mile for n in nodes]

    dist: dict[tuple, float] = {}
    pred: dict[tuple, tuple] = {}

    start_f     = _snap_fuel(start_fuel)
    start_state = (0, start_f)
    dist[start_state] = 0.0
    heap = [(0.0, 0, start_f)]
    end_idx = N - 1

    # Pre-compute snapped fuel costs per-gallon for all fuel levels.
    # Avoids calling _snap_fuel in the hot path for the most common fills.
    # _snap_fuel(x, 'ceil') and 'floor' are still needed for fuel_consumed/fuel_after.

    while heap:
        cost, i, f = heapq.heappop(heap)

        if cost > dist.get((i, f), INF):
            continue

        if i == end_idx:
            break

        node_i          = nodes[i]
        mile_i          = route_miles[i]
        max_reach_miles = mile_i + f * MILES_PER_GALLON

        for j in range(i + 1, N):
            node_j        = nodes[j]
            mile_j        = route_miles[j]
            route_segment = mile_j - mile_i

            if mile_j > max_reach_miles:
                break

            total_drive   = route_segment + node_j.detour_miles
            fuel_consumed = _snap_fuel(total_drive / MILES_PER_GALLON, mode='ceil')

            if fuel_consumed > f:
                continue

            fuel_after = _snap_fuel(f - fuel_consumed, mode='floor')

            # ── End node — no purchase ────────────────────────────────────────
            if node_j.node_id == '__end__':
                state = (j, fuel_after)
                if cost < dist.get(state, INF):
                    dist[state] = cost
                    pred[state] = (i, f, None)
                    heapq.heappush(heap, (cost, j, fuel_after))
                continue

            max_fill = _snap_fuel(TANK_CAPACITY - fuel_after, mode='floor')

            if max_fill == 0.0:
                # Tank is full — only option is pass-through (0 gallons)
                fill_candidates: set[float] = {0.0}
            else:
               
                # to ensure we never strand the driver even if 0.0 < that amount < max_fill.
                fill_candidates = {0.0, max_fill}
                # destination — the fill amount for that path was never generated.
                for lookahead_idx in _safety_lookahead_indices(j, N):
                    node_la   = nodes[lookahead_idx]
                    miles_la  = (route_miles[lookahead_idx] - mile_j) + node_la.detour_miles
                    fuel_la   = _snap_fuel(miles_la / MILES_PER_GALLON, mode='ceil')
                    shortfall = max(0.0, fuel_la - fuel_after)
                    fill_candidates.add(_snap_fuel(min(shortfall, max_fill), mode='ceil'))
            for gallons_bought in fill_candidates:
                new_f     = _snap_fuel(fuel_after + gallons_bought)
                edge_cost = gallons_bought * node_j.price + node_j.detour_fuel_cost
                new_cost  = cost + edge_cost
                state     = (j, new_f)

                if new_cost < dist.get(state, INF):
                    dist[state] = new_cost
                    pred[state] = (
                        i, f,
                        _make_stop_record(
                            node_j, fuel_consumed, gallons_bought, i,
                            miles_from_prev=total_drive,
                            total_distance_miles=distance_miles,
                        )
                    )
                    heapq.heappush(heap, (new_cost, j, new_f))


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



def _make_stop_record(
    node: DPNode,
    fuel_consumed_to_reach: float,
    gallons_bought: float,
    prev_node_idx: int,
    miles_from_prev: float,
    total_distance_miles: float = 0.0,   # thread this in from _dp_optimal_stops
) -> dict:
    lon, lat       = node.point
    station_lon    = node.station.location.x
    station_lat    = node.station.location.y
    gallons_bought = round(gallons_bought, 4)
    fuel_cost      = round(gallons_bought * node.price + node.detour_fuel_cost, 2)

    stop = {
        'sequence':           0,
        'distance_traveled':  round(node.route_mile, 2),
        'miles_remaining':    round(max(total_distance_miles - node.route_mile, 0.0), 2),
        'miles_from_prev':    round(miles_from_prev, 2),
        'gallons_to_fill':    round(gallons_bought, 4),
        'fuel_cost':          fuel_cost,
        'map_marker':         {'lat': station_lat, 'lng': station_lon},
        'route_anchor':       {'lat': lat, 'lng': lon},
        'is_detour':          node.is_detour,
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

   
# ─────────────────────────────────────────────────────────────────────────────
# select_fuel_stops — cumdist now comes from the opt cache, not recomputed
# ─────────────────────────────────────────────────────────────────────────────
def select_fuel_stops(
    route: dict,
    optimizer_route: dict,
    start: dict,
    finish: dict,
    start_mode: str = START_MODE_NEAREST,
    start_fuel: float = START_FUEL_FIXED_GALLONS,
    stop_analysis: bool = False,
) -> tuple[list, int, list, bool]:
    with profile_step("select_fuel_stops", start_mode=start_mode):
        distance = route['distance_miles']
        coords   = optimizer_route['coordinates']
        extra_api = 0

        with profile_step("build_cumulative_distances"):
            cumdist = optimizer_route.get('cumdist') or _build_cumulative_distances(coords)

        log.info("coordinate count: %d", len(coords))

        if start_mode == START_MODE_NEAREST:
            first_station = _nearest_station(start['lng'], start['lat'])
            if first_station is None:
                prefix_stop = None
            else:
                start_fuel = TANK_CAPACITY
                s_lon = first_station.location.x
                s_lat = first_station.location.y
                haversine_to_first = _haversine_miles(
                    (start['lng'], start['lat']), (s_lon, s_lat)
                )
                road_to_first     = _estimate_road_miles(haversine_to_first)
                gallons_for_first = TANK_CAPACITY
                prefix_stop = {
                    'sequence':          1,
                    'distance_traveled': 0.0,
                    'miles_remaining':   round(distance, 2),
                    'miles_from_prev':   round(road_to_first, 2),
                    'gallons_to_fill':   round(gallons_for_first, 4),
                    'fuel_cost':         round(gallons_for_first * float(first_station.price), 2),
                    'map_marker':        {'lat': s_lat, 'lng': s_lon},
                    'is_detour':         False,
                    'note':              'nearest station at trip start',
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
            prefix_stop = None

        candidate_map, all_stations, station_arr = _candidate_stations_from_postgis(
            coords, distance, cumdist
        )

        raw_key          = _route_cache_key(start, finish)
        corridor_polygon = get_corridor_polygon(raw_key, coords)

        dp_nodes = _build_dp_nodes(
            coords, distance, candidate_map, all_stations, station_arr, start_fuel, cumdist,
            corridor_polygon,
        )

        stops = _dp_optimal_stops(dp_nodes, start_fuel, distance_miles=distance)

        log.info(
            "start_mode=%s  start_fuel=%.2f gal  max_first_stop_mile=%.0f  first_stop_mile=%s",
            start_mode,
            start_fuel,
            start_fuel * MILES_PER_GALLON,
            stops[0]['distance_traveled'] if stops else 'NO STOPS',
        )

        if prefix_stop is not None:
            for stop in stops:
                stop['sequence'] += 1
            stops.insert(0, prefix_stop)

        return stops, extra_api, dp_nodes, stop_analysis
    
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


@profiled("plan_trip")
def plan_trip(
    start_location,
    finish_location,
    start_mode: str = START_MODE_NEAREST,
    start_fuel_gallons: float | None = None,
    stop_analysis: bool = False,    
):
    start,  start_gc  = geocode_location(start_location)
    finish, finish_gc = geocode_location(finish_location)

    if not _is_usa(start) or not _is_usa(finish):
        raise RouteError('Start and finish must both be within the USA.')

    if start_mode == START_MODE_PARTIAL:
        opening_fuel = start_fuel_gallons if start_fuel_gallons is not None \
                       else START_FUEL_FIXED_GALLONS
    else:
        opening_fuel = TANK_CAPACITY

    route           = get_route(start, finish)
    optimizer_route = get_route_for_optimizer(start, finish)

    stops, extra_gc, debug_nodes, stop_analysis = select_fuel_stops(
        route,
        optimizer_route,
        start,
        finish,
        start_mode=start_mode,
        start_fuel=opening_fuel,
        stop_analysis=stop_analysis,
    )
    
    optimal_breakdown_items = [
       {
        'label':   f'Stop {s["sequence"]}',
        'cost':    float(s['fuel_cost']),
        'name': s.get('station', {}).get('name', ''),
        }
        for s in stops
    ]
    from core.stop_analysis import _build_cost_breakdown
    optimal_breakdown = _build_cost_breakdown(optimal_breakdown_items)
    

    total_cost   = round(sum(s['fuel_cost'] for s in stops), 2)
    detour_stops = [s for s in stops if s.get('is_detour')]
    detour_count = len(detour_stops)
    detour_miles = round(sum(s['detour']['round_trip_road_miles'] for s in detour_stops), 2)

    response = {
        'start':  start,
        'finish': finish,
        'vehicle': {
            'max_range_miles':   MAX_RANGE_MILES,
            'miles_per_gallon':  MILES_PER_GALLON,
            'tank_capacity_gal': TANK_CAPACITY,
        },
        'start_config': {
            'mode':               start_mode,
            'start_fuel_gallons': opening_fuel,
            'max_range_miles':    round(opening_fuel * MILES_PER_GALLON, 1),
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
        'total_fuel_cost': total_cost,
        'total_fuel_cost_breakdown': optimal_breakdown,
        'detour_summary': {
            'detours_taken':            detour_count,
            'total_detour_road_miles':  detour_miles,
            'road_factor_expected':     ROAD_FACTOR_EXPECTED,
            'road_factor_conservative': ROAD_FACTOR_CONSERVATIVE,
            'road_factor_blend_weight': ROAD_FACTOR_WEIGHT,
            'effective_road_factor':    round(
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

    # Debug API call disabled.
    # from core.stop_analysis import build_debug_station_records
    #
    # # debug_gas_stations only included when ?stop_analysis=true
    # if stop_analysis:
    #     response['debug_gas_stations'] = build_debug_station_records(
    #         debug_nodes,
    #         stops,
    #         total_cost,
    #         start_fuel=opening_fuel,
    #         total_distance_miles=route['distance_miles'],
    #         stop_analysis=True,
    #     )

    return response
