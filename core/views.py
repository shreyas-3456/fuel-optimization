import json

import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .profiling import profile_step
from .routing import (
    RouteError,
    START_MODE_NEAREST,
    START_MODE_PARTIAL,
    START_FUEL_FIXED_GALLONS,
    TANK_CAPACITY,
    plan_trip,
)

VALID_START_MODES = {START_MODE_NEAREST, START_MODE_PARTIAL}


def _cors_response(response):
    response['Access-Control-Allow-Origin'] = '*'
    response['Access-Control-Allow-Headers'] = 'Content-Type'
    response['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
    return response


@csrf_exempt
def api_route_fuel(request):
    if request.method == 'OPTIONS':
        return _cors_response(JsonResponse({}))

    if request.method != 'POST':
        return _cors_response(JsonResponse({'error': 'POST is required.'}, status=405))

    with profile_step("api_route_fuel.parse_json"):
        try:
            payload = json.loads(request.body.decode('utf-8') or '{}')
        except json.JSONDecodeError:
            return _cors_response(JsonResponse({'error': 'Request body must be valid JSON.'}, status=400))

    with profile_step("api_route_fuel.validate_payload"):
        start  = payload.get('start_location')
        finish = payload.get('finish_location')
        if not start or not finish:
            return _cors_response(JsonResponse({
                'error': 'start_location and finish_location are required.'
            }, status=400))

        # ── start_mode ────────────────────────────────────────────────────────────
        start_mode = payload.get('start_mode', START_MODE_NEAREST)
        if start_mode not in VALID_START_MODES:
            return _cors_response(JsonResponse({
                'error': f'start_mode must be one of: {sorted(VALID_START_MODES)}'
            }, status=400))

        # ── start_fuel_gallons ────────────────────────────────────────────────────
        start_fuel_gallons = None
        if start_mode == START_MODE_PARTIAL:
            raw = payload.get('start_fuel_gallons')
            if raw is not None:
                try:
                    start_fuel_gallons = float(raw)
                except (TypeError, ValueError):
                    return _cors_response(JsonResponse({
                        'error': 'start_fuel_gallons must be a number.'
                    }, status=400))

                if not (0 < start_fuel_gallons <= TANK_CAPACITY):
                    return _cors_response(JsonResponse({
                        'error': (
                            f'start_fuel_gallons must be between 0 and '
                            f'{TANK_CAPACITY} (tank capacity).'
                        )
                    }, status=400))

        # ── stop_analysis ─────────────────────────────────────────────────────────
        # Debug API call disabled. (Decoupling approach is complex and flawed better approach is just create a new graph for each  debug point append or replace  TODO later )
        # stop_analysis = request.GET.get('stop_analysis', 'false').lower() == 'true'
        stop_analysis = False

    try:
        with profile_step("api_route_fuel.plan_trip_call"):
            result = plan_trip(
                start,
                finish,
                start_mode=start_mode,
                start_fuel_gallons=start_fuel_gallons,
                stop_analysis=stop_analysis,
            )
        return _cors_response(JsonResponse(result))
    except RouteError as exc:
        return _cors_response(JsonResponse({'error': str(exc)}, status=400))
    except requests.HTTPError as exc:
        message = exc.response.text if exc.response is not None else str(exc)
        return _cors_response(JsonResponse({'error': 'External map request failed.', 'details': message}, status=502))
    except requests.RequestException as exc:
        return _cors_response(JsonResponse({'error': 'External map request failed.', 'details': str(exc)}, status=502))
