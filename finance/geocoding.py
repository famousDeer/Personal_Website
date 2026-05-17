import json
import logging
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings

logger = logging.getLogger(__name__)

NOMINATIM_SEARCH_URL = 'https://nominatim.openstreetmap.org/search'
COORDINATE_PRECISION = Decimal('0.000001')


def _decimal_coordinate(value):
    try:
        return Decimal(str(value)).quantize(COORDINATE_PRECISION)
    except (InvalidOperation, TypeError, ValueError):
        return None


def geocode_destination(city, country_name, country_code=None):
    if not getattr(settings, 'TRAVEL_GEOCODING_ENABLED', True):
        return None

    query_parts = [part for part in [city, country_name or country_code] if part]
    if not query_parts:
        return None

    params = {
        'q': ', '.join(query_parts),
        'format': 'jsonv2',
        'limit': 1,
    }
    if country_code:
        params['countrycodes'] = str(country_code).lower()

    request = Request(
        f'{NOMINATIM_SEARCH_URL}?{urlencode(params)}',
        headers={
            'Accept': 'application/json',
            'User-Agent': getattr(
                settings,
                'TRAVEL_GEOCODING_USER_AGENT',
                'Website-Finance travel map geocoder',
            ),
        },
    )

    try:
        timeout = getattr(settings, 'TRAVEL_GEOCODING_TIMEOUT', 3)
        with urlopen(request, timeout=timeout) as response:
            results = json.loads(response.read().decode('utf-8'))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning('Travel geocoding failed for %s: %s', params['q'], exc)
        return None

    if not results:
        return None

    latitude = _decimal_coordinate(results[0].get('lat'))
    longitude = _decimal_coordinate(results[0].get('lon'))
    if latitude is None or longitude is None:
        return None

    return latitude, longitude


def populate_destination_coordinates(destination, force=False):
    if force:
        destination.latitude = None
        destination.longitude = None
    elif destination.has_coordinates:
        return True

    coordinates = geocode_destination(
        destination.city,
        destination.country.name,
        destination.country.code,
    )
    if coordinates is None:
        return False

    destination.latitude, destination.longitude = coordinates
    return True
