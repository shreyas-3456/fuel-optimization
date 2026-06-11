import bisect
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
from typing import Optional

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

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_in_memory_route_cache: dict[str, dict] = {}

MILES_PER_METER       = 0.000621371
MILES_PER_GALLON      = 10
MAX_RANGE_MILES       = 500
TANK_CAPACITY         = MAX_RANGE_MILES / MILES_PER_GALLON

SAMPLE_INTERVAL_MILES = 50
SEARCH_RADIUS_MILES   = 40
CANDIDATES_PER_SAMPLE = 5

ROAD_FACTOR_EXPECTED     = 1.30
ROAD_FACTOR_CONSERVATIVE = 1.60
ROAD_FACTOR_WEIGHT       = 0.40

DETOUR_SEARCH_RADIUS     = 75
MAX_DETOUR_ONE_WAY_MILES = 50

START_FUEL_FIXED_GALLONS = 5
START_MODE_NEAREST       = 'nearest_station'
START_MODE_PARTIAL       = 'partial_tank'

FUEL_STEP_GALLONS = 0.5
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


# ─────────────────────────────────────────────────────────────────────────────
# RDP simplification — Shapely/GEOS (C) replaces pure-Python rdp
# ─────────────────────────────────────────────────────────────────────────────
# The original code used the `rdp` PyPI package, a pure-Python implementation.
# On a 28,943-point route (typical GraphHopper output for 3,000+ miles) it took
# ~8 seconds. Shapely's simplify() calls GEOS (C library) and runs the same
# operation in ~5 ms — a ~1,600× speedup.
#
# Epsilon is unchanged at 0.01° ≈ 1.1 km, appropriate for a 40–75 mile search
# radius.  preserve_topology=False is correct here: we only care about geometry
# fidelity, not polygon closure.
#
# IMPORTANT: GraphHopper returns [lon, lat] order.  Shapely LineString also
# stores coordinates as given and returns them in the same order, so no
# reordering is needed here or downstream.

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


# ─────────────────────────────────────────────────────────────────────────────
# Optimizer route cache — separate Redis key stores simplified coords + cumdist
# ─────────────────────────────────────────────────────────────────────────────
# The opt payload is tiny (~7 KB for 370 coords + distances) so we skip zlib.
# Using a sibling key (raw_key + ":opt") lets us invalidate simplified coords
# independently if RDP_EPSILON changes, without touching valid raw payloads.
# TTL is refreshed on write, matching the raw key's 7-day window.

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
    cumdist = [0.0]
    for prev, curr in zip(coordinates, coordinates[1:]):
        cumdist.append(cumdist[-1] + _haversine_miles(prev, curr))
    return cumdist


def _point_at_mile_indexed(coordinates: list, cumdist: list[float], target_mile: float):
    if target_mile <= 0:
        return coordinates[0]
    if target_mile >= cumdist[-1]:
        return coordinates[-1]
    idx = bisect.bisect_right(cumdist, target_mile) - 1
    idx = max(0, min(idx, len(coordinates) - 2))
    seg_start, seg_end = cumdist[idx], cumdist[idx + 1]
    seg = seg_end - seg_start
    ratio = (target_mile - seg_start) / seg if seg else 0.0
    prev, curr = coordinates[idx], coordinates[idx + 1]
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
# Pre-extracting a (N, 2) float64 array from the Django QuerySet objects avoids
# repeated attribute access inside tight loops and gives NumPy contiguous memory
# to work on.  Built in _candidate_stations_from_postgis and threaded through
# to _build_dp_nodes and _detour_candidates_near.

