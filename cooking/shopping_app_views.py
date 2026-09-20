"""Tryb zakupów offline: powłoka aplikacji, service worker, manifest i API.

Powłoka (strona) nie zawiera żadnych danych - dane przychodzą z API i są
trzymane w telefonie (IndexedDB), więc strona może być zapisana przez
service workera i otwierać się bez połączenia z domowym serwerem.
"""
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.views import View

from .constants import PANTRY_CATEGORY_GROUPS, PANTRY_CATEGORY_OTHER
from .models import PANTRY_UNIT_CHOICES, ShoppingList
from .services.polish import completion_message
from .services.shopping_sync import (
    apply_operations,
    complete_shopping_list,
    prune_operations,
    shopping_snapshot,
)

APP_STATIC_FILES = [
    'css/tokens.css',
    'vendor/bootstrap-icons/bootstrap-icons.css',
    'vendor/bootstrap-icons/fonts/bootstrap-icons.woff2',
    'vendor/bootstrap-icons/fonts/bootstrap-icons.woff',
    'cooking/css/shopping-app.css',
    'cooking/js/shopping-app.js',
    'cooking/icons/shopping-192.png',
    'cooking/icons/shopping-512.png',
    'cooking/icons/shopping-maskable-512.png',
    'cooking/icons/apple-touch-icon.png',
]
APP_TEMPLATES = ['cooking/shopping_app.html', 'cooking/shopping_app_sw.js']


def _file_digest(paths):
    digest = hashlib.sha256()
    for path in paths:
        if path and Path(path).exists():
            digest.update(Path(path).read_bytes())
    return digest.hexdigest()[:16]


def _compute_app_version():
    static_paths = [finders.find(name) for name in APP_STATIC_FILES]
    template_dir = Path(__file__).resolve().parent / 'templates'
    template_paths = [template_dir / name for name in APP_TEMPLATES]
    return _file_digest([*static_paths, *template_paths])


_cached_app_version = lru_cache(maxsize=1)(_compute_app_version)


def app_version():
    """Wersja aplikacji = skrót zawartości plików. Zmiana dowolnego pliku
    powłoki zmienia service workera, więc telefon pobiera nową wersję przy
    pierwszym otwarciu w domu. W DEBUG liczona na bieżąco."""
    return _compute_app_version() if settings.DEBUG else _cached_app_version()


class ShoppingAppView(View):
    """Powłoka trybu zakupów. Bez logowania - nie zawiera danych."""

    def get(self, request):
        response = render(request, 'cooking/shopping_app.html', {
            'app_version': app_version(),
            'app_config': {
                'version': app_version(),
                'snapshotUrl': reverse('cooking:shopping-api-snapshot'),
                'syncUrl': reverse('cooking:shopping-api-sync'),
                'completeUrl': reverse('cooking:shopping-api-complete', args=[0]),
                'serviceWorkerUrl': reverse('cooking:shopping-app-sw'),
                'scope': reverse('cooking:shopping-app'),
                'loginUrl': f"{reverse('login')}?next={reverse('cooking:shopping-app')}",
                'listsUrl': reverse('cooking:shopping-list'),
                'units': [{'value': value, 'label': label} for value, label in PANTRY_UNIT_CHOICES],
                'categoryGroups': [
                    {'label': label, 'categories': list(categories)}
                    for label, categories in PANTRY_CATEGORY_GROUPS
                ] + [{'label': '', 'categories': [PANTRY_CATEGORY_OTHER]}],
            },
        })
        response['Cache-Control'] = 'no-cache'
        return response


class ShoppingAppServiceWorkerView(View):
    def get(self, request):
        version = app_version()
        precache = [reverse('cooking:shopping-app'), reverse('cooking:shopping-app-manifest')]
        precache += [static(name) for name in APP_STATIC_FILES]
        body = render_to_string('cooking/shopping_app_sw.js', {
            'version': version,
            'precache_json': json.dumps(precache),
            'shell_url_json': json.dumps(reverse('cooking:shopping-app')),
            'api_prefix_json': json.dumps(reverse('cooking:shopping-app') + 'api/'),
        })
        response = HttpResponse(body, content_type='application/javascript; charset=utf-8')
        # Przeglądarka i tak sprawdza service workera co najwyżej co 24 h;
        # no-cache sprawia, że po wdrożeniu nowa wersja przychodzi od razu.
        response['Cache-Control'] = 'no-cache'
        return response


