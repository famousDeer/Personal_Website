"""Aktywność fizyczna: strona, service worker (offline) i API dla przeglądarki.

Strona nie zawiera danych - przychodzą z API i są trzymane w telefonie
(IndexedDB), a service worker zapisuje stronę i jej pliki. Dzięki temu
wykresy i dodawanie treningu działają bez połączenia z domowym serwerem,
a zmiany wysyłają się po powrocie do domu (jak tryb zakupów w cooking).
"""
import hashlib
import json
import time
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.staticfiles import finders
from django.http import Http404, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from .models import ActivitySettings, Workout, WorkoutRoute
from .services import garmin, hevy
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
    'habits/js/activity-detail.js',
    'habits/js/activity-health.js',
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
    # Mapa trasy - sam podkład (kafelki OSM) i tak wymaga internetu.
    'vendor/leaflet/leaflet.js',
    'vendor/leaflet/leaflet.css',
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
                'refreshUrl': reverse('habits:api-refresh'),
                'routeUrl': reverse('habits:api-route', args=['UID']),
                'connectionsUrl': reverse('habits:connections'),
                'leafletJs': static('vendor/leaflet/leaflet.js'),
                'leafletCss': static('vendor/leaflet/leaflet.css'),
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


class RefreshApiView(ActivityApiMixin, View):
    """Przycisk „Pobierz teraz” na stronie: Garmin (treningi, dodatki, zdrowie)
    i Hevy - to, co osoba ma połączone. Tylko z połączeniem."""

    def post(self, request):
        settings_row = ActivitySettings.for_user(request.user)
        notes, errors = [], []
        if settings.GARMIN_ENABLED and garmin.is_connected(request.user):
            try:
                added = garmin.sync_all(request.user, force_health=True, **garmin.INTERACTIVE)
                notes.append(f'Garmin: nowe treningi {added}.' if added else 'Garmin: brak nowych treningów.')
            except garmin.GarminError as exc:
                errors.append(f'Garmin: {exc}')
        if settings.HEVY_ENABLED and settings_row.hevy_api_key:
            try:
                added = hevy.sync_user(request.user)
                notes.append(f'Hevy: nowe treningi {added}.' if added else 'Hevy: brak nowych treningów.')
            except hevy.HevyError as exc:
                errors.append(f'Hevy: {exc}')
        if not notes and not errors:
            return JsonResponse({'ok': False, 'error': 'none', 'message': 'Brak połączonych kont – połącz Garmina albo Hevy.'}, status=400)
        if errors and not notes:
            return JsonResponse({'ok': False, 'error': 'sync', 'message': ' '.join(errors)}, status=502)
        return self.payload(request, message=' '.join(notes + errors))


class RouteApiView(ActivityApiMixin, View):
    """Trasa GPS treningu - poza snapshotem, bo bez internetu i tak nie ma mapy."""

    def get(self, request, uid):
        route = WorkoutRoute.objects.filter(workout__uid=uid, workout__user=request.user).first()
        if route is None:
            raise Http404
        response = JsonResponse({'ok': True, 'points': route.points})
        response['Cache-Control'] = 'private, max-age=86400'
        return response


MFA_SESSION_KEY = 'garmin_mfa'
MFA_TTL_SECONDS = 10 * 60
LOCAL_HOSTS = {'localhost', '127.0.0.1', '[::1]', 'testserver'}


