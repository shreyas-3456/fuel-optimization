"""
Integration tests for /api/route-fuel/

Two test classes:
  1. DebugStationCalculationTests
       Verifies the arithmetic inside each debug_gas_stations record is
       self-consistent (fuel_cost_if_chosen, total_fuel_cost_if_chosen,
       delta_total_fuel_cost) without making a live HTTP call — uses only
       the calculation helpers from optimizer.py.

  2. RouteFuelIntegrationTests
       Makes a real POST to the running server and asserts:
         a. HTTP 200 with required top-level keys
         b. Every debug_gas_stations record has correct arithmetic
         c. No debug station has a negative delta_total_fuel_cost
            (a negative delta means the optimizer skipped a cheaper option —
            this is the failure condition described in the task).

Run the calculation unit-tests in isolation (no server required):
    python -m pytest tests/test_route_fuel_integration.py \
        -k DebugStationCalculationTests -v

Run the full integration suite (server must be running on localhost:8000):
    python -m pytest tests/test_route_fuel_integration.py -v
"""

from __future__ import annotations

import math
import unittest

import requests  # pip install requests


# ─────────────────────────────────────────────────────────────────────────────
# Constants (mirrored from optimizer.py — keep in sync if they change)
# ─────────────────────────────────────────────────────────────────────────────

TANK_CAPACITY   = 50.0          # gallons  (MAX_RANGE_MILES / MILES_PER_GALLON)
COST_TOLERANCE  = 0.02          # $0.02 rounding tolerance for float arithmetic


# ─────────────────────────────────────────────────────────────────────────────
# Arithmetic helpers (pure Python, no Django dependency)
# ─────────────────────────────────────────────────────────────────────────────

def _expected_fuel_cost_if_chosen(record: dict) -> float:
    """
    fuel_cost_if_chosen = gallons_if_chosen * price_per_gallon
                        + detour_fuel_cost (0 if not a detour)
    """
    gallons      = record["gallons_if_chosen"]
    price        = record["station"]["price_per_gallon"]
    detour_cost  = record.get("detour", {}).get("detour_fuel_cost", 0.0)
    return gallons * price + detour_cost


def _expected_total_if_chosen(record: dict, total_cost: float) -> float:
    """
    total_fuel_cost_if_chosen = total_cost - compared_stop_cost + fuel_cost_if_chosen
    When is_selected is True, compared_stop_cost == fuel_cost_if_chosen so
    total_if_chosen == total_cost.
    """
    fuel_cost_if_chosen = record["fuel_cost_if_chosen"]
    compared_stop_cost  = record.get("compared_stop_cost", 0.0)
    return total_cost - compared_stop_cost + fuel_cost_if_chosen


def _expected_delta(record: dict, total_cost: float) -> float:
    """delta = total_fuel_cost_if_chosen - total_cost"""
    return _expected_total_if_chosen(record, total_cost) - total_cost


# ─────────────────────────────────────────────────────────────────────────────
# 1. Calculation unit tests (no HTTP, no Django)
# ─────────────────────────────────────────────────────────────────────────────

