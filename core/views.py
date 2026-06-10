import json

import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .routing import RouteError, plan_trip


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

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return _cors_response(JsonResponse({'error': 'Request body must be valid JSON.'}, status=400))

    start = payload.get('start_location')
    finish = payload.get('finish_location')
    if not start or not finish:
        return _cors_response(JsonResponse({
            'error': 'start_location and finish_location are required.'
        }, status=400))

    try:
        return _cors_response(JsonResponse(plan_trip(start, finish)))
    except RouteError as exc:
        return _cors_response(JsonResponse({'error': str(exc)}, status=400))
    except requests.HTTPError as exc:
        message = exc.response.text if exc.response is not None else str(exc)
        return _cors_response(JsonResponse({'error': 'External map request failed.', 'details': message}, status=502))
    except requests.RequestException as exc:
        return _cors_response(JsonResponse({'error': 'External map request failed.', 'details': str(exc)}, status=502))
