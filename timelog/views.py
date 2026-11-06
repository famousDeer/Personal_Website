import datetime
import json
import secrets

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from utils.navigation import redirect_back
from utils.undo import delete_with_undo

from . import services
from .models import Category, Entry, TimelogSettings

# Ikony do wyboru w kategorii, w grupach: (klasa, podpis dla czytnika i dymka).
# Klasy bi-* są z Bootstrap Icons; tl-icon-* to własne rysunki (pies, łapa)
# z shared.css - tego zestaw Bootstrapa nie ma.
ICON_GROUPS = [
    ('Praca i nauka', [
        ('bi-briefcase', 'Teczka'), ('bi-laptop', 'Laptop'), ('bi-person-workspace', 'Biurko'),
        ('bi-code-slash', 'Kod'), ('bi-terminal', 'Terminal'), ('bi-kanban', 'Zadania'),
        ('bi-list-check', 'Lista zadań'), ('bi-envelope', 'Poczta'), ('bi-calendar-event', 'Spotkanie'),
        ('bi-graph-up', 'Wykres'), ('bi-book', 'Książka'), ('bi-journal-text', 'Notatki'),
        ('bi-mortarboard', 'Nauka'), ('bi-translate', 'Języki'), ('bi-easel', 'Tablica'),
        ('bi-lightbulb', 'Pomysł'),
    ]),
    ('Dom i sprawy', [
        ('bi-house', 'Dom'), ('bi-house-heart', 'Rodzinny dom'), ('bi-bucket', 'Sprzątanie'),
        ('bi-droplet', 'Higiena'), ('bi-scissors', 'Fryzjer'), ('bi-hammer', 'Młotek'),
        ('bi-tools', 'Narzędzia'), ('bi-wrench', 'Naprawy'), ('bi-basket', 'Zakupy'),
        ('bi-cart3', 'Wózek'), ('bi-bag', 'Torba'), ('bi-cash-coin', 'Pieniądze'),
        ('bi-wallet2', 'Portfel'), ('bi-piggy-bank', 'Skarbonka'),
    ]),
    ('Jedzenie', [
        ('bi-cup-hot', 'Kawa'), ('bi-cup-straw', 'Napój'), ('bi-egg-fried', 'Śniadanie'),
        ('bi-cake2', 'Ciasto'),
    ]),
    ('Ruch i zdrowie', [
        ('bi-activity', 'Puls'), ('bi-heart-pulse', 'Zdrowie'), ('bi-person-walking', 'Spacer'),
        ('bi-person-arms-up', 'Rozciąganie'), ('bi-bicycle', 'Rower'), ('bi-dribbble', 'Piłka'),
        ('bi-water', 'Pływanie'), ('bi-trophy', 'Puchar'), ('bi-lungs', 'Oddech'),
        ('bi-capsule', 'Leki'), ('bi-bandaid', 'Plaster'), ('bi-hospital', 'Lekarz'),
    ]),
    ('Ludzie i zwierzęta', [
        ('bi-people', 'Ludzie'), ('bi-heart', 'Serce'), ('bi-chat-dots', 'Rozmowa'),
        ('bi-gift', 'Prezent'), ('bi-balloon', 'Impreza'), ('bi-emoji-smile', 'Uśmiech'),
        ('tl-icon-dog', 'Pies'), ('tl-icon-paw', 'Łapa'),
    ]),
    ('Odpoczynek', [
        ('bi-moon-stars', 'Noc'), ('bi-cloud-moon', 'Drzemka'), ('bi-sun', 'Słońce'),
        ('bi-tree', 'Drzewo'), ('bi-flower1', 'Ogród'), ('bi-umbrella', 'Parasol'),
        ('bi-music-note', 'Muzyka'), ('bi-headphones', 'Słuchawki'), ('bi-palette', 'Malowanie'),
        ('bi-brush', 'Pędzel'), ('bi-camera', 'Aparat'), ('bi-film', 'Film'),
        ('bi-tv', 'Telewizor'), ('bi-controller', 'Gry'),
    ]),
    ('Telefon i internet', [
        ('bi-phone', 'Telefon'), ('bi-instagram', 'Instagram'), ('bi-youtube', 'YouTube'),
        ('bi-tiktok', 'TikTok'), ('bi-facebook', 'Facebook'), ('bi-reddit', 'Reddit'),
        ('bi-twitch', 'Twitch'), ('bi-spotify', 'Spotify'), ('bi-newspaper', 'Wiadomości'),
        ('bi-globe', 'Internet'),
    ]),
    ('Podróże', [
        ('bi-car-front', 'Auto'), ('bi-fuel-pump', 'Tankowanie'), ('bi-bus-front', 'Autobus'),
        ('bi-train-front', 'Pociąg'), ('bi-airplane', 'Samolot'), ('bi-scooter', 'Hulajnoga'),
        ('bi-backpack', 'Plecak'), ('bi-signpost-2', 'Drogowskaz'),
    ]),
    ('Inne', [
        ('bi-circle', 'Kółko'), ('bi-stars', 'Gwiazdki'), ('bi-fire', 'Ogień'),
        ('bi-flag', 'Flaga'), ('bi-bookmark', 'Zakładka'), ('bi-clock', 'Zegar'),
        ('bi-alarm', 'Budzik'),
    ]),
]
ICONS = dict(icon for _, icons in ICON_GROUPS for icon in icons)


