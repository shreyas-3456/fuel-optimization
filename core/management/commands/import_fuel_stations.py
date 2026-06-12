import csv
import difflib
import json
import os
import re
import tempfile
import time

import requests
from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from core.models import FuelStation

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_HEADERS = {"User-Agent": "spotterAi-fuel-importer/4.0 (admin@spotter.ai)"}
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
CACHE_FILE = "geocode_cache.json"

_city_coords_cache: dict = {}


def load_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE) as f:
            raw = json.load(f)
        out = {}
        for k, v in raw.items():
            if v == "NOT_FOUND":
                out[k] = v
            elif isinstance(v, list) and len(v) == 2:
                out[k] = tuple(v)
        return out
    except Exception:
        return {}


def save_cache(cache: dict):
    raw = {}
    for k, v in cache.items():
        if v == "NOT_FOUND":
            raw[k] = v
        else:
            raw[k] = list(v)
    dir_name = os.path.dirname(os.path.abspath(CACHE_FILE))
    with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, suffix=".tmp") as tmp:
        json.dump(raw, tmp)
        os.replace(tmp.name, CACHE_FILE)


def normalize_brand(name: str) -> str:
    """Strips branch numbers, STOP/CENTER keywords to isolate the core brand name."""
    n = (name or "").upper()
    n = re.sub(r'\s*#\d+.*$', '', n)
    n = re.sub(r'(?i)\b(TRAVEL CENTER|TRAVEL CENTERS|TRUCK STOP|TRUCK PLAZA|TRAVEL PLAZA|FOODS?|STOPPING CENTER|STOP|INC|LLC|EXPRESS|STORE|STATION)\b', '', n)
    n = n.replace("'", "").replace('"', '').replace('-', ' ').strip()
    return n


def build_geocode_queries(name: str, address: str, city: str, state: str) -> list:
    queries = []
    name = (name or "").strip()
    address = (address or "").strip()
    city = (city or "").strip()
    state = state.upper().strip()

    # 1. Street number + road only
    num_match = re.search(r'^(\d{2,})\s+(.*)', address)
    if num_match:
        num = num_match.group(1)
        road = num_match.group(2).strip()
        road = re.sub(r'(?i)\b(STE|SUITE|UNIT|BLDG|APT)\b.*', '', road).strip()
        if road:
            queries.append(f"{num} {road}, {city}, {state}")

    # 2. Highway/Exit Intersection
    if " & " in address or address.startswith("I-") or address.startswith("US-") or address.startswith("SR-"):
        hwy_intersect = address.replace(" & ", " and ")
        hwy_intersect = re.sub(r',?\s*EXIT\s*\d+[A-Z]?', '', hwy_intersect, flags=re.I).strip()
        if "/" in hwy_intersect:
            hwy_intersect = hwy_intersect.split("/")[0].strip()
        if hwy_intersect:
            queries.append(f"{hwy_intersect}, {city}, {state}")

    # 3. Cleaned standard address
    clean = address
    if not address.startswith("I-"):
        clean = re.sub(r'I-\d+[^,]*', '', clean, flags=re.I)
        clean = re.sub(r'EXIT\s*\d+[A-Z]?', '', clean, flags=re.I)
        clean = re.sub(r'&.*$', '', clean)
        clean = re.sub(r'\s+', ' ', clean).strip(' ,')
        if clean and len(clean) > 4:
            queries.append(f"{clean}, {city}, {state}")

    # 4. Name + city
    if name:
        queries.append(f"{name}, {city}, {state}")

    # 5. Full raw address
    if address:
        queries.append(f"{address}, {city}, {state}")

    seen = set()
    return [q for q in queries if not (q in seen or seen.add(q))]


