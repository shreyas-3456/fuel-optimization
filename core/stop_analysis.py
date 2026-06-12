from typing import Optional
import math
import logging
from core.profiling import profiled

log = logging.getLogger(__name__)

from .routing import (
    DPNode,
    MILES_PER_GALLON,
    TANK_CAPACITY,
    ROAD_FACTOR_WEIGHT,
    ROAD_FACTOR_EXPECTED,
    ROAD_FACTOR_CONSERVATIVE,
    _snap_fuel,
    _haversine_miles,
)


# -----------------------------------------------------------------------------
# Public helper — used by routing.py for the optimal breakdown string
# -----------------------------------------------------------------------------

def _build_cost_breakdown(ordered_stops: list[dict]) -> str:
    parts = []
    total = 0.0
    for item in ordered_stops:
        label   = item['label']
        cost    = item['cost']
        name    = item.get('name', '')
        total  += cost
        id_part = f" [{name}]" if name else ""
        parts.append(f"{label}{id_part}: ${cost:.2f}")
    return " + ".join(parts) + f" = ${round(total, 2):.2f}"


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------

def _nearest_stop_for_candidate(node: DPNode, stops: list[dict]) -> Optional[dict]:
    if not stops:
        return None
    return min(
        stops,
        key=lambda s: abs(float(s.get('distance_traveled', 0.0)) - node.route_mile),
    )


def _nearest_prior_stop(node: DPNode, stops: list[dict]) -> Optional[dict]:
    prior = [s for s in stops if float(s.get('distance_traveled', 0.0)) < node.route_mile]
    return prior[-1] if prior else None


def _min_gallons_to_reach(from_stop: dict, to_node: DPNode) -> float:
    from_mile   = float(from_stop['distance_traveled'])
    # To reach a detour station from the route: drive to its route anchor,
    # then drive the one-way detour distance to the station itself.
    one_way     = to_node.detour_miles / 2 if to_node.is_detour else 0.0
    drive_miles = (to_node.route_mile - from_mile) + one_way
    return _snap_fuel(drive_miles / MILES_PER_GALLON, mode='ceil')


# -----------------------------------------------------------------------------
# Reachability check using haversine + road factor
#
# Determines whether the candidate station is physically reachable from the
# starting point given available fuel, accounting for:
#   - Route miles to the candidate's anchor point on the route
#   - One-way detour miles (if it's an off-route station)
# Returns True if reachable, False otherwise.
# -----------------------------------------------------------------------------

def _can_reach_candidate(
    node: DPNode,
    start_fuel: float,
    prior_fuel_on_arrival: float,
    from_mile: float,
) -> bool:
    """
    Check if we can physically reach the candidate node from `from_mile`
    with `prior_fuel_on_arrival` gallons.

    For a detour station: drive (route_mile - from_mile) + one_way_detour.
    Max range from prior point = prior_fuel_on_arrival * MILES_PER_GALLON.
    """
    one_way     = node.detour_miles / 2 if node.is_detour else 0.0
    drive_miles = (node.route_mile - from_mile) + one_way
    fuel_needed = drive_miles / MILES_PER_GALLON
    return prior_fuel_on_arrival >= fuel_needed - 1e-6


# -----------------------------------------------------------------------------
# Fuel-state simulation
#
# Walk the original selected stops in order, tracking tank level exactly
# the way the DP did.  Returns (fuel_on_arrival, prefix_cost, reachable).
#
#   fuel_on_arrival  — gallons in tank when we pull into this candidate
#                      (AFTER driving to its anchor point + one-way detour)
#   prefix_cost      — extra money spent topping up at earlier stops just
#                      to reach this candidate (normally 0 on the DP plan)
#   reachable        — False if we physically cannot reach the candidate
#                      even after filling to the brim at every prior stop
# -----------------------------------------------------------------------------

