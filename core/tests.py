import json
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .routing import _geocode_text_location


class RouteFuelApiTests(SimpleTestCase):
    def setUp(self):
        _geocode_text_location.cache_clear()

    @override_settings(GRAPHHOPPER_API_KEY='test-key')
    @patch('core.routing.requests.get')
    def test_route_fuel_returns_route_stops_and_total(self, mock_get):
        mock_get.side_effect = [
            _mock_response([{
                'display_name': 'Chicago, Illinois, United States',
                'lat': '41.8781',
                'lon': '-87.6298',
                'address': {
                    'country': 'United States',
                    'state': 'Illinois',
                    'ISO3166-2-lvl4': 'US-IL',
                },
            }]),
            _mock_response([{
                'display_name': 'Dallas, Texas, United States',
                'lat': '32.7767',
                'lon': '-96.7970',
                'address': {
                    'country': 'United States',
                    'state': 'Texas',
                    'ISO3166-2-lvl4': 'US-TX',
                },
            }]),
            _mock_response({
                'paths': [{
                    'distance': 1609344,
                    'time': 36000000,
                    'points': {
                        'coordinates': [
                            [-87.6298, 41.8781],
                            [-92.0, 38.0],
                            [-96.7970, 32.7767],
                        ]
                    },
                    'instructions': [{'text': 'Drive south', 'distance': 1609344}],
                }]
            }),
            _mock_reverse_response('Illinois', 'IL', 'Chicago'),
            _mock_reverse_response('Missouri', 'MO', 'Springfield'),
        ]

        response = self.client.post(
            '/api/route-fuel/',
            data=json.dumps({
                'start_location': 'Chicago, IL',
                'finish_location': 'Dallas, TX',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['external_api_calls']['graphhopper_total'], 1)
        self.assertEqual(payload['external_api_calls']['openstreetmap_geocoding'], 2)
        self.assertEqual(payload['external_api_calls']['openstreetmap_reverse_geocoding'], 2)
        self.assertEqual(payload['external_api_calls']['graphhopper_routing'], 1)
        self.assertEqual(payload['vehicle']['max_range_miles'], 500)
        self.assertGreaterEqual(len(payload['fuel_stops']), 2)
        self.assertIn('geojson', payload['route'])
        self.assertGreater(payload['total_fuel_cost'], 0)

    def test_route_fuel_requires_locations(self):
        response = self.client.post('/api/route-fuel/', data='{}', content_type='application/json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('start_location', response.json()['error'])

    @override_settings(GRAPHHOPPER_API_KEY='test-key')
    @patch('core.routing.requests.get')
    def test_route_fuel_uses_one_osm_geocode_when_start_is_coordinates(self, mock_get):
        mock_get.side_effect = [
            _mock_response([{
                'display_name': 'Dallas, Texas, United States',
                'lat': '32.7767',
                'lon': '-96.7970',
                'address': {
                    'country': 'United States',
                    'state': 'Texas',
                    'ISO3166-2-lvl4': 'US-TX',
                },
            }]),
            _mock_response({
                'paths': [{
                    'distance': 1609344,
                    'time': 36000000,
                    'points': {
                        'coordinates': [
                            [-87.6298, 41.8781],
                            [-92.0, 38.0],
                            [-96.7970, 32.7767],
                        ]
                    },
                    'instructions': [{'text': 'Drive south', 'distance': 1609344}],
                }]
            }),
            _mock_reverse_response('Illinois', 'IL', 'Chicago'),
            _mock_reverse_response('Missouri', 'MO', 'Springfield'),
        ]

        response = self.client.post(
            '/api/route-fuel/',
            data=json.dumps({
                'start_location': {'lat': 41.8781, 'lng': -87.6298, 'label': 'Chicago, IL'},
                'finish_location': 'Dallas, TX',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['external_api_calls']['openstreetmap_geocoding'], 1)
        self.assertEqual(payload['external_api_calls']['graphhopper_total'], 1)

    @override_settings(GRAPHHOPPER_API_KEY='test-key')
    @patch('core.routing.requests.get')
    def test_route_fuel_uses_one_osm_geocode_when_finish_is_coordinates(self, mock_get):
        mock_get.side_effect = [
            _mock_response([{
                'display_name': 'Chicago, Illinois, United States',
                'lat': '41.8781',
                'lon': '-87.6298',
                'address': {
                    'country': 'United States',
                    'state': 'Illinois',
                    'ISO3166-2-lvl4': 'US-IL',
                },
            }]),
            _mock_response({
                'paths': [{
                    'distance': 1609344,
                    'time': 36000000,
                    'points': {
                        'coordinates': [
                            [-87.6298, 41.8781],
                            [-92.0, 38.0],
                            [-96.7970, 32.7767],
                        ]
                    },
                    'instructions': [{'text': 'Drive south', 'distance': 1609344}],
                }]
            }),
            _mock_reverse_response('Illinois', 'IL', 'Chicago'),
            _mock_reverse_response('Missouri', 'MO', 'Springfield'),
        ]

        response = self.client.post(
            '/api/route-fuel/',
            data=json.dumps({
                'start_location': 'Chicago, IL',
                'finish_location': {'lat': 32.7767, 'lng': -96.7970, 'label': 'Dallas, TX'},
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['external_api_calls']['openstreetmap_geocoding'], 1)
        self.assertEqual(payload['external_api_calls']['graphhopper_total'], 1)

    @override_settings(GRAPHHOPPER_API_KEY='test-key')
    @patch('core.routing.requests.get')
    def test_route_fuel_skips_osm_geocode_when_both_locations_are_coordinates(self, mock_get):
        mock_get.return_value = _mock_response({
            'paths': [{
                'distance': 1609344,
                'time': 36000000,
                'points': {
                    'coordinates': [
                        [-87.6298, 41.8781],
                        [-92.0, 38.0],
                        [-96.7970, 32.7767],
                    ]
                },
                'instructions': [{'text': 'Drive south', 'distance': 1609344}],
            }]
        })
        mock_get.side_effect = [
            _mock_response({
                'paths': [{
                    'distance': 1609344,
                    'time': 36000000,
                    'points': {
                        'coordinates': [
                            [-87.6298, 41.8781],
                            [-92.0, 38.0],
                            [-96.7970, 32.7767],
                        ]
                    },
                    'instructions': [{'text': 'Drive south', 'distance': 1609344}],
                }]
            }),
            _mock_reverse_response('Illinois', 'IL', 'Chicago'),
            _mock_reverse_response('Missouri', 'MO', 'Springfield'),
        ]

        response = self.client.post(
            '/api/route-fuel/',
            data=json.dumps({
                'start_location': {'lat': 41.8781, 'lng': -87.6298, 'label': 'Chicago, IL'},
                'finish_location': {'lat': 32.7767, 'lng': -96.7970, 'label': 'Dallas, TX'},
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['external_api_calls']['openstreetmap_geocoding'], 0)
        self.assertEqual(payload['external_api_calls']['graphhopper_total'], 1)
        self.assertEqual(payload['finish']['state'], 'Texas')
        self.assertEqual(payload['finish']['state_code'], 'TX')

    def test_route_fuel_allows_cors_preflight(self):
        response = self.client.options('/api/route-fuel/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Access-Control-Allow-Origin'], '*')
        self.assertIn('OPTIONS', response['Access-Control-Allow-Methods'])


class _mock_response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _mock_reverse_response(state, state_code, city):
    return _mock_response({
        'address': {
            'country': 'United States',
            'state': state,
            'ISO3166-2-lvl4': f'US-{state_code}',
            'city': city,
        }
    })