def parse_day(value):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def parse_time(value):
    try:
        return datetime.time.fromisoformat((value or '').strip())
    except ValueError:
        return None


class TimelogView(LoginRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            services.ensure_categories(request.user)
        return super().dispatch(request, *args, **kwargs)

    def categories(self):
        return list(Category.objects.filter(user=self.request.user, is_archived=False))


class DayView(TimelogView):
    """Dziś: stoper z przyciskami kategorii, pasek doby, wpisy i luki."""

    def get(self, request, day=None):
        today = timezone.localdate()
        day = parse_day(day) or today
        if day > today:
            return redirect('timelog:index')
        services.import_automatic(request.user, day - datetime.timedelta(days=1), day + datetime.timedelta(days=1))
        data = services.day_data(request.user, day)
        running = services.running_entry(request.user)
        rows = [{'kind': 'entry', 'at': e.start, 'entry': e} for e in data['entries']]
        rows += [{'kind': 'gap', 'at': start, 'start': start, 'end': end,
                  'seconds': int((end - start).total_seconds())} for start, end in data['gaps']]
        rows.sort(key=lambda row: (row['at'], row['kind'] == 'entry'))
        tracked = data['tracked'] or 1
        return render(request, 'timelog/day.html', {
            'day': day,
            'today': today,
            'is_today': day == today,
            'prev_day': day - datetime.timedelta(days=1),
            'next_day': day + datetime.timedelta(days=1) if day < today else None,
            'running': running,
            'resume_to': services.resume_target(running),
            'categories': self.categories(),
            'rows': rows,
            'data': data,
            'totals': [
                {'category': category, 'seconds': seconds, 'share': round(seconds / tracked * 100, 2)}
                for category, seconds in data['totals']
            ],
        })


class StartView(TimelogView):
    def post(self, request):
        category = get_object_or_404(Category, pk=request.POST.get('category'), user=request.user)
        services.start_timer(request.user, category, note=request.POST.get('note', '').strip()[:200])
        return redirect_back(request, 'timelog:index')


def stopped_message(entry):
    if entry is None:
        return ''
    if entry.resumed is not None:
        return f'Koniec: {entry.category}. Wracasz do: {entry.resumed.category}.'
    return f'Zatrzymano: {entry.category}.' if entry.pk else ''


class StopView(TimelogView):
    """Stop; `resume=1` przy chwilowej kategorii wraca do przerwanej czynności."""

    def post(self, request):
        entry = services.stop_timer(request.user, resume=request.POST.get('resume') == '1')
        message = stopped_message(entry)
        if message:
            messages.success(request, message)
        return redirect_back(request, 'timelog:index')


class BubbleView(View):
    """Stan stopera dla pływającego dymka na każdej stronie (static/timelog/js/bubble.js).

    JSON zamiast danych w szablonie: strona Sport otwiera się najpierw
    z pamięci telefonu, więc stan wpisany w HTML byłby nieaktualny.
    POST z `action` start (z `category`) albo stop (z `resume` - powrót do
    przerwanej czynności, `running.resumeTo`) odpowiada nowym stanem.
    Bez logowania 401 - dymek się wtedy nie pokazuje.
    """

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'ok': False}, status=401)
        services.ensure_categories(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return self.state(request)

    def post(self, request):
        try:
            data = json.loads(request.body or b'{}') if request.content_type == 'application/json' else request.POST
        except ValueError:
            data = {}
        if not hasattr(data, 'get'):
            data = {}
        action = data.get('action')
        if action == 'stop':
            services.stop_timer(request.user, resume=data.get('resume') in (True, 1, '1', 'true'))
        elif action == 'start':
            pk = str(data.get('category', ''))
            category = Category.objects.filter(user=request.user, is_archived=False, pk=pk).first() if pk.isdigit() else None
            if category is None:
                return JsonResponse({'ok': False, 'error': 'Nie ma takiej kategorii.'}, status=400)
            services.start_timer(request.user, category)
        else:
            return JsonResponse({'ok': False, 'error': 'Nieznana akcja.'}, status=400)
        return self.state(request)

    def state(self, request):
        entry = services.running_entry(request.user)
        category = entry.category if entry else None
        previous = services.resume_target(entry)
        response = JsonResponse({
            'ok': True,
            'csrfToken': get_token(request),
            'running': None if entry is None else {
                'category': category.pk, 'name': category.name, 'icon': category.icon,
                'color': category.color, 'since': entry.start.isoformat(), 'note': entry.note,
                'resumeTo': None if previous is None else {
                    'name': previous.category.name, 'icon': previous.category.icon, 'color': previous.category.color,
                },
            },
            'categories': [
                {'id': c.pk, 'name': c.name, 'icon': c.icon, 'color': c.color}
                for c in Category.objects.filter(user=request.user, is_archived=False)
            ],
        })
        response['Cache-Control'] = 'no-store'
        return response


class EntryCreateView(TimelogView):
    """Wpis ręczny albo uzupełnienie luki. Koniec przed początkiem = po północy."""

    def post(self, request):
        category = get_object_or_404(Category, pk=request.POST.get('category'), user=request.user)
        day = parse_day(request.POST.get('date'))
        start_time, end_time = parse_time(request.POST.get('start')), parse_time(request.POST.get('end'))
        if not (day and start_time and end_time):
            messages.error(request, 'Podaj datę oraz godzinę początku i końca.')
            return redirect_back(request, 'timelog:index')
        start = services.local_time(day, start_time)
        end = services.local_time(day, end_time)
        if end <= start:
            end = services.local_time(day + datetime.timedelta(days=1), end_time)
        Entry.objects.create(user=request.user, category=category, start=start, end=end,
                             note=request.POST.get('note', '').strip()[:200], source=Entry.MANUAL)
        messages.success(request, f'Dodano: {category} {timezone.localtime(start):%H:%M}–{timezone.localtime(end):%H:%M}.')
        return redirect_back(request, 'timelog:day', day=day.isoformat())


class EntryEditView(TimelogView):
    def get_entry(self, pk):
        return get_object_or_404(Entry.objects.select_related('category'), pk=pk, user=self.request.user)

    def get(self, request, pk):
        entry = self.get_entry(pk)
        return render(request, 'timelog/entry_form.html', {
            'entry': entry,
            'start': timezone.localtime(entry.start),
            'end': timezone.localtime(entry.end) if entry.end else None,
            'categories': Category.objects.filter(Q(is_archived=False) | Q(pk=entry.category_id), user=request.user),
        })

    def post(self, request, pk):
        entry = self.get_entry(pk)
        category = get_object_or_404(Category, pk=request.POST.get('category'), user=request.user)
        start_day, start_time = parse_day(request.POST.get('start_date')), parse_time(request.POST.get('start_time'))
        end_day, end_time = parse_day(request.POST.get('end_date')), parse_time(request.POST.get('end_time'))
        if not (start_day and start_time):
            messages.error(request, 'Podaj początek wpisu.')
            return redirect('timelog:entry', pk=entry.pk)
        start = services.local_time(start_day, start_time)
        end = services.local_time(end_day or start_day, end_time) if end_time else None
        if end is not None and end <= start:
            messages.error(request, 'Koniec musi być po początku.')
            return redirect('timelog:entry', pk=entry.pk)
        if end is None and Entry.objects.filter(user=request.user, end__isnull=True).exclude(pk=entry.pk).exists():
            messages.error(request, 'Inny wpis już trwa – podaj koniec albo najpierw zatrzymaj stoper.')
            return redirect('timelog:entry', pk=entry.pk)
        entry.category, entry.start, entry.end = category, start, end
        entry.note = request.POST.get('note', '').strip()[:200]
        # Poprawiony ręcznie wpis jest już Twój: import z zegarka go nie
        # nadpisze, a sen go nie przytnie.
        entry.source = Entry.MANUAL
        entry.save()
        messages.success(request, 'Zapisano wpis.')
        return redirect('timelog:day', day=timezone.localdate(start).isoformat())


class EntryDeleteView(TimelogView):
    def post(self, request, pk):
        entry = get_object_or_404(Entry, pk=pk, user=request.user)
        day = timezone.localdate(entry.start)
        if entry.external_id:
            settings = TimelogSettings.for_user(request.user)
            if entry.external_id not in settings.dismissed:
                settings.dismissed = (settings.dismissed + [entry.external_id])[-500:]
                settings.save(update_fields=['dismissed'])
        delete_with_undo(request, entry, f'Usunięto wpis: {entry.category}.')
        return redirect_back(request, 'timelog:day', day=day.isoformat())


class SummaryView(TimelogView):
    """Tydzień albo miesiąc: na co poszedł czas, ile było straty i luk."""

    def get(self, request):
        today = timezone.localdate()
        period = 'month' if request.GET.get('okres') == 'miesiac' else 'week'
        anchor = min(parse_day(request.GET.get('dzien')) or today, today)
        if period == 'week':
            first = anchor - datetime.timedelta(days=anchor.weekday())
            last = first + datetime.timedelta(days=6)
            prev_first = first - datetime.timedelta(days=7)
            prev_last = first - datetime.timedelta(days=1)
            next_anchor = first + datetime.timedelta(days=7)
        else:
            first = anchor.replace(day=1)
            next_anchor = (first + datetime.timedelta(days=32)).replace(day=1)
            last = next_anchor - datetime.timedelta(days=1)
            prev_last = first - datetime.timedelta(days=1)
            prev_first = prev_last.replace(day=1)
        services.import_automatic(request.user, prev_first - datetime.timedelta(days=1), min(last, today) + datetime.timedelta(days=1))
        summary = services.period_summary(request.user, first, last)
        previous = services.period_summary(request.user, prev_first, prev_last)
        days = summary['elapsed_days'] or 1
        prev_days = previous['elapsed_days'] or 1
        prev_totals = {category.pk: seconds for category, seconds in previous['totals'].items()}
        top = max(summary['totals'].values(), default=1)
        ranking = []
        for category, seconds in sorted(summary['totals'].items(), key=lambda item: -item[1]):
            per_day = seconds / days
            prev_per_day = prev_totals.get(category.pk, 0) / prev_days if previous['elapsed_days'] else None
            ranking.append({
                'category': category,
                'seconds': seconds,
                'per_day': per_day,
                'delta': None if prev_per_day is None else per_day - prev_per_day,
                'share': round(seconds / top * 100, 2),
            })
        awake = summary['awake'] or 1
        waste = summary['by_value'].get(Category.WASTE, 0)
        prev_waste = previous['by_value'].get(Category.WASTE, 0)
        return render(request, 'timelog/summary.html', {
            'period': period,
            'first': first,
            'last': last,
            'prev_anchor': prev_first,
            'next_anchor': next_anchor if next_anchor <= today else None,
            'summary': summary,
            'ranking': ranking,
            'per_day': {
                'tracked': summary['tracked'] / days,
                'untracked': summary['untracked'] / days,
                'waste': waste / days,
                'good': summary['by_value'].get(Category.GOOD, 0) / days,
            },
            'waste_share': waste / awake * 100 if summary['awake'] else None,
            'waste_delta': (waste / days - prev_waste / prev_days) if previous['elapsed_days'] else None,
            'hours_scale': range(0, 25, 6),
        })


class CategoriesView(TimelogView):
    def get(self, request):
        return render(request, 'timelog/categories.html', {
            'categories': Category.objects.filter(user=request.user),
            'value_choices': Category.VALUE_CHOICES,
            'auto_choices': Category.AUTO_CHOICES,
            'icon_groups': ICON_GROUPS,
            'colors': range(1, 9),
        })

    def post(self, request):
        action = request.POST.get('action')
        if action == 'add':
            name = request.POST.get('name', '').strip()[:40]
            if not name:
                messages.error(request, 'Podaj nazwę kategorii.')
            elif Category.objects.filter(user=request.user, name__iexact=name).exists():
                messages.error(request, f'Kategoria „{name}” już jest.')
            else:
                last = Category.objects.filter(user=request.user).order_by('-position').first()
                Category.objects.create(
                    user=request.user, name=name, icon=self._icon(request), color=self._color(request),
                    value=self._value(request), position=(last.position + 1) if last else 0,
                    is_interruption=request.POST.get('is_interruption') == '1',
                )
                messages.success(request, f'Dodano kategorię „{name}”.')
            return redirect('timelog:categories')

        category = get_object_or_404(Category, pk=request.POST.get('id'), user=request.user)
        if action == 'save':
            name = request.POST.get('name', '').strip()[:40]
            if name and not Category.objects.filter(user=request.user, name__iexact=name).exclude(pk=category.pk).exists():
                category.name = name
            category.icon, category.color, category.value = self._icon(request), self._color(request), self._value(request)
            category.is_interruption = request.POST.get('is_interruption') == '1'
            auto = request.POST.get('auto', '')
            if auto in dict(Category.AUTO_CHOICES):
                if auto:
                    # Jedna kategoria na sen i jedna na treningi.
                    Category.objects.filter(user=request.user, auto=auto).exclude(pk=category.pk).update(auto='')
                category.auto = auto
            category.save()
            messages.success(request, f'Zapisano „{category.name}”.')
        elif action in ('up', 'down'):
            ordered = list(Category.objects.filter(user=request.user))
            index = ordered.index(category)
            other = index - 1 if action == 'up' else index + 1
            if 0 <= other < len(ordered):
                ordered[index], ordered[other] = ordered[other], ordered[index]
                for position, item in enumerate(ordered):
                    if item.position != position:
                        Category.objects.filter(pk=item.pk).update(position=position)
        elif action == 'archive':
            category.is_archived = not category.is_archived
            category.save(update_fields=['is_archived'])
            messages.success(request, f'„{category.name}” ' + ('ukryta.' if category.is_archived else 'znów widoczna.'))
        elif action == 'delete':
            if category.entries.exists():
                category.is_archived = True
                category.save(update_fields=['is_archived'])
                messages.info(request, f'„{category.name}” ma wpisy, więc została ukryta zamiast usunięta.')
            else:
                delete_with_undo(request, category, f'Usunięto kategorię „{category.name}”.')
        return redirect('timelog:categories')

    def _icon(self, request):
        icon = request.POST.get('icon', '')
        return icon if icon in ICONS else 'bi-circle'

    def _color(self, request):
        try:
            return min(8, max(1, int(request.POST.get('color', 1))))
        except ValueError:
            return 1

    def _value(self, request):
        value = request.POST.get('value', '')
        return value if value in dict(Category.VALUE_CHOICES) else Category.NEUTRAL


class AutomationView(TimelogView):
    """Klucz dla Skrótów iOS i instrukcja automatyzacji."""

    def get(self, request):
        settings = TimelogSettings.for_user(request.user)
        # Skrót działa też w aucie i w pracy tylko pod adresem z Tailscale -
        # adres, pod którym otwarto tę stronę w domu, poza domem nie odpowie.
        base = django_settings.REMOTE_BASE_URL or request.build_absolute_uri('/').rstrip('/')
        return render(request, 'timelog/automation.html', {
            'token': settings.api_token,
            'remote': bool(django_settings.REMOTE_BASE_URL),
            'start_url': base + reverse('timelog:api-start'),
            'stop_url': base + reverse('timelog:api-stop'),
            'status_url': base + reverse('timelog:api-status'),
            'categories': self.categories(),
            'sleep_category': Category.objects.filter(user=request.user, auto=Category.SLEEP).first(),
            'sport_category': Category.objects.filter(user=request.user, auto=Category.SPORT).first(),
        })

    def post(self, request):
        settings = TimelogSettings.for_user(request.user)
        if request.POST.get('action') == 'disable':
            settings.api_token = ''
            messages.success(request, 'Klucz wyłączony – Skróty przestaną działać.')
        else:
            settings.api_token = secrets.token_urlsafe(32)
            messages.success(request, 'Nowy klucz gotowy. Wpisz go w Skrótach.')
        settings.save(update_fields=['api_token'])
        return redirect('timelog:automation')


# --- API dla Skrótów iOS (bez sesji, klucz w nagłówku) ----------------------

@method_decorator(csrf_exempt, name='dispatch')
class ApiView(View):
    """`Authorization: Bearer <klucz>` (albo `X-Timelog-Token`). Klucza nie
    przyjmujemy w adresie - zostawałby w logach Caddy i historii."""

    def dispatch(self, request, *args, **kwargs):
        header = request.headers.get('Authorization', '')
        token = header[7:].strip() if header.lower().startswith('bearer ') else request.headers.get('X-Timelog-Token', '').strip()
        settings = TimelogSettings.objects.filter(api_token=token).select_related('user').first() if token else None
        if settings is None or not secrets.compare_digest(settings.api_token, token):
            return JsonResponse({'ok': False, 'error': 'Zły albo wyłączony klucz.'}, status=401)
        self.user = settings.user
        services.ensure_categories(self.user)
        return super().dispatch(request, *args, **kwargs)

    def payload(self, request):
        if request.content_type == 'application/json':
            try:
                data = json.loads(request.body or b'{}')
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}
        return request.POST

    def state(self, **extra):
        entry = services.running_entry(self.user)
        return JsonResponse({
            'ok': True,
            'running': None if entry is None else {
                'category': entry.category.name,
                'since': timezone.localtime(entry.start).isoformat(),
                'minutes': int((services.now() - entry.start).total_seconds() // 60),
            },
            **extra,
        })


class ApiStartView(ApiView):
    def post(self, request):
        data = self.payload(request)
        category = services.find_category(self.user, data.get('category', ''))
        if category is None:
            names = ', '.join(c.name for c in Category.objects.filter(user=self.user, is_archived=False))
            return JsonResponse({'ok': False, 'error': f'Nie ma takiej kategorii. Są: {names}.'}, status=400)
        services.start_timer(self.user, category, source=Entry.SHORTCUT, note=str(data.get('note', ''))[:200])
        return self.state()


class ApiStopView(ApiView):
    """Zatrzymanie chwilowej kategorii (Instagram zamknięty) wraca do tego,
    co biegło przed nią; `resume` = false wyłącza powrót."""

    def post(self, request):
        data = self.payload(request)
        wanted = data.get('category')
        entry = services.running_entry(self.user)
        # „Wyjście z pracy” zatrzymuje tylko Pracę - nie ubije stopera,
        # który w międzyczasie przełączyłeś na coś innego.
        if entry and wanted and services.find_category(self.user, wanted) != entry.category:
            return self.state(stopped=None, resumed=None)
        resume = str(data.get('resume', '')).strip().lower() not in ('0', 'false', 'nie', 'no')
        stopped = services.stop_timer(self.user, resume=resume)
        return self.state(
            stopped=stopped.category.name if stopped else None,
            resumed=stopped.resumed.category.name if stopped and stopped.resumed else None,
        )


class ApiStatusView(ApiView):
    def get(self, request):
        return self.state()

    post = get