@method_decorator(login_required, name='dispatch')
class ConnectionsView(View):
    """Połączenia: Garmin Connect i Hevy - każdy domownik łączy własne konta.

    Hasło do Garmina idzie tylko do Garmina i nie jest zapisywane. Gdy konto ma
    2FA, stan logowania (ciasteczka sesji Garmina) czeka w sesji Django najwyżej
    10 minut, do wpisania kodu. Klucz API Hevy jest zapisany w bazie.
    Strona działa tylko z połączeniem (service worker jej nie zapisuje).
    """

    def pending_mfa(self, request):
        pending = request.session.get(MFA_SESSION_KEY)
        if pending and time.time() - pending.get('at', 0) > MFA_TTL_SECONDS:
            del request.session[MFA_SESSION_KEY]
            messages.warning(request, 'Minęło 10 minut bez kodu – zaloguj się do Garmina jeszcze raz.')
            return None
        return pending

    def get(self, request):
        settings_row = ActivitySettings.for_user(request.user)
        host = request.get_host().split(':')[0]
        return render(request, 'habits/connections.html', {
            'garmin': garmin.status(request.user, settings_row),
            'hevy': hevy.status(request.user, settings_row),
            'hevy_key_hint': settings_row.hevy_api_key[-4:] if settings_row.hevy_api_key else '',
            'waiting_for_code': bool(self.pending_mfa(request)),
            'insecure': not request.is_secure() and host not in LOCAL_HOSTS,
            'back_url': reverse('habits:index'),
        })

    def post(self, request):
        handler = {
            'garmin-login': self.garmin_login,
            'garmin-code': self.garmin_code,
            'garmin-cancel': self.garmin_cancel,
            'garmin-sync': self.garmin_sync,
            'garmin-disconnect': self.garmin_disconnect,
            'hevy-key': self.hevy_key,
            'hevy-sync': self.hevy_sync,
            'hevy-disconnect': self.hevy_disconnect,
            'hevy-csv': self.hevy_csv,
        }.get(request.POST.get('action'))
        if handler is None:
            return redirect('habits:connections')
        if request.POST['action'].startswith('garmin') and not settings.GARMIN_ENABLED:
            messages.error(request, 'Połączenie z Garminem jest wyłączone (GARMIN_ENABLED).')
        elif request.POST['action'] in ('hevy-key', 'hevy-sync') and not settings.HEVY_ENABLED:
            messages.error(request, 'Pobieranie z Hevy jest wyłączone (HEVY_ENABLED).')
        else:
            handler(request)
        return redirect('habits:connections')

    # --- Garmin ---
    def garmin_connected(self, request):
        garmin.start_import(request.user)
        messages.success(
            request,
            'Połączono z Garmin Connect. Pobieram treningi z ostatniego roku i dane zdrowotne z 30 dni – '
            'to może potrwać kilka minut, potem pojawią się na stronie Aktywność. Nowe dane dochodzą same co godzinę.',
        )

    def garmin_login(self, request):
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        if not email or not password:
            messages.error(request, 'Podaj e-mail i hasło do Garmin Connect.')
            return
        try:
            state = garmin.start_login(request.user, email, password)
        except garmin.GarminError as exc:
            messages.error(request, str(exc))
            return
        if state is None:
            self.garmin_connected(request)
        else:
            request.session[MFA_SESSION_KEY] = {'state': state, 'at': time.time()}
            messages.info(request, 'Garmin wysłał kod weryfikacyjny – wpisz go poniżej.')

    def garmin_code(self, request):
        pending = self.pending_mfa(request)
        code = ''.join(request.POST.get('code', '').split())
        if not pending:
            return
        if not code:
            messages.error(request, 'Wpisz kod z wiadomości od Garmina.')
            return
        try:
            garmin.finish_login(request.user, pending['state'], code)
        except garmin.GarminError as exc:
            # Stan logowania bywa jednorazowy - po błędzie zaczynamy od nowa.
            request.session.pop(MFA_SESSION_KEY, None)
            messages.error(request, f'{exc} Zaloguj się jeszcze raz.')
            return
        request.session.pop(MFA_SESSION_KEY, None)
        self.garmin_connected(request)

    def garmin_cancel(self, request):
        request.session.pop(MFA_SESSION_KEY, None)

    def garmin_sync(self, request):
        try:
            added = garmin.sync_all(request.user, force_health=True, **garmin.INTERACTIVE)
        except garmin.GarminError as exc:
            messages.error(request, str(exc))
            return
        messages.success(request, f'Pobrano nowe treningi: {added}.' if added else 'Brak nowych treningów w Garminie.')

    def garmin_disconnect(self, request):
        garmin.disconnect(request.user)
        messages.success(request, 'Rozłączono konto Garmin. Pobrane treningi zostały na stronie.')

    # --- Hevy ---
    def hevy_key(self, request):
        api_key = request.POST.get('api_key', '').strip()
        if not api_key:
            messages.error(request, 'Wklej klucz API z hevy.com/settings?developer.')
            return
        try:
            hevy.connect(request.user, api_key)
        except hevy.HevyError as exc:
            messages.error(request, str(exc))
            return
        hevy.start_import(request.user)
        messages.success(request, 'Połączono z Hevy. Pobieram historię treningów – za chwilę pojawią się na stronie Aktywność.')

    def hevy_sync(self, request):
        try:
            added = hevy.sync_user(request.user)
        except hevy.HevyError as exc:
            messages.error(request, str(exc))
            return
        messages.success(request, f'Pobrano nowe treningi z Hevy: {added}.' if added else 'Brak nowych treningów w Hevy.')

    def hevy_disconnect(self, request):
        hevy.disconnect(request.user)
        messages.success(request, 'Usunięto klucz Hevy. Pobrane treningi zostały na stronie.')

    def hevy_csv(self, request):
        uploaded = request.FILES.get('file')
        if uploaded is None:
            messages.error(request, 'Wybierz plik CSV wyeksportowany z Hevy.')
            return
        try:
            created, updated = hevy.import_csv(request.user, uploaded)
        except hevy.HevyError as exc:
            messages.error(request, str(exc))
            return
        messages.success(request, f'Zaimportowano z pliku: nowe treningi {created}, zaktualizowane {updated}.')