class ShoppingAppManifestView(View):
    def get(self, request):
        scope_url = reverse('cooking:shopping-app')
        manifest = {
            'id': scope_url,
            'name': 'Lista zakupów',
            'short_name': 'Zakupy',
            'description': 'Domowa lista zakupów, działa także bez połączenia z domową siecią.',
            'lang': 'pl',
            'start_url': scope_url,
            # Zakres całej strony: logowanie otwiera się w oknie aplikacji,
            # a nie w osobnej przeglądarce z innym zestawem ciasteczek.
            'scope': '/',
            'display': 'standalone',
            'background_color': '#f4f6f8',
            'theme_color': '#087443',
            'icons': [
                {'src': static('cooking/icons/shopping-192.png'), 'sizes': '192x192', 'type': 'image/png'},
                {'src': static('cooking/icons/shopping-512.png'), 'sizes': '512x512', 'type': 'image/png'},
                {
                    'src': static('cooking/icons/shopping-maskable-512.png'),
                    'sizes': '512x512',
                    'type': 'image/png',
                    'purpose': 'maskable',
                },
            ],
        }
        response = HttpResponse(
            json.dumps(manifest, ensure_ascii=False),
            content_type='application/manifest+json; charset=utf-8',
        )
        response['Cache-Control'] = 'no-cache'
        return response


class ShoppingApiMixin:
    """API dla telefonu: bez przekierowań na stronę logowania.

    Aplikacja rozpoznaje 401 i prosi o zalogowanie, zachowując kolejkę zmian.
    Każde udane połączenie przedłuża sesję (najwyżej raz dziennie), więc
    telefon używany w domu przynajmniej raz na dwa tygodnie nie wylogowuje się.
    """

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {'ok': False, 'error': 'auth', 'message': 'Zaloguj się, aby zsynchronizować listę.'},
                status=401,
            )
        today = timezone.localdate().isoformat()
        if request.session.get('shopping_app_seen') != today:
            request.session['shopping_app_seen'] = today
        return super().dispatch(request, *args, **kwargs)

    def payload(self, request, **extra):
        data = {
            'ok': True,
            'user': request.user.get_username(),
            'csrf_token': get_token(request),
            'app_version': app_version(),
            'snapshot': shopping_snapshot(),
        }
        data.update(extra)
        response = JsonResponse(data)
        response['Cache-Control'] = 'no-store'
        return response

    def read_json(self, request):
        try:
            data = json.loads(request.body or b'{}')
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError('Nie udało się odczytać danych z telefonu.') from None
        if not isinstance(data, dict):
            raise ValueError('Dane z telefonu mają nieprawidłowy format.')
        return data


class ShoppingSnapshotApiView(ShoppingApiMixin, View):
    def get(self, request):
        return self.payload(request)


class ShoppingSyncApiView(ShoppingApiMixin, View):
    def post(self, request):
        try:
            data = self.read_json(request)
            results = apply_operations(data.get('ops', []), request.user)
        except ValueError as exc:
            return JsonResponse({'ok': False, 'error': 'invalid', 'message': str(exc)}, status=400)
        if results:
            prune_operations()
        return self.payload(request, results=results)


class ShoppingCompleteApiView(ShoppingApiMixin, View):
    """Zakończenie listy - tylko z połączeniem (telefon nie kolejkuje go)."""

    def post(self, request, list_id):
        with transaction.atomic():
            shopping_list = ShoppingList.objects.select_for_update().filter(pk=list_id).first()
            if shopping_list is None:
                return JsonResponse({'ok': False, 'error': 'missing', 'message': 'Lista została usunięta.'}, status=404)
            if shopping_list.status == ShoppingList.COMPLETED:
                return self.payload(request, message=f'Lista „{shopping_list.title}” była już zakończona.')
            try:
                added, already, errors = complete_shopping_list(shopping_list, user=request.user)
            except ValueError as exc:
                transaction.set_rollback(True)
                errors = [str(exc)]
        if errors:
            return JsonResponse({'ok': False, 'error': 'invalid', 'message': ' '.join(errors[:5])}, status=400)
        return self.payload(request, message=completion_message(added, already))