def _fuel_state_at_candidate(
    node:                 DPNode,
    stops:                list[dict],
    start_fuel:           float,
) -> tuple[float, float, bool]:
    candidate_mile = node.route_mile
    one_way        = node.detour_miles / 2 if node.is_detour else 0.0
    candidate_id   = node.station.opis_id if node.station else None

    # Only consider stops that are before this candidate and not the candidate itself
    prior_stops = sorted(
        [s for s in stops
         if s.get('station', {}).get('opis_id') != candidate_id
         and float(s['distance_traveled']) < candidate_mile],
        key=lambda s: float(s['distance_traveled']),
    )

    # Two-pass approach:
    # Pass 1: walk stops tracking fuel; record per-stop state for backfill.
    # Pass 2 (inline): when short, walk BACKWARDS through already-visited stops
    #         to top up at ones that have tank headroom.
    #
    # Each entry: (price, fuel_after_fill, max_topup_room)
    stop_states: list[dict] = []   # one per prior stop, in order

    fuel          = start_fuel
    prefix_cost   = 0.0
    prev_mile     = 0.0
    prev_one_way  = 0.0   # return leg owed from the previous detour stop

    for idx, s in enumerate(prior_stops):
        s_mile    = float(s['distance_traveled'])
        s_detour  = float(s.get('detour', {}).get('one_way_road_miles', 0.0))
        s_price   = float(s['station']['price_per_gallon'])
        # Drive: return from prev detour (if any) + route miles to this anchor + arrive detour
        leg       = prev_one_way + (s_mile - prev_mile) + s_detour
        consume   = _snap_fuel(leg / MILES_PER_GALLON, mode='ceil')

        if consume > fuel + 1e-6:
            # Short on fuel — walk BACKWARDS through already-visited stops
            # and top up at ones that still have tank headroom.
            shortfall = _snap_fuel(consume - fuel, mode='ceil')
            for back_idx in range(len(stop_states) - 1, -1, -1):
                if shortfall <= 1e-6:
                    break
                back = stop_states[back_idx]
                headroom = TANK_CAPACITY - back['fuel_after']
                if headroom < 0.5 - 1e-6:     # less than FUEL_STEP
                    continue
                topup = _snap_fuel(min(shortfall, headroom), mode='ceil')
                if topup <= 1e-6:
                    continue
                # Apply topup: update this historical stop's fuel_after and
                # propagate the extra fuel forward to `fuel`.
                back['fuel_after'] = min(back['fuel_after'] + topup, TANK_CAPACITY)
                prefix_cost       += topup * back['price']
                fuel              += topup
                shortfall          = _snap_fuel(max(0.0, consume - fuel), mode='ceil')
                log.debug(
                    "_fuel_state_at_candidate | backfill %.2f gal at prior stop idx=%d "
                    "(price=%.3f) to reach stop idx=%d",
                    topup, back_idx, back['price'], idx,
                )

        if consume > fuel + 1e-6:
            # Still can't reach even after backfilling — truly unreachable
            return 0.0, 0.0, False

        fuel      -= _snap_fuel(consume, mode='ceil')
        fuel       = max(fuel, 0.0)
        fuel      += _snap_fuel(float(s['gallons_to_fill']))
        fuel       = min(fuel, TANK_CAPACITY)

        stop_states.append({
            'price':      s_price,
            'fuel_after': fuel,
        })

        # After a detour stop, we still owe the return leg; record it for the next iteration.
        # prev_mile stays as the route anchor (not offset by detour).
        prev_one_way = s_detour
        prev_mile    = s_mile

    # Drive from last prior stop's route anchor to the candidate's anchor + one-way detour
    # Include the return leg from the last prior detour stop (if any).
    leg_to_cand = prev_one_way + (candidate_mile - prev_mile) + one_way
    consume     = _snap_fuel(leg_to_cand / MILES_PER_GALLON, mode='ceil')

    if consume > fuel + 1e-6:
        # Short on fuel for final leg to candidate — try backfilling at prior stops
        shortfall = _snap_fuel(consume - fuel, mode='ceil')
        for back_idx in range(len(stop_states) - 1, -1, -1):
            if shortfall <= 1e-6:
                break
            back     = stop_states[back_idx]
            headroom = TANK_CAPACITY - back['fuel_after']
            if headroom < 0.5 - 1e-6:
                continue
            topup = _snap_fuel(min(shortfall, headroom), mode='ceil')
            if topup <= 1e-6:
                continue
            back['fuel_after'] = min(back['fuel_after'] + topup, TANK_CAPACITY)
            prefix_cost       += topup * back['price']
            fuel              += topup
            shortfall          = _snap_fuel(max(0.0, consume - fuel), mode='ceil')
            log.debug(
                "_fuel_state_at_candidate | backfill %.2f gal at prior stop idx=%d "
                "to reach candidate",
                topup, back_idx,
            )

    if consume > fuel + 1e-6:
        return 0.0, 0.0, False

    fuel_on_arrival = max(fuel - _snap_fuel(consume, mode='ceil'), 0.0)
    return fuel_on_arrival, prefix_cost, True


