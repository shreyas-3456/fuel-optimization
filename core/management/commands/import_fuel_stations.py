# core/management/commands/import_fuel_stations.py
import csv
import io
import json
import time
import os
import random
import tempfile
import requests
from django.core.management.base import BaseCommand
from django.contrib.gis.geos import Point
from django.db import transaction
from core.models import FuelStation

NOMINATIM_URL     = 'https://nominatim.openstreetmap.org/search'
NOMINATIM_HEADERS = {'User-Agent': 'spotterAi-fuel-importer/1.0'}
CACHE_FILE        = 'geocode_cache.json'

# Max random offset in degrees (~500m at US latitudes)
JITTER = 0.05


# ---------------------------------------------------------------------------
# Cache helpers  (keyed by "city|state")
# ---------------------------------------------------------------------------

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                raw = json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  WARNING: Cache file corrupt ({e}). Starting fresh.")
            return {}
        cleaned = {k: tuple(v) for k, v in raw.items() if v is not None}
        print(f"  Loaded {len(cleaned)} cities from {CACHE_FILE}")
        return cleaned
    print("  No cache file found, starting fresh.")
    return {}


def save_cache(cache):
    raw      = {k: list(v) for k, v in cache.items() if v is not None}
    dir_name = os.path.dirname(os.path.abspath(CACHE_FILE))
    with tempfile.NamedTemporaryFile('w', dir=dir_name, delete=False, suffix='.tmp') as tmp:
        json.dump(raw, tmp)
        tmp_path = tmp.name
    os.replace(tmp_path, CACHE_FILE)


# ---------------------------------------------------------------------------
# Nominatim — one request per unique city
# ---------------------------------------------------------------------------

def geocode_cities(city_state_pairs, stdout):
    """
    city_state_pairs: set of (city, state) tuples
    Returns cache dict keyed by "city|state" -> (lat, lon)
    """
    cache  = load_cache()
    needed = [(c, s) for c, s in city_state_pairs if f"{c}|{s}" not in cache]
    stdout.write(f"  {len(cache)} cities cached, {len(needed)} to geocode via Nominatim...")

    for i, (city, state) in enumerate(needed):
        key    = f"{city}|{state}"
        query  = f"{city}, {state}, USA"
        result = None

        try:
            r = requests.get(
                NOMINATIM_URL,
                params={'q': query, 'format': 'json', 'limit': 1},
                headers=NOMINATIM_HEADERS,
                timeout=10,
            )
            r.raise_for_status()
            hits = r.json()
            if hits:
                result = (float(hits[0]['lat']), float(hits[0]['lon']))
        except Exception as e:
            stdout.write(f"  Nominatim error for '{query}': {e}")

        if result:
            cache[key] = result
            save_cache(cache)
        else:
            stdout.write(f"  Could not geocode {query} -- will skip.")

        time.sleep(1)   # Nominatim rate limit

        if (i + 1) % 50 == 0:
            stdout.write(f"  {i + 1}/{len(needed)} cities done...")

    stdout.write(f"  Geocoding done. {len(cache)} cities resolved.")
    return cache


# ---------------------------------------------------------------------------
# Jitter — spread stations within the same city
# ---------------------------------------------------------------------------

def jittered(lat, lon):
    """Return (lat, lon) with a small random offset so points don't stack."""
    return (
        lat + random.uniform(-JITTER, JITTER),
        lon + random.uniform(-JITTER, JITTER),
    )


# ---------------------------------------------------------------------------
# Django management command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = 'Import fuel stations from CSV into PostGIS (geocodes by city)'

    def add_arguments(self, parser):
        parser.add_argument('csv_path', type=str)

    def handle(self, *args, **options):
        csv_path = options['csv_path']

        with open(csv_path, newline='', encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))

        self.stdout.write(f"Read {len(rows)} rows from CSV.")

        # Step 1: parse valid rows
        valid_rows = []
        for row in rows:
            opis_id = row.get('OPIS Truckstop ID', '').strip()
            address = row.get('Address', '').strip()
            city    = row.get('City', '').strip()
            state   = row.get('State', '').strip().upper()
            try:
                price = float(row.get('Retail Price', ''))
            except (ValueError, TypeError):
                continue
            if not opis_id or not city or not state:
                continue
            valid_rows.append((opis_id, address, city, state, price, row))

        # Step 2: skip already-imported stations
        existing = set(FuelStation.objects.values_list('opis_id', flat=True))
        new_rows  = [r for r in valid_rows if r[0] not in existing]
        self.stdout.write(f"{len(existing)} already imported, {len(new_rows)} to add.")

        if not new_rows:
            self.stdout.write(self.style.SUCCESS("Nothing to import."))
            return

        # Step 3: geocode unique cities only
        city_state_pairs = {(city, state) for _, _, city, state, _, _ in new_rows}
        self.stdout.write(f"  {len(city_state_pairs)} unique cities to geocode.")
        city_coords = geocode_cities(city_state_pairs, self.stdout)

        # Step 4: bulk insert with per-station jitter
        to_create    = []
        failed_count = 0
        for opis_id, address, city, state, price, row in new_rows:
            key    = f"{city}|{state}"
            latlon = city_coords.get(key)
            if latlon is None:
                failed_count += 1
                continue
            lat, lon = jittered(*latlon)
            to_create.append(FuelStation(
                opis_id  = opis_id,
                name     = row.get('Truckstop Name', '').strip(),
                address  = address,
                city     = city,
                state    = state,
                rack_id  = row.get('Rack ID', '').strip(),
                price    = price,
                location = Point(lon, lat, srid=4326),
            ))

        with transaction.atomic():
            FuelStation.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)

        self.stdout.write(self.style.SUCCESS(
            f"Done. Inserted: {len(to_create)}, "
            f"Already in DB: {len(existing)}, "
            f"No coords (skipped): {failed_count}"
        ))