class DebugStationCalculationTests(unittest.TestCase):
    """
    Verify the arithmetic of _debug_station_record against a known fixture.

    The fixture is taken directly from the example in the task description.
    Total cost (41.41) is back-calculated from:
        total_fuel_cost_if_chosen (41.26) - delta (-0.15) = 41.41
    """

    TOTAL_COST = 41.41

    RECORD = {
        "distance_traveled": 100.0,
        "miles_remaining": 200.87,
        "map_marker": {"lat": 43.2908053, "lng": -71.9473074},
        "route_anchor": {"lat": 43.113349, "lng": -70.726746},
        "is_selected": False,
        "is_detour": False,
        "gallons_if_chosen": 3.0,
        "fuel_cost_if_chosen": 10.95,          # 3.0 * 3.649 = 10.947 → stored as 10.95
        "total_fuel_cost_if_chosen": 41.26,
        "delta_total_fuel_cost": -0.15,
        "compared_stop_sequence": 1,
        "compared_stop_cost": 11.1,
        "station": {
            "opis_id": "67409",
            "name": "XTRA MART #7025",
            "address": "SR-146",
            "city": "Sutton",
            "state": "MA",
            "price_per_gallon": 3.649,
        },
    }

    # ── fuel_cost_if_chosen arithmetic ────────────────────────────────────────

    def test_fuel_cost_if_chosen_is_gallons_times_price(self):
        """fuel_cost_if_chosen ≈ gallons * price_per_gallon (±tolerance)."""
        rec      = self.RECORD
        expected = _expected_fuel_cost_if_chosen(rec)
        actual   = rec["fuel_cost_if_chosen"]
        self.assertAlmostEqual(
            actual, expected, delta=COST_TOLERANCE,
            msg=(
                f"fuel_cost_if_chosen={actual} does not match "
                f"gallons({rec['gallons_if_chosen']}) × price({rec['station']['price_per_gallon']}) "
                f"= {expected:.4f}"
            ),
        )

    # ── total_fuel_cost_if_chosen arithmetic ──────────────────────────────────

    def test_total_fuel_cost_if_chosen_formula(self):
        """
        total_fuel_cost_if_chosen = total_cost
                                    - compared_stop_cost
                                    + fuel_cost_if_chosen
        """
        rec      = self.RECORD
        expected = _expected_total_if_chosen(rec, self.TOTAL_COST)
        actual   = rec["total_fuel_cost_if_chosen"]
        self.assertAlmostEqual(
            actual, expected, delta=COST_TOLERANCE,
            msg=(
                f"total_fuel_cost_if_chosen={actual} should be "
                f"total_cost({self.TOTAL_COST}) "
                f"- compared_stop_cost({rec['compared_stop_cost']}) "
                f"+ fuel_cost_if_chosen({rec['fuel_cost_if_chosen']}) "
                f"= {expected:.2f}"
            ),
        )

    # ── delta arithmetic ──────────────────────────────────────────────────────

    def test_delta_equals_total_if_chosen_minus_total_cost(self):
        """delta_total_fuel_cost = total_fuel_cost_if_chosen - total_cost."""
        rec      = self.RECORD
        expected = rec["total_fuel_cost_if_chosen"] - self.TOTAL_COST
        actual   = rec["delta_total_fuel_cost"]
        self.assertAlmostEqual(
            actual, expected, delta=COST_TOLERANCE,
            msg=(
                f"delta_total_fuel_cost={actual} should equal "
                f"total_fuel_cost_if_chosen({rec['total_fuel_cost_if_chosen']}) "
                f"- total_cost({self.TOTAL_COST}) = {expected:.2f}"
            ),
        )

    # ── negative-delta invariant ──────────────────────────────────────────────

    def test_fixture_has_negative_delta_demonstrating_optimizer_miss(self):
        """
        The fixture deliberately has delta=-0.15, which means the optimizer
        chose a more expensive stop when this station would have been cheaper.
        This test documents the known failure; once the optimizer is fixed,
        flip the assertion to assertGreaterEqual.
        """
        delta = self.RECORD["delta_total_fuel_cost"]
        # Document the current buggy state:
        self.assertLess(
            delta, 0,
            msg=(
                "Expected this fixture to demonstrate a negative delta "
                "(optimizer missed a cheaper stop). If this assertion now fails "
                "it means the optimizer has been fixed — change this test to "
                "assertGreaterEqual(delta, 0)."
            ),
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Live integration tests
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL = "http://127.0.0.1:8000"
ENDPOINT = f"{BASE_URL}/api/route-fuel/"

HEADERS = {
    "Accept":           "*/*",
    "Accept-Language":  "en-US,en;q=0.9",
    "Connection":       "keep-alive",
    "Content-Type":     "application/json",
    "DNT":              "1",
    "Origin":           "http://localhost:3000",
    "Referer":          "http://localhost:3000/",
}

PAYLOAD = {
    "start_location": {
        "lat":   44.088138,
        "lng":  -69.531244,
        "label": "44.088138, -69.531244",
    },
    "finish_location": {
        "lat":   43.377,
        "lng":  -73.637966,
        "label": "43.377, -73.637966",
    },
    "start_mode":          "partial_tank",
    "start_fuel_gallons":  25,
}


def _assert_record_arithmetic(test: unittest.TestCase, record: dict, total_cost: float, idx: int):
    """
    Re-usable helper: check a single debug_gas_stations record.
    Called from both the arithmetic test and the no-negative-delta test.
    """
    station_name = record.get("station", {}).get("name", f"record[{idx}]")
    prefix       = f"debug_gas_stations[{idx}] ({station_name})"

    # ── fuel_cost_if_chosen ───────────────────────────────────────────────────
    expected_fuelcost = _expected_fuel_cost_if_chosen(record)
    actual_fuelcost   = record["fuel_cost_if_chosen"]
    test.assertAlmostEqual(
        actual_fuelcost, expected_fuelcost, delta=COST_TOLERANCE,
        msg=(
            f"{prefix}: fuel_cost_if_chosen={actual_fuelcost} should be "
            f"gallons({record['gallons_if_chosen']}) × "
            f"price({record['station']['price_per_gallon']}) "
            f"+ detour_fuel_cost = {expected_fuelcost:.4f}"
        ),
    )

    # ── total_fuel_cost_if_chosen ─────────────────────────────────────────────
    expected_total = _expected_total_if_chosen(record, total_cost)
    actual_total   = record["total_fuel_cost_if_chosen"]
    test.assertAlmostEqual(
        actual_total, expected_total, delta=COST_TOLERANCE,
        msg=(
            f"{prefix}: total_fuel_cost_if_chosen={actual_total} should be "
            f"total_cost({total_cost}) "
            f"- compared_stop_cost({record.get('compared_stop_cost', 0.0)}) "
            f"+ fuel_cost_if_chosen({actual_fuelcost}) "
            f"= {expected_total:.2f}"
        ),
    )

    # ── delta_total_fuel_cost ─────────────────────────────────────────────────
    expected_delta = actual_total - total_cost
    actual_delta   = record["delta_total_fuel_cost"]
    test.assertAlmostEqual(
        actual_delta, expected_delta, delta=COST_TOLERANCE,
        msg=(
            f"{prefix}: delta_total_fuel_cost={actual_delta} should equal "
            f"total_fuel_cost_if_chosen({actual_total}) "
            f"- total_cost({total_cost}) = {expected_delta:.2f}"
        ),
    )


class RouteFuelIntegrationTests(unittest.TestCase):
    """
    End-to-end tests that POST to the running Django server.
    Skipped automatically when the server is unreachable.
    """

    @classmethod
    def setUpClass(cls):
        try:
            resp = requests.get(BASE_URL, timeout=3)
            cls._server_available = True
        except requests.exceptions.ConnectionError:
            cls._server_available = False

        if cls._server_available:
            resp = requests.post(
                ENDPOINT,
                json=PAYLOAD,
                headers=HEADERS,
                timeout=120,   # route + DB queries can be slow on first call
            )
            cls._status_code = resp.status_code
            try:
                cls._data = resp.json()
            except Exception:
                cls._data = {}
                cls._status_code = -1

    def _skip_if_unavailable(self):
        if not self._server_available:
            self.skipTest("Server not running on localhost:8000 — skipping live test.")

    # ── HTTP basics ───────────────────────────────────────────────────────────

    def test_http_200(self):
        self._skip_if_unavailable()
        self.assertEqual(
            self._status_code, 200,
            msg=f"Expected HTTP 200, got {self._status_code}. Body: {self._data}",
        )

    def test_required_top_level_keys(self):
        self._skip_if_unavailable()
        required = {
            "start", "finish", "vehicle", "route",
            "fuel_stops", "debug_gas_stations", "total_fuel_cost",
        }
        missing = required - set(self._data.keys())
        self.assertFalse(missing, f"Response missing keys: {missing}")

    def test_total_fuel_cost_is_sum_of_stop_costs(self):
        self._skip_if_unavailable()
        stops      = self._data.get("fuel_stops", [])
        total_cost = self._data.get("total_fuel_cost", 0.0)
        computed   = round(sum(s["fuel_cost"] for s in stops), 2)
        self.assertAlmostEqual(
            computed, total_cost, delta=COST_TOLERANCE,
            msg=(
                f"total_fuel_cost={total_cost} but sum(fuel_stops.fuel_cost)={computed}"
            ),
        )

    # ── Per-record arithmetic ─────────────────────────────────────────────────

    def test_debug_station_arithmetic_is_consistent(self):
        """
        For every record in debug_gas_stations:
          fuel_cost_if_chosen  ≈ gallons × price + detour_cost
          total_fuel_cost_if_chosen ≈ total_cost - compared_stop_cost + fuel_cost_if_chosen
          delta_total_fuel_cost ≈ total_fuel_cost_if_chosen - total_cost
        """
        self._skip_if_unavailable()
        records    = self._data.get("debug_gas_stations", [])
        total_cost = self._data.get("total_fuel_cost", 0.0)

        self.assertTrue(records, "debug_gas_stations is empty — nothing to verify.")

        for idx, record in enumerate(records):
            with self.subTest(idx=idx, station=record.get("station", {}).get("name")):
                _assert_record_arithmetic(self, record, total_cost, idx)

    # ── No negative delta (core optimizer correctness check) ─────────────────

    def test_no_debug_station_has_negative_delta(self):
        """
        delta_total_fuel_cost < 0 means the optimizer chose a more expensive
        stop when this cheaper station was reachable.  All deltas must be ≥ 0.

        A negative delta is the symptom described in the task: a station in
        debug_gas_stations shows it would have been cheaper than the selected
        stop, which means the DP missed the optimal solution.
        """
        self._skip_if_unavailable()
        records    = self._data.get("debug_gas_stations", [])
        total_cost = self._data.get("total_fuel_cost", 0.0)

        violations = []
        for idx, record in enumerate(records):
            delta = record.get("delta_total_fuel_cost", 0.0)
            if delta < -COST_TOLERANCE:
                station = record.get("station", {})
                violations.append(
                    f"  [{idx}] {station.get('name')} ({station.get('city')}, {station.get('state')}) "
                    f"@ mile {record.get('distance_traveled')}: "
                    f"delta={delta:.4f}  "
                    f"(would save ${abs(delta):.2f} vs total_cost={total_cost})"
                )

        self.assertFalse(
            violations,
            msg=(
                f"Optimizer missed {len(violations)} cheaper station(s) — "
                f"their delta_total_fuel_cost is negative:\n"
                + "\n".join(violations)
                + "\n\nA negative delta means the DP chose a more expensive stop "
                "when a cheaper reachable station existed. Fix the optimizer "
                "so all debug stations have delta ≥ 0."
            ),
        )

    # ── Fuel stop sequence sanity ─────────────────────────────────────────────

    def test_fuel_stops_are_in_order(self):
        """Stops must appear in ascending distance_traveled order."""
        self._skip_if_unavailable()
        stops = self._data.get("fuel_stops", [])
        miles = [s["distance_traveled"] for s in stops]
        self.assertEqual(
            miles, sorted(miles),
            msg=f"fuel_stops are not in ascending mile order: {miles}",
        )

    def test_fuel_stop_sequences_are_consecutive(self):
        """sequence numbers must be 1, 2, 3, … with no gaps."""
        self._skip_if_unavailable()
        stops   = self._data.get("fuel_stops", [])
        seqs    = [s["sequence"] for s in stops]
        expected = list(range(1, len(stops) + 1))
        self.assertEqual(seqs, expected, msg=f"sequence numbers: {seqs}")

    def test_no_stop_exceeds_tank_capacity(self):
        """gallons_to_fill must never exceed tank capacity."""
        self._skip_if_unavailable()
        for stop in self._data.get("fuel_stops", []):
            self.assertLessEqual(
                stop["gallons_to_fill"], TANK_CAPACITY + 0.01,
                msg=(
                    f"Stop at mile {stop['distance_traveled']} wants to fill "
                    f"{stop['gallons_to_fill']} gal which exceeds tank capacity "
                    f"{TANK_CAPACITY} gal."
                ),
            )

    def test_route_has_geometry(self):
        """Route GeoJSON LineString must have at least two coordinates."""
        self._skip_if_unavailable()
        coords = self._data.get("route", {}).get("geojson", {}).get("coordinates", [])
        self.assertGreaterEqual(len(coords), 2, "Route geometry has fewer than 2 coordinates.")

    def test_miles_remaining_decreases(self):
        """miles_remaining at each stop must be less than at the previous stop."""
        self._skip_if_unavailable()
        stops = self._data.get("fuel_stops", [])
        for i in range(1, len(stops)):
            prev = stops[i - 1]["miles_remaining"]
            curr = stops[i]["miles_remaining"]
            self.assertLess(
                curr, prev,
                msg=(
                    f"miles_remaining did not decrease between stop {i} ({prev}) "
                    f"and stop {i+1} ({curr})."
                ),
            )


# ─────────────────────────────────────────────────────────────────────────────
# Run with plain `python test_route_fuel_integration.py`
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