# -----------------------------------------------------------------------------
# Trip simulation
#
# Given an ordered stop plan, simulate the full trip and return per-stop
# results and total cost.
#
# Stop plan entry keys:
#   route_mile, one_way_detour, detour_fuel_cost, price,
#   label, name, is_candidate (bool), fixed_gallons (float or None)
#
# Fixed stops  -> buy exactly fixed_gallons (DP decision, preserved)
# Candidate    -> buy the minimum needed to keep EVERY remaining leg feasible,
#                 accounting for what will be bought at later fixed stops.
#
# Raises ValueError if any leg is physically impossible (including when the
# required fuel exceeds TANK_CAPACITY — the trip cannot be completed).
# -----------------------------------------------------------------------------

def _simulate_trip(
    plan:                 list[dict],
    start_fuel:           float,
    total_distance_miles: float,
) -> tuple[list[dict], float]:

    fuel       = start_fuel
    results    = []
    total_cost = 0.0

    for i, stop in enumerate(plan):
        route_mile     = stop['route_mile']
        one_way_detour = stop['one_way_detour']
        price          = stop['price']
        is_candidate   = stop.get('is_candidate', False)

        # Drive to this stop:
        #   return from prev stop's detour (if it was off-route)
        #   + route miles from prev anchor to this anchor
        #   + one-way detour to reach this station
        if i > 0:
            prev_stop         = plan[i - 1]
            prev_mile         = prev_stop['route_mile']
            prev_return_miles = prev_stop['one_way_detour']   # 0 if on-route
        else:
            prev_mile         = 0.0
            prev_return_miles = 0.0
        leg_miles = prev_return_miles + (route_mile - prev_mile) + one_way_detour
        consume   = _snap_fuel(leg_miles / MILES_PER_GALLON, mode='ceil')

        if consume > fuel + 1e-6:
            # Physically impossible even on a full tank — truly infeasible.
            if consume > TANK_CAPACITY + 1e-6:
                raise ValueError(
                    f"Infeasible: leg of {leg_miles:.1f} mi to {stop['label']} "
                    f"requires {consume:.2f} gal > tank capacity {TANK_CAPACITY:.1f} gal"
                )
            # We're short — walk BACKWARDS through prior stops to top up
            # at ones that have tank headroom, spreading the fill if needed.
            shortfall = _snap_fuel(consume - fuel, mode='ceil')
            if results:
                for back_i in range(len(results) - 1, -1, -1):
                    if shortfall <= 1e-6:
                        break
                    prev_result = results[back_i]
                    headroom    = TANK_CAPACITY - prev_result['fuel_after']
                    if headroom < 0.5 - 1e-6:   # less than FUEL_STEP
                        continue
                    topup = _snap_fuel(min(shortfall, headroom), mode='ceil')
                    if topup <= 1e-6:
                        continue
                    topup_cost = round(topup * prev_result['price'], 2)
                    results[back_i] = {
                        **prev_result,
                        'gallons_bought': round(prev_result['gallons_bought'] + topup, 4),
                        'fuel_cost':      round(prev_result['fuel_cost']      + topup_cost, 2),
                        'fuel_after':     round(min(prev_result['fuel_after'] + topup, TANK_CAPACITY), 4),
                    }
                    total_cost  = round(total_cost + topup_cost, 2)
                    fuel        = min(fuel + topup, TANK_CAPACITY)
                    shortfall   = _snap_fuel(max(0.0, consume - fuel), mode='ceil')
                    log.debug(
                        "_simulate_trip | topped up %.2f gal at %s (+$%.2f) to reach %s",
                        topup, prev_result['label'], topup_cost, stop['label'],
                    )
                if shortfall > 1e-6:
                    raise ValueError(
                        f"Infeasible: cannot top up enough at prior stops "
                        f"to reach {stop['label']} (still need {shortfall:.2f} extra gal)"
                    )
            else:
                raise ValueError(
                    f"Infeasible: need {consume:.2f} gal to reach first stop "
                    f"{stop['label']} but only have {fuel:.2f} gal and no prior stop to top up"
                )

        fuel -= _snap_fuel(consume, mode='ceil')
        fuel  = max(fuel, 0.0)

        # Decide how many gallons to buy
        if is_candidate:
            # Walk ALL remaining legs to find the tightest shortfall,
            # simulating what later fixed stops will contribute.
            # CRITICAL: also verify the leg is physically possible (≤ TANK_CAPACITY).
            remaining     = plan[i + 1:]
            running_fuel  = fuel
            running_mile  = route_mile
            max_shortfall = 0.0

            if not remaining:
                # Last stop before destination — must be able to reach the end.
                end_leg     = one_way_detour + (total_distance_miles - route_mile)
                end_consume = _snap_fuel(end_leg / MILES_PER_GALLON, mode='ceil')
                if end_consume > TANK_CAPACITY + 1e-6:
                    raise ValueError(
                        f"Infeasible: leg of {end_leg:.1f} mi from {stop['label']} to "
                        f"destination requires {end_consume:.2f} gal > tank capacity "
                        f"{TANK_CAPACITY:.1f} gal"
                    )
                max_shortfall = max(0.0, end_consume - running_fuel)
            else:
                # Start with the candidate's own return leg on the first future stop.
                # After fueling at a detour station, the driver returns to the route
                # anchor before heading to the next stop.
                pending_return = one_way_detour   # candidate's one-way return
                for future in remaining:
                    f_leg     = pending_return + (future['route_mile'] - running_mile) + future['one_way_detour']
                    f_consume = _snap_fuel(f_leg / MILES_PER_GALLON, mode='ceil')

                    # If a single leg exceeds tank capacity the plan is physically impossible.
                    if f_consume > TANK_CAPACITY + 1e-6:
                        raise ValueError(
                            f"Infeasible: leg of {f_leg:.1f} mi from {stop['label']} to "
                            f"{future['label']} requires {f_consume:.2f} gal > tank capacity "
                            f"{TANK_CAPACITY:.1f} gal"
                        )

                    shortfall = max(0.0, f_consume - running_fuel)
                    if shortfall > max_shortfall:
                        max_shortfall = shortfall
                    running_fuel   = max(running_fuel - f_consume, 0.0)
                    fixed          = future.get('fixed_gallons') or 0.0
                    running_fuel   = min(running_fuel + fixed, TANK_CAPACITY)
                    running_mile   = future['route_mile']
                    pending_return = future['one_way_detour']   # return owed from THIS future stop

                # Also check the final leg from last stop to destination.
                # pending_return carries the last future stop's one_way return (if detour).
                last_future = remaining[-1]
                last_mile   = last_future['route_mile']
                final_leg   = pending_return + (total_distance_miles - last_mile)
                final_cons  = _snap_fuel(final_leg / MILES_PER_GALLON, mode='ceil')
                if final_cons > TANK_CAPACITY + 1e-6:
                    raise ValueError(
                        f"Infeasible: final leg of {final_leg:.1f} mi from "
                        f"{last_future['label']} to destination requires {final_cons:.2f} gal "
                        f"> tank capacity {TANK_CAPACITY:.1f} gal"
                    )
                # Account for the final leg shortfall given the last stop's fixed fill.
                # If the last fixed stop is short, the candidate must cover that too.
                last_fuel_after = min(running_fuel, TANK_CAPACITY)
                final_shortfall = max(0.0, final_cons - last_fuel_after)
                if final_shortfall > max_shortfall:
                    max_shortfall = final_shortfall

            gallons = _snap_fuel(
                min(max_shortfall, TANK_CAPACITY - fuel), mode='ceil'
            )

            # Verify that even after filling to max, we can cover the
            # immediate next leg (candidate must reach at least the next stop).
            # Includes: candidate's own return leg + route miles + next stop's detour.
            if remaining:
                next_stop    = remaining[0]
                next_leg     = one_way_detour + (next_stop['route_mile'] - route_mile) + next_stop['one_way_detour']
                next_consume = _snap_fuel(next_leg / MILES_PER_GALLON, mode='ceil')
                if fuel + gallons < next_consume - 1e-6:
                    raise ValueError(
                        f"Infeasible: even filling to {fuel + gallons:.2f} gal at "
                        f"{stop['label']} is not enough to reach next stop "
                        f"{next_stop['label']} ({next_consume:.2f} gal needed)"
                    )
        else:
            gallons = _snap_fuel(stop['fixed_gallons'])

        cost        = round(gallons * price + stop['detour_fuel_cost'], 2)
        fuel       += gallons
        fuel        = min(fuel, TANK_CAPACITY)
        total_cost += cost

        results.append({
            **stop,
            'gallons_bought': round(gallons, 4),
            'fuel_cost':      cost,
            'fuel_after':     round(fuel, 4),
        })

    # Final check: verify we can reach destination from the last stop.
    # Instead of hard-rejecting, top up at prior stops (walking backwards)
    # if there's tank headroom.
    if results:
        last       = results[-1]
        final_leg  = last['one_way_detour'] + (total_distance_miles - last['route_mile'])
        final_cons = _snap_fuel(final_leg / MILES_PER_GALLON, mode='ceil')
        shortfall  = final_cons - last['fuel_after']
        if shortfall > 1e-6:
            # Physically impossible even on a full tank.
            if final_cons > TANK_CAPACITY + 1e-6:
                raise ValueError(
                    f"Infeasible: final leg of {final_leg:.1f} mi requires "
                    f"{final_cons:.2f} gal > tank capacity {TANK_CAPACITY:.1f} gal"
                )
            extra_needed = _snap_fuel(shortfall, mode='ceil')
            for back_i in range(len(results) - 1, -1, -1):
                if extra_needed <= 1e-6:
                    break
                r        = results[back_i]
                headroom = TANK_CAPACITY - r['fuel_after']
                if headroom < 0.5 - 1e-6:
                    continue
                topup = _snap_fuel(min(extra_needed, headroom), mode='ceil')
                if topup <= 1e-6:
                    continue
                topup_cost = round(topup * r['price'], 2)
                results[back_i] = {
                    **r,
                    'gallons_bought': round(r['gallons_bought'] + topup, 4),
                    'fuel_cost':      round(r['fuel_cost']      + topup_cost, 2),
                    'fuel_after':     round(min(r['fuel_after'] + topup, TANK_CAPACITY), 4),
                }
                total_cost   = round(total_cost + topup_cost, 2)
                extra_needed = _snap_fuel(max(0.0, extra_needed - topup), mode='ceil')
                log.debug(
                    "_simulate_trip | topped up %.2f gal at %s (+$%.2f) to reach destination",
                    topup, r['label'], topup_cost,
                )
            if extra_needed > 1e-6:
                raise ValueError(
                    f"Infeasible: cannot top up enough at prior stops "
                    f"to reach destination (still need {extra_needed:.2f} extra gal)"
                )

    return results, round(total_cost, 2)