def get_city_coords(city: str, state: str) -> tuple | None:
    """
    Returns (lat, lon) for the center of the municipality via Nominatim.
    Uses free-form text query `q` because it perfectly bypasses strict structural bugs.
    """
    key = f"{city}|{state}"
    if key in _city_coords_cache:
        val = _city_coords_cache[key]
        return None if val == "NOT_FOUND" else val

    time.sleep(1.1)  # Strictly respect API limits
    try:
        r = requests.get(
            NOMINATIM_URL,
            params={
                "q": f"{city}, {state}",
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "us,ca"  # Support Canadian locations like AB, ON
            },
            headers=NOMINATIM_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        hits = r.json()
        if hits:
            coords = (float(hits[0]["lat"]), float(hits[0]["lon"]))
            _city_coords_cache[key] = coords
            return coords
        else:
            # 200 OK, but city doesn't exist at all.
            _city_coords_cache[key] = "NOT_FOUND"
            return None
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code in (429, 403):
            time.sleep(5)  # Back off if hit with temporary rate block
    except Exception:
        pass

    return None


def overpass_by_name_in_city(name: str, city: str, state: str, stdout) -> tuple | None:
    """
    Query Overpass for ALL fuel/truck-stops within a 15km radius of the city center.
    Uses fuzzy matching against normalized core brand names.
    """
    coords = get_city_coords(city, state)
    if not coords:
        return None

    c_lat, c_lon = coords
    target_norm = normalize_brand(name)

    ql = f"""
[out:json][timeout:25];
(
  node["amenity"~"fuel|truck_stop"](around:15000, {c_lat}, {c_lon});
  way["amenity"~"fuel|truck_stop"](around:15000, {c_lat}, {c_lon});
  node["highway"="services"](around:15000, {c_lat}, {c_lon});
  way["highway"="services"](around:15000, {c_lat}, {c_lon});
);
out center;
"""
    time.sleep(1.2)
    try:
        r = requests.post(
            OVERPASS_URL,
            data={"data": ql},
            headers=NOMINATIM_HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        elements = r.json().get("elements", [])
        
        best_match = None
        highest_ratio = 0.0

        for el in elements:
            tags = el.get("tags", {})
            osm_name = tags.get("name", "")
            osm_brand = tags.get("brand", "")

            norm_osm_name = normalize_brand(osm_name)
            norm_osm_brand = normalize_brand(osm_brand)

            ratio_name = difflib.SequenceMatcher(None, target_norm, norm_osm_name).ratio()
            ratio_brand = difflib.SequenceMatcher(None, target_norm, norm_osm_brand).ratio()
            
            if target_norm and len(target_norm) > 2:
                if target_norm in norm_osm_name or target_norm in norm_osm_brand:
                    ratio_name = max(ratio_name, 0.85)

            max_ratio = max(ratio_name, ratio_brand)

            if max_ratio > highest_ratio:
                highest_ratio = max_ratio
                lat = el.get("lat") or el.get("center", {}).get("lat")
                lon = el.get("lon") or el.get("center", {}).get("lon")
                if lat and lon:
                    best_match = (float(lat), float(lon), osm_name or osm_brand, max_ratio)

        if best_match and best_match[3] >= 0.60:
            stdout.write(
                f"  [Overpass Radius OK] '{target_norm}' matched OSM '{best_match[2]}' "
                f"(Score: {best_match[3]:.2f}) -> ({best_match[0]:.5f}, {best_match[1]:.5f})"
            )
            return (best_match[0], best_match[1])

    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            time.sleep(5)
    except Exception:
        pass

    return None


def geocode_address(name: str, address: str, city: str, state: str, stdout) -> tuple | None:
    # Layer 1: Nominatim free-form queries (Intersections, names, streets)
    for query in build_geocode_queries(name, address, city, state):
        time.sleep(1.1)
        try:
            r = requests.get(
                NOMINATIM_URL,
                params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "us,ca"},
                headers=NOMINATIM_HEADERS,
                timeout=15,
            )
            r.raise_for_status()
            hits = r.json()
            if hits:
                lat, lon = float(hits[0]["lat"]), float(hits[0]["lon"])
                stdout.write(f"  [Nominatim] {query[:80]}")
                return (lat, lon)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in (429, 403):
                time.sleep(5)
            continue
        except Exception:
            continue

    # Layer 2: Overpass city-scoped fuzzy name search (15km radius)
    stdout.write(f"  [Nominatim failed] -> Overpass 15km Radius: {name} / {city}, {state}")
    coords = overpass_by_name_in_city(name, city, state, stdout)
    if coords:
        return coords

    # Layer 3: THE ULTIMATE FALLBACK. Do not fail, just use the City Center.
    city_coords = get_city_coords(city, state)
    if city_coords:
        stdout.write(f"  [Fallback] Used exact City Center for: {city}, {state}")
        return city_coords

    stdout.write(f"  [FATAL FAILED] Could not even find City bounds for: {city}, {state}")
    return None


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------
class Command(BaseCommand):
    help = "Import fuel stations with Nominatim + Overpass geocoding"

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str)
        parser.add_argument("--flush", action="store_true", default=False)

    def handle(self, *args, **options):
        if options["flush"]:
            with connection.cursor() as cur:
                cur.execute("DELETE FROM core_fuelstation;")
                cur.execute("DELETE FROM core_routedistance;")
            self.stdout.write(self.style.WARNING("DB flushed."))
            if os.path.exists(CACHE_FILE):
                os.remove(CACHE_FILE)
                self.stdout.write("Cache cleared.")

        with open(options["csv_path"], newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.stdout.write(f"Read {len(rows)} rows.")

        valid_rows = []
        for row in rows:
            opis_id = row.get("OPIS Truckstop ID", "").strip()
            name    = row.get("Truckstop Name", "").strip()
            address = row.get("Address", "").strip()
            city    = row.get("City", "").strip()
            state   = row.get("State", "").strip().upper()
            try:
                price = float(row.get("Retail Price", 0))
            except (ValueError, TypeError):
                continue
            if not opis_id or not city or not state or price <= 0:
                continue
            valid_rows.append((opis_id, name, address, city, state, price, row))

        self.stdout.write(f"{len(valid_rows)} valid rows.")

        cache = load_cache()
        addr_coords: dict = {}
        failed = 0
        total = len(valid_rows)

        for i, (opis_id, name, address, city, state, price, row) in enumerate(valid_rows):
            key = f"{name}|{address}|{city}|{state}"
            
            if key in cache:
                if cache[key] == "NOT_FOUND":
                    failed += 1
                else:
                    addr_coords[key] = cache[key]
            else:
                coords = geocode_address(name, address, city, state, self.stdout)
                if coords:
                    cache[key] = coords
                    addr_coords[key] = coords
                    save_cache(cache)
                else:
                    cache[key] = "NOT_FOUND"
                    save_cache(cache)
                    failed += 1

            if (i + 1) % 25 == 0 or i == total - 1:
                self.stdout.write(f"Progress: {i+1}/{total} | Failed: {failed}")

        inserted = updated = 0
        with transaction.atomic():
            for opis_id, name, address, city, state, price, row in valid_rows:
                key = f"{name}|{address}|{city}|{state}"
                latlon = addr_coords.get(key)
                if not latlon:
                    continue
                lat, lon = latlon
                _, created = FuelStation.objects.update_or_create(
                    opis_id=opis_id,
                    defaults={
                        "name": name,
                        "address": address,
                        "city": city,
                        "state": state,
                        "rack_id": row.get("Rack ID", "").strip(),
                        "price": price,
                        "location": Point(lon, lat, srid=4326),
                    },
                )
                if created:
                    inserted += 1
                else:
                    updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Inserted: {inserted} | Updated: {updated} | Failed: {failed}"
        ))