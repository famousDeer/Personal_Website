"""Pobiera z Garmin Connect nowe treningi, okrążenia i trasy (po kilka na raz)
oraz dane zdrowotne - dla osób, które połączyły konto (strona Połączenia).

Uruchamiane z crona na Pi, np. co godzinę:

    docker compose exec -T web python manage.py sync_garmin
    docker compose exec -T web python manage.py sync_garmin --user dawid --days 90
    docker compose exec -T web python manage.py sync_garmin --health-days 30
    docker compose exec -T web python manage.py sync_garmin --reparse   # bez sieci: pola z zapisanych odpowiedzi
    docker compose exec -T web python manage.py sync_garmin --retry-routes --extras 50   # trasy jeszcze raz
    docker compose exec -T web python manage.py sync_garmin --inspect-route --user dawid  # diagnoza tras
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from habits.services import garmin, garmin_health


class Command(BaseCommand):
    help = 'Pobiera nowe treningi z Garmin Connect.'

    def add_arguments(self, parser):
        parser.add_argument('--user', help='Tylko ta osoba (login).')
        parser.add_argument('--days', type=int, help='Pobierz ostatnie N dni zamiast od ostatniego treningu.')
        parser.add_argument('--health-days', type=int, help='Dane zdrowotne z ostatnich N dni (domyślnie dziś i wczoraj).')
        parser.add_argument('--reparse', action='store_true', help='Przelicz pola z zapisanych odpowiedzi Garmina, bez pobierania.')
        parser.add_argument('--extras', type=int, default=garmin.EXTRAS_PER_RUN,
                            help='Ile treningów dostanie okrążenia i trasę w tym przebiegu (domyślnie %(default)s).')
        parser.add_argument('--retry-routes', action='store_true', help='Ponów pobranie tras dla treningów na zewnątrz bez trasy.')
        parser.add_argument('--inspect-route', action='store_true', help='Pokaż, co Garmin zwraca o trasie ostatniego treningu na zewnątrz.')

    def handle(self, *args, user=None, days=None, health_days=None, reparse=False, extras=None,
               retry_routes=False, inspect_route=False, **options):
        if reparse:
            changed = garmin.reparse()
            garmin_health.reparse()
            self.stdout.write(f'Przeliczono treningi: {changed}; dni zdrowia przeliczone.')
            return
        if not settings.GARMIN_ENABLED:
            self.stdout.write(self.style.WARNING('GARMIN_ENABLED jest wyłączone - pomijam.'))
            return
        people = get_user_model().objects.filter(is_active=True)
        if user:
            people = people.filter(username=user)
        for person in people:
            if not garmin.is_connected(person):
                continue
            if inspect_route:
                self.inspect(person)
                continue
            if retry_routes:
                self.stdout.write(f'{person}: trasy do ponownego pobrania: {garmin.retry_routes(person)}.')
            try:
                added = garmin.sync_all(person, days=days, health_days=health_days, force_health=bool(health_days),
                                        extras_limit=extras)
            except garmin.GarminError as exc:
                self.stderr.write(f'{person}: {exc}')
                continue
            self.stdout.write(f'{person}: nowych treningów {added}.')

    def inspect(self, person):
        from habits.models import Workout

        candidates = Workout.objects.filter(user=person, garmin_id__isnull=False, is_deleted=False).exclude(kind=Workout.GYM)
        workout = next((w for w in candidates.order_by('-date', '-start_time')[:30] if w.distance_m), None)
        if workout is None:
            self.stdout.write(f'{person}: brak treningu z dystansem z Garmina.')
            return
        try:
            client = garmin.connect(person)
        except garmin.GarminError as exc:
            self.stderr.write(f'{person}: {exc}')
            return
        for line in garmin.inspect_route(client, workout):
            self.stdout.write(line)