# -----------------------------------------------------------------------------
# Build a stop plan for REPLACE or APPEND
#
# REPLACE: remove the nearest selected stop, insert candidate in its place.
#          The stop AFTER the removed one now needs its prev_mile updated —
#          this is handled naturally since _build_plan re-sorts by route_mile
#          and _simulate_trip always uses plan[i-1] as the previous stop.
#
# APPEND:  keep all selected stops, insert candidate as an extra stop.
#
# The candidate always has is_candidate=True / fixed_gallons=None so the
# simulation computes the correct minimum fill for that specific plan.
# -----------------------------------------------------------------------------

def _build_plan(
    stops:      list[dict],
    node:       DPNode,
    exclude_id: str | None,
) -> list[dict]:
    plan = []

    for s in stops:
        if exclude_id and s.get('station', {}).get('opis_id') == exclude_id:
            continue
        det = s.get('detour', {})
        plan.append({
            'route_mile':       float(s['distance_traveled']),
            'price':            float(s['station']['price_per_gallon']),
            'one_way_detour':   float(det.get('one_way_road_miles', 0.0)),
            'detour_fuel_cost': float(det.get('detour_fuel_cost', 0.0)),
            'label':            f"Stop {s['sequence']}",
            'name':             s.get('station', {}).get('name', ''),
            'is_candidate':     False,
            'fixed_gallons':    float(s['gallons_to_fill']),
        })

    one_way = node.detour_miles / 2 if node.is_detour else 0.0
    plan.append({
        'route_mile':       node.route_mile,
        'price':            node.price,
        'one_way_detour':   one_way,
        'detour_fuel_cost': node.detour_fuel_cost,
        'label':            '(this)',
        'name':             node.station.name if node.station else '',
        'is_candidate':     True,
        'fixed_gallons':    None,
    })

    plan.sort(key=lambda x: x['route_mile'])

    # Re-label sequentially so breakdown strings read correctly
    seq = 1
    for item in plan:
        suffix        = ' (this)' if item['label'] == '(this)' else ''
        item['label'] = f'Stop {seq}{suffix}'
        seq          += 1

    return plan


