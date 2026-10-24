"""Aktywność fizyczna: strona, service worker (offline) i API dla przeglądarki.

Strona nie zawiera danych - przychodzą z API i są trzymane w telefonie
(IndexedDB), a service worker zapisuje stronę i jej pliki. Dzięki temu
wykresy i dodawanie treningu działają bez połączenia z domowym serwerem,
a zmiany wysyłają się po powrocie do domu (jak tryb zakupów w cooking).
"""
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.staticfiles import finders
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from .models import Workout
from .services import garmin
from .services.workouts import activity_snapshot, apply_operations

# Pliki potrzebne stronie bez połączenia: base.html + własne. Wymagane muszą
# się zapisać przy instalacji service workera, reszta dochodzi w tle.
APP_REQUIRED_STATIC_FILES = [
    'css/tokens.css',
    'vendor/bootstrap/bootstrap.min.css',
    'css/ui.css',
    'css/app.css',
    'vendor/chart/chart.umd.js',
    'js/charts.js',
    'habits/css/activity.css',
    'habits/js/activity-stats.js',
    'habits/js/activity-app.js',
]
APP_STATIC_FILES = APP_REQUIRED_STATIC_FILES + [
    'css/style.css',
    'vendor/bootstrap-icons/bootstrap-icons.css',
    'vendor/bootstrap-icons/fonts/bootstrap-icons.woff2',
    'vendor/bootstrap-icons/fonts/bootstrap-icons.woff',
    'vendor/flatpickr/flatpickr.min.css',
    'vendor/flatpickr/flatpickr.min.js',
    'vendor/flatpickr/pl.js',
    'vendor/bootstrap/bootstrap.bundle.min.js',
    'js/app-actions.js',
]
TEMPLATE_DIRS = [
    Path(__file__).resolve().parent / 'templates' / 'habits',
    Path(settings.BASE_DIR) / 'templates',
]
APP_TEMPLATES = [
    TEMPLATE_DIRS[0] / 'activity.html',
    TEMPLATE_DIRS[0] / 'activity_sw.js',
    TEMPLATE_DIRS[1] / 'base.html',
]


def _compute_app_version():
    digest = hashlib.sha256()
    paths = [finders.find(name) for name in APP_STATIC_FILES] + APP_TEMPLATES
    for path in paths:
        if path and Path(path).exists():
            digest.update(Path(path).read_bytes())
    return digest.hexdigest()[:16]


_cached_app_version = lru_cache(maxsize=1)(_compute_app_version)


def app_version():
    """Skrót zawartości plików strony: każde wdrożenie zmienia service workera,
    więc telefon pobiera nową wersję przy pierwszym otwarciu w domu."""
    return _compute_app_version() if settings.DEBUG else _cached_app_version()


@method_decorator(login_required, name='dispatch')
class ActivityView(View):
    def get(self, request):
        response = render(request, 'habits/activity.html', {
            'app_config': {
                'version': app_version(),
                'snapshotUrl': reverse('habits:api-snapshot'),
                'syncUrl': reverse('habits:api-sync'),
                'garminUrl': reverse('habits:api-garmin'),
                'serviceWorkerUrl': reverse('habits:sw'),
                'scope': reverse('habits:index'),
                'loginUrl': f"{reverse('login')}?next={reverse('habits:index')}",
                'kinds': [{'value': value, 'label': label} for value, label in Workout.KIND_CHOICES],
            },
        })
        response['Cache-Control'] = 'no-cache'
        return response


class ActivityServiceWorkerView(View):
    def get(self, request):
        scope = reverse('habits:index')
        precache = [scope] + [static(name) for name in APP_STATIC_FILES]
        required = [scope] + [static(name) for name in APP_REQUIRED_STATIC_FILES]
        body = render_to_string('habits/activity_sw.js', {
            'version': app_version(),
            'precache_json': json.dumps(precache),
            'required_json': json.dumps(required),
            'shell_url_json': json.dumps(scope),
            'api_prefix_json': json.dumps(scope + 'api/'),
        })
        response = HttpResponse(body, content_type='application/javascript; charset=utf-8')
        response['Cache-Control'] = 'no-cache'
        return response


class ActivityApiMixin:
    """API bez przekierowań na logowanie: strona rozpoznaje 401 i zachowuje
    kolejkę zmian do czasu zalogowania."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {'ok': False, 'error': 'auth', 'message': 'Zaloguj się, aby zsynchronizować treningi.'},
                status=401,
            )
        return super().dispatch(request, *args, **kwargs)

    def payload(self, request, **extra):
        data = {
            'ok': True,
            'user': request.user.get_username(),
            'csrf_token': get_token(request),
            'app_version': app_version(),
            'snapshot': activity_snapshot(request.user),
        }
        data.update(extra)
        response = JsonResponse(data)
        response['Cache-Control'] = 'no-store'
        return response


class SnapshotApiView(ActivityApiMixin, View):
    def get(self, request):
        return self.payload(request)


class SyncApiView(ActivityApiMixin, View):
    def post(self, request):
        try:
            data = json.loads(request.body or b'{}')
            if not isinstance(data, dict):
                raise ValueError
            results = apply_operations(data.get('ops', []), request.user)
        except (ValueError, UnicodeDecodeError):
            return JsonResponse(
                {'ok': False, 'error': 'invalid', 'message': 'Nie udało się odczytać zmian z telefonu.'},
                status=400,
            )
        return self.payload(request, results=results)


class GarminSyncApiView(ActivityApiMixin, View):
    """Przycisk „Pobierz z Garmina” - tylko z połączeniem."""

    def post(self, request):
        try:
            added = garmin.sync_user(request.user)
        except garmin.GarminError as exc:
            return JsonResponse({'ok': False, 'error': 'garmin', 'message': str(exc)}, status=502)
        if added == 1:
            message = 'Pobrano 1 nowy trening z Garmina.'
        elif added:
            message = f'Pobrano nowe treningi z Garmina: {added}.'
        else:
            message = 'Brak nowych treningów w Garminie.'
        return self.payload(request, message=message)