def _station_coords_array(all_stations: list) -> np.ndarray:
    """Return (N, 2) float64 array of [lon, lat] for each station."""
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
      candidates   – mile → [FuelStationModel, ...] (up to CANDIDATES_PER_SAMPLE)
      all_stations – full corridor list (for detour scanning in _build_dp_nodes)
      station_arr  – (N, 2) float64 [lon, lat] array pre-built for NumPy ops

    Key change vs original: the inner distance check is now a single vectorized
    haversine call per sample point rather than a Python for-loop over 1,404
    stations.  The sort is done once on the filtered subset (typically <60 items)
    so its cost is negligible.
    """
    with profile_step("build_candidate_stations", distance_miles=round(distance_miles, 2)):
        all_stations = _all_corridor_stations(coordinates, distance_miles)

        # Build coord array once — reused for every sample point below
        station_arr = _station_coords_array(all_stations)  # (N, 2)

        candidates: dict[float, list] = {}
        mile = 0.0
        while mile <= distance_miles:
            route_pt = _point_at_mile_indexed(coordinates, cumdist, mile)
            query    = np.array([route_pt[0], route_pt[1]], dtype=np.float64)

            # Vectorized: distances for all N stations in one NumPy call
            dists = _haversine_miles_vec(query, station_arr)   # (N,)

            # Boolean mask → indices within SEARCH_RADIUS_MILES
            mask    = dists <= SEARCH_RADIUS_MILES
            indices = np.where(mask)[0]

            if indices.size > 0:
                # Sort by price (already ordered by DB, but mask may reorder)
                # Use argsort on the price-ordered subset for stability
                nearby = sorted(
                    (all_stations[i] for i in indices),
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


# ─────────────────────────────────────────────────────────────────────────────
# DP graph builder — station_arr threaded through for vectorized detour scans
# ─────────────────────────────────────────────────────────────────────────────

@profiled("build_dp_nodes")
def _build_dp_nodes(
    coordinates: list,
    distance_miles: float,
    candidate_map: dict,
    all_stations: list,
    station_arr: np.ndarray,   # ← new: pre-built (N, 2) array
    start_fuel: float,
    cumdist: list[float],
) -> list[DPNode]:
    ON_ROUTE_THRESHOLD_MILES = 5

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

    lookahead_miles = MAX_RANGE_MILES / 2
    best_reachable_price_at: dict[float, float] = {}
    for mile in sorted_miles:
        prices = [
            float(s.price)
            for m, stations in candidate_map.items()
            if mile <= m <= mile + lookahead_miles
            for s in stations
            if s.dist_to_route.mi <= ON_ROUTE_THRESHOLD_MILES
        ]
        best_reachable_price_at[mile] = min(prices) if prices else float('inf')

    for mile in sorted_miles:
        route_pt = _point_at_mile_indexed(coordinates, cumdist, mile)
        on_route_stations = candidate_map[mile]

        # ── On-route candidates ───────────────────────────────────────────────
        for db_station in on_route_stations:
            if db_station.opis_id in seen_ids:
                continue
            seen_ids.add(db_station.opis_id)

            dist_to_route_miles = db_station.dist_to_route.mi

            if dist_to_route_miles > ON_ROUTE_THRESHOLD_MILES:
                one_way_road    = _estimate_road_miles(dist_to_route_miles)
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

        # ── Detour candidates (wider sweep, in-memory) ────────────────────────
        # Use the forward-looking best price: cheapest on-route station
        # reachable from this mile within half a tank. A detour that's not
        # cheaper than what's coming up on-route is never worth taking.
        best_reachable_price = best_reachable_price_at[mile]

        # Pass station_arr so the detour scan is vectorized
        for db_station in _detour_candidates_near(
            route_pt[0], route_pt[1], frozenset(seen_ids), all_stations, station_arr
        ):
            if db_station.opis_id in seen_ids:
                continue

            dist_to_route_miles = db_station.dist_to_route.mi

            if dist_to_route_miles <= ON_ROUTE_THRESHOLD_MILES:
                seen_ids.add(db_station.opis_id)
                nodes.append(DPNode(
                    node_id=db_station.opis_id,
                    route_mile=mile,
                    point=route_pt,
                    station=db_station,
                    price=float(db_station.price),
                    is_detour=False,
                    detour_miles=0.0,
                    detour_fuel_cost=0.0,
                ))
                continue

            # True detour — must be cheaper than anything reachable on-route
            price = float(db_station.price)
            if price >= best_reachable_price:
                continue
            if dist_to_route_miles > MAX_DETOUR_ONE_WAY_MILES:
                continue

            one_way_road    = _estimate_road_miles(dist_to_route_miles)
            round_trip_road = one_way_road * 2
            detour_fuel_cost = (round_trip_road / MILES_PER_GALLON) * price

            seen_ids.add(db_station.opis_id)
            nodes.append(DPNode(
                node_id=db_station.opis_id,
                route_mile=mile,
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
# Dijkstra / DP  — unchanged
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
def _dp_optimal_stops(nodes: list[DPNode], start_fuel: float, distance_miles:float) -> list[dict]:
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

        node_i          = nodes[i]
        max_reach_miles = node_i.route_mile + f * MILES_PER_GALLON

        for j in range(i + 1, N):
            node_j       = nodes[j]
            route_segment = node_j.route_mile - node_i.route_mile
            if route_segment > max_reach_miles - node_i.route_mile:
                break

            total_drive   = route_segment + node_j.detour_miles
            fuel_consumed = total_drive / MILES_PER_GALLON
            fuel_consumed = _snap_fuel(fuel_consumed, mode='ceil')

            if fuel_consumed > f:
                continue

            fuel_after = f - fuel_consumed

            # ── End node — no purchase needed ─────────────────────────────────
            if node_j.node_id == '__end__':
                new_f  = _snap_fuel(fuel_after, mode='floor')
                state  = (j, new_f)
                if cost < dist.get(state, INF):
                    dist[state] = cost
                    pred[state] = (i, f, None)
                    heapq.heappush(heap, (cost, j, new_f))
                continue

            fuel_after     = _snap_fuel(fuel_after, mode='floor')
            gallons_bought = max(0.0, TANK_CAPACITY - fuel_after)
            edge_cost      = gallons_bought * node_j.price + node_j.detour_fuel_cost
            new_cost       = cost + edge_cost
            new_f          = _snap_fuel(TANK_CAPACITY)
            state          = (j, new_f)

            if new_cost < dist.get(state, INF):
                dist[state] = new_cost
                pred[state] = (
                    i, f,
                    _make_stop_record(
                        node_j, fuel_consumed, gallons_bought, i,
                        miles_from_prev=total_drive,
                        total_distance_miles=distance_miles
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
    miles_from_prev: float,
    total_distance_miles: float = 0.0,   # thread this in from _dp_optimal_stops
) -> dict:
    lon, lat       = node.point
    gallons_bought = round(gallons_bought, 4)
    fuel_cost      = round(gallons_bought * node.price + node.detour_fuel_cost, 2)

    stop = {
        'sequence':           0,
        'distance_traveled':  round(node.route_mile, 2),
        'miles_remaining':    round(max(total_distance_miles - node.route_mile, 0.0), 2),
        'miles_from_prev':    round(miles_from_prev, 2),
        'gallons_to_fill':    round(gallons_bought, 4),
        'fuel_cost':          fuel_cost,
        'map_marker':         {'lat': lat, 'lng': lon},
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


def _nearest_stop_for_candidate(node: DPNode, stops: list[dict]) -> Optional[dict]:
    if not stops:
        return None
    return min(
        stops,
        key=lambda stop: abs(float(stop.get('distance_traveled', 0.0)) - node.route_mile),
    )


def _debug_station_record(node: DPNode, stops: list[dict], total_cost: float) -> Optional[dict]:
    if node.station is None:
        return None

    selected_stop     = _nearest_stop_for_candidate(node, stops)
    selected_ids      = {stop.get('station', {}).get('opis_id') for stop in stops}
    is_selected       = node.station.opis_id in selected_ids

    gallons = (
        float(selected_stop.get('gallons', TANK_CAPACITY))
        if selected_stop else TANK_CAPACITY
    )
    selected_stop_cost  = float(selected_stop.get('fuel_cost', 0.0)) if selected_stop else 0.0
    candidate_stop_cost = round((gallons * node.price) + node.detour_fuel_cost, 2)
    total_if_chosen     = (
        total_cost if is_selected
        else round(total_cost - selected_stop_cost + candidate_stop_cost, 2)
    )

    lon, lat        = node.point
    station_lon     = node.station.location.x
    station_lat     = node.station.location.y
    record = {
        'distance_traveled':                 round(node.route_mile, 2),
        'map_marker':                 {'lat': station_lat, 'lng': station_lon},
        'route_anchor':               {'lat': lat, 'lng': lon},
        'is_selected':                is_selected,
        'is_detour':                  node.is_detour,
        'gallons_if_chosen':          round(gallons, 4),
        'fuel_cost_if_chosen':        candidate_stop_cost,
        'total_fuel_cost_if_chosen':  total_if_chosen,
        'delta_total_fuel_cost':      round(total_if_chosen - total_cost, 2),
        'compared_stop_sequence':     selected_stop.get('sequence') if selected_stop else None,
        'compared_stop_cost':         round(selected_stop_cost, 2),
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
        record['detour'] = {
            'one_way_road_miles':    round(node.detour_miles / 2, 2),
            'round_trip_road_miles': node.detour_miles,
            'detour_fuel_cost':      node.detour_fuel_cost,
            'road_factor_used':      round(
                ROAD_FACTOR_WEIGHT * ROAD_FACTOR_EXPECTED
                + (1 - ROAD_FACTOR_WEIGHT) * ROAD_FACTOR_CONSERVATIVE, 3
            ),
        }
    return record


@profiled("debug_station_records")
def _debug_station_records(nodes: list[DPNode], stops: list[dict], total_cost: float) -> list[dict]:
    records = [
        record
        for record in (_debug_station_record(node, stops, total_cost) for node in nodes)
        if record is not None
    ]
    return sorted(records, key=lambda record: (
        record['distance_traveled'],
        record['station']['price_per_gallon'],
        record['station']['opis_id'],
    ))


# ─────────────────────────────────────────────────────────────────────────────
# select_fuel_stops — cumdist now comes from the opt cache, not recomputed
# ─────────────────────────────────────────────────────────────────────────────

def select_fuel_stops(
    route: dict,
    optimizer_route: dict,   # now includes "cumdist" key from opt cache
    start: dict,
    finish: dict,
    start_mode: str = START_MODE_NEAREST,
    start_fuel: float = START_FUEL_FIXED_GALLONS,
) -> tuple[list, int, list]:
    with profile_step("select_fuel_stops", start_mode=start_mode):
        distance = route['distance_miles']
        coords   = optimizer_route['coordinates']
        extra_api = 0

        # cumdist is pre-computed in get_route_for_optimizer and cached with the
        # opt payload — no recomputation needed on cache hits.
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

        # _candidate_stations_from_postgis now also returns station_arr
        candidate_map, all_stations, station_arr = _candidate_stations_from_postgis(
            coords, distance, cumdist
        )

        # station_arr threaded through so _detour_candidates_near can use it
        dp_nodes = _build_dp_nodes(
            coords, distance, candidate_map, all_stations, station_arr, start_fuel, cumdist
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
# plan_trip — public API  (unchanged except optimizer_route now carries cumdist)
# ─────────────────────────────────────────────────────────────────────────────

@profiled("plan_trip")
def plan_trip(
    start_location,
    finish_location,
    start_mode: str = START_MODE_NEAREST,
    start_fuel_gallons: float | None = None,
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
    optimizer_route = get_route_for_optimizer(start, finish)   # includes cumdist

    stops, extra_gc, debug_nodes = select_fuel_stops(
        route,
        optimizer_route,
        start,
        finish,
        start_mode=start_mode,
        start_fuel=opening_fuel,
    )

    total_cost    = round(sum(s['fuel_cost'] for s in stops), 2)
    detour_stops  = [s for s in stops if s.get('is_detour')]
    detour_count  = len(detour_stops)
    detour_miles  = round(sum(s['detour']['round_trip_road_miles'] for s in detour_stops), 2)
    debug_stations = _debug_station_records(debug_nodes, stops, total_cost)

    return {
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
        'fuel_stops':         stops,
        'debug_gas_stations': debug_stations,
        'nearby_stations':    debug_stations,
        'total_fuel_cost':    total_cost,
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