# -----------------------------------------------------------------------------
# Turn filled simulation results into a strategy dict
# -----------------------------------------------------------------------------

def _make_strategy(filled: list[dict], dp_total: float) -> dict:
    sim_total = round(sum(s['fuel_cost'] for s in filled), 2)
    delta_raw = round(sim_total - dp_total, 2)
    delta_str = f"+${delta_raw:.2f}" if delta_raw >= 0 else f"-${abs(delta_raw):.2f}"

    if sim_total < dp_total - 0.02:
        log.info(
            "stop_analysis | sim_total=%.2f dp_total=%.2f delta=%.2f"
            " — candidate plan is cheaper than DP original",
            sim_total, dp_total, delta_raw,
        )

    parts = [
        f"{s['label']} [{s['name']}]: ${s['fuel_cost']:.2f}"
        for s in filled
    ]
    breakdown = ' + '.join(parts) + f' = ${sim_total:.2f}'

    return {
        'available':      True,
        'total_cost':     sim_total,
        'cost_delta':     delta_str,
        'cost_delta_raw': delta_raw,
        'cost_breakdown': breakdown,
        # Include per-stop detail so frontend can show correct gallons
        'stops':          [
            {
                'label':          s['label'],
                'name':           s['name'],
                'gallons_bought': s['gallons_bought'],
                'fuel_cost':      s['fuel_cost'],
                'fuel_after':     s['fuel_after'],
                'is_candidate':   s.get('is_candidate', False),
            }
            for s in filled
        ],
    }


# -----------------------------------------------------------------------------
# Run REPLACE and APPEND for one candidate node
#
# REPLACE: swap out the nearest selected stop for this candidate.
#          BUG FIX — before running the simulation, verify the candidate
#          is reachable from start with start_fuel given the new plan.
#          If the replaced stop was the only stop reachable from start,
#          the driver may not be able to reach the candidate at all.
#
# Returns dict with keys: replace, append, cheapest
# Returns {} if both strategies are infeasible.
# -----------------------------------------------------------------------------

def _run_strategies(
    node:                 DPNode,
    stops:                list[dict],
    dp_total:             float,
    start_fuel:           float,
    total_distance_miles: float,
) -> dict:
    if not node.station:
        return {}

    nearest    = _nearest_stop_for_candidate(node, stops)
    replace_id = nearest.get('station', {}).get('opis_id') if nearest else None

    strategies = {}

    for name, exclude_id in (('replace', replace_id), ('append', None)):
        try:
            plan = _build_plan(stops, node, exclude_id)

            # Let _simulate_trip handle feasibility — it has proper top-up
            # logic that can buy extra fuel at prior stops when the driver
            # is short.  A hard pre-reject here misses cases where topping up
            # at an earlier stop makes the candidate reachable.

            filled, _         = _simulate_trip(plan, start_fuel, total_distance_miles)
            strategies[name]  = _make_strategy(filled, dp_total)
        except ValueError as exc:
            log.debug(
                "stop_analysis | %s infeasible opis=%s: %s",
                name, node.station.opis_id, exc,
            )
            strategies[name] = {'available': False, 'reason': str(exc)}
        except Exception as exc:
            log.exception(
                "stop_analysis | %s error opis=%s: %s",
                name, node.station.opis_id, exc,
            )
            strategies[name] = {'available': False, 'reason': str(exc)}

    rep_ok = strategies['replace'].get('available', False)
    app_ok = strategies['append'].get('available',  False)

    if not rep_ok and not app_ok:
        return {}

    if rep_ok and app_ok:
        cheapest = (
            'replace'
            if strategies['replace']['total_cost'] <= strategies['append']['total_cost']
            else 'append'
        )
    else:
        cheapest = 'replace' if rep_ok else 'append'

    return {
        'replace':  strategies['replace'],
        'append':   strategies['append'],
        'cheapest': cheapest,
    }


# -----------------------------------------------------------------------------
# Build the debug record for one candidate DPNode
# -----------------------------------------------------------------------------

def _debug_station_record(
    node:                 DPNode,
    stops:                list[dict],
    dp_total:             float,
    start_fuel:           float,
    total_distance_miles: float = 0.0,
    run_analysis:         bool  = False,
) -> dict | None:
    if node.station is None:
        return None

    selected_ids = {s.get('station', {}).get('opis_id') for s in stops}
    is_selected  = node.station.opis_id in selected_ids
    candidate_id = node.station.opis_id
    one_way      = node.detour_miles / 2 if node.is_detour else 0.0

    # Compute exact fuel state when arriving at this candidate
    fuel_on_arrival, prefix_cost, reachable = _fuel_state_at_candidate(
        node, stops, start_fuel
    )

    # -------------------------------------------------------------------------
    # BUG FIX: gallons_if_chosen must account for the FULL remaining journey,
    # not just the next stop. We also need to validate that a full tank is
    # even sufficient to cover the next leg (detour return + route to next stop).
    # -------------------------------------------------------------------------
    later_stops = sorted(
        [s for s in stops
         if s.get('station', {}).get('opis_id') != candidate_id
         and float(s['distance_traveled']) > node.route_mile],
        key=lambda s: float(s['distance_traveled']),
    )

    if not reachable:
        gallons_if_chosen = 0.0
    else:
        # Walk ALL remaining legs to find the maximum shortfall at any point,
        # accounting for what later fixed stops will contribute.
        running_fuel = fuel_on_arrival
        running_mile = node.route_mile   # back on route after detour
        max_shortfall = 0.0

        if not later_stops:
            # Must reach destination from here
            # After detour station, drive back to route anchor (one_way) then to end
            final_drive = one_way + (total_distance_miles - node.route_mile)
            final_cons  = _snap_fuel(final_drive / MILES_PER_GALLON, mode='ceil')
            # If even a full tank can't cover it, we'd need intermediate stops — flag as 0
            if final_cons > TANK_CAPACITY + 1e-6:
                gallons_if_chosen = 0.0
                reachable = False  # effectively unreachable with no more stops
            else:
                max_shortfall     = max(0.0, final_cons - running_fuel)
                gallons_if_chosen = _snap_fuel(
                    min(max_shortfall, TANK_CAPACITY - fuel_on_arrival), mode='ceil'
                )
        else:
            # Use pending_return to chain detour returns through all iterations,
            # just like _simulate_trip does.
            pending_return = one_way   # candidate's one-way return
            for future in later_stops:
                f_mile   = float(future['distance_traveled'])
                f_detour = float(future.get('detour', {}).get('one_way_road_miles', 0.0))
                # From our current position, first return from the previous detour (if any),
                # then drive route miles to next stop's anchor + its one-way detour.
                f_leg = pending_return + (f_mile - running_mile) + f_detour

                f_consume = _snap_fuel(f_leg / MILES_PER_GALLON, mode='ceil')
                shortfall = max(0.0, f_consume - running_fuel)
                if shortfall > max_shortfall:
                    max_shortfall = shortfall
                running_fuel  = max(running_fuel - f_consume, 0.0)
                running_fuel += _snap_fuel(float(future['gallons_to_fill']))
                running_fuel  = min(running_fuel, TANK_CAPACITY)
                running_mile  = f_mile
                pending_return = f_detour   # return owed from THIS future stop

            # Also check the final leg from last stop to destination.
            # pending_return already carries the last future stop's detour return.
            last_mile   = float(later_stops[-1]['distance_traveled'])
            final_drive = pending_return + (total_distance_miles - last_mile)
            final_cons  = _snap_fuel(final_drive / MILES_PER_GALLON, mode='ceil')
            final_short = max(0.0, final_cons - running_fuel)
            if final_short > max_shortfall:
                max_shortfall = final_short

            gallons_if_chosen = _snap_fuel(
                min(max_shortfall, TANK_CAPACITY - fuel_on_arrival), mode='ceil'
            )

    # Skip non-selected detour stations where we'd buy nothing
    if not is_selected and node.is_detour and gallons_if_chosen == 0.0 and reachable:
        return None

    # Skip non-selected non-detour stations where we'd buy nothing
    if not is_selected and not reachable:
        pass  # keep unreachable stations so frontend can show them as such
    elif not is_selected and gallons_if_chosen == 0.0:
        return None

    fuel_cost_if_chosen = round(gallons_if_chosen * node.price, 2)

    nearest_selected      = _nearest_stop_for_candidate(node, stops)
    nearest_selected_cost = float(nearest_selected.get('fuel_cost', 0.0)) if nearest_selected else 0.0

    prior_stop                  = _nearest_prior_stop(node, stops)
    prior_gallons_original      = float(prior_stop['gallons_to_fill']) if prior_stop else None
    prior_gallons_to_reach_here = _min_gallons_to_reach(prior_stop, node) if prior_stop else None

    lon, lat    = node.point
    station_lon = node.station.location.x
    station_lat = node.station.location.y

    record: dict = {
        'distance_traveled':   round(node.route_mile, 2),
        'miles_remaining':     round(max(total_distance_miles - node.route_mile, 0.0), 2),
        'map_marker':          {'lat': station_lat, 'lng': station_lon},
        'route_anchor':        {'lat': lat,         'lng': lon},
        'is_selected':         is_selected,
        'is_detour':           node.is_detour,
        'is_unreachable':      not reachable,
        'is_feasible':         False,       # filled below
        'gallons_if_chosen':   round(gallons_if_chosen, 4),
        'fuel_cost_if_chosen': fuel_cost_if_chosen,
        'prefix_fill_cost':    round(prefix_cost, 2) if reachable else None,
        'total_fuel_cost_if_chosen': None,  # filled below
        'delta_total_fuel_cost':     None,  # filled below
        'compared_stop_sequence':    nearest_selected.get('sequence') if nearest_selected else None,
        'compared_stop_cost':        round(nearest_selected_cost, 2),
        'previous_stop_gallons_original':
            round(prior_gallons_original, 4) if prior_gallons_original is not None else None,
        'previous_stop_gallons_to_reach_here':
            round(prior_gallons_to_reach_here, 4) if prior_gallons_to_reach_here is not None else None,
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

    # ---- selected stop: cost is dp_total by definition ----------------------
    if is_selected:
        record['total_fuel_cost_if_chosen'] = dp_total
        record['delta_total_fuel_cost']     = 0.0
        record['is_feasible']               = True
        return record

    # ---- unreachable --------------------------------------------------------
    if not reachable:
        record['infeasible_reason'] = 'unreachable'
        return record

    # ---- run replace + append simulations -----------------------------------
    if run_analysis:
        analysis = _run_strategies(node, stops, dp_total, start_fuel, total_distance_miles)

        if not analysis:
            record['is_feasible']       = False
            record['infeasible_reason'] = 'no_feasible_strategy'
            record['stop_analysis']     = {'available': False, 'reason': 'no_feasible_strategy'}
            return record

        cheapest_key = analysis['cheapest']
        best         = analysis[cheapest_key]

        record['total_fuel_cost_if_chosen'] = best['total_cost']
        record['delta_total_fuel_cost']     = best['cost_delta_raw']
        record['is_feasible']               = True

        # BUG FIX: update gallons_if_chosen from the simulation result
        # (the candidate stop's actual simulated gallons), not the approximation.
        candidate_sim_stop = next(
            (s for s in best.get('stops', []) if s.get('is_candidate')), None
        )
        if candidate_sim_stop is not None:
            record['gallons_if_chosen']   = candidate_sim_stop['gallons_bought']
            record['fuel_cost_if_chosen'] = candidate_sim_stop['fuel_cost']

        record['stop_analysis'] = analysis
        return record

    # ---- normal path: simple substitution (no simulation) ------------------
    # Swap this candidate for the nearest selected stop and recompute total.
    # This is a fast approximation — only used when stop_analysis=False.
    candidate_cost = round(fuel_cost_if_chosen + node.detour_fuel_cost, 2)
    raw_total      = dp_total - nearest_selected_cost + candidate_cost + prefix_cost
    record['total_fuel_cost_if_chosen'] = round(raw_total, 2)
    record['delta_total_fuel_cost']     = round(raw_total - dp_total, 2)
    record['is_feasible']               = True
    return record


# -----------------------------------------------------------------------------
# Public entry point — called from plan_trip in routing.py
# -----------------------------------------------------------------------------

@profiled("build_debug_station_records")
def build_debug_station_records(
    nodes:                list,
    stops:                list[dict],
    total_cost:           float,
    start_fuel:           float,
    total_distance_miles: float = 0.0,
    stop_analysis:        bool  = False,
) -> list[dict]:
    selected_ids    = {
        s.get('station', {}).get('opis_id')
        for s in stops
        if s.get('station', {}).get('opis_id')
    }
    records_by_opis = {}

    for node in nodes:
        record = _debug_station_record(
            node, stops, total_cost, start_fuel,
            total_distance_miles, run_analysis=stop_analysis,
        )
        if record is None:
            continue
        opis_id = record['station']['opis_id']
        if opis_id in selected_ids:
            continue
        records_by_opis.setdefault(opis_id, record)

    return sorted(records_by_opis.values(), key=lambda r: (
        r['distance_traveled'],
        r['station']['price_per_gallon'],
        r['station']['opis_id'],
    ))