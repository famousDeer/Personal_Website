"""Pobiera z Garmin Connect nowe treningi, okrążenia i trasy (po kilka na raz)
oraz dane zdrowotne - dla osób, które połączyły konto (strona Połączenia).

Uruchamiane z crona na Pi, np. co godzinę:

    docker compose exec -T web python manage.py sync_garmin
    docker compose exec -T web python manage.py sync_garmin --user <login> --days 90
    docker compose exec -T web python manage.py sync_garmin --health-days 30
    docker compose exec -T web python manage.py sync_garmin --reparse   # bez sieci: pola z zapisanych odpowiedzi
    docker compose exec -T web python manage.py sync_garmin --retry-routes --extras 50   # trasy jeszcze raz
    docker compose exec -T web python manage.py sync_garmin --inspect-route --user <login>  # diagnoza tras
    docker compose exec -T web python manage.py sync_garmin --health-report --user <login>  # zapisane dni zdrowia

Luki w danych zdrowotnych z ostatnich 30 dni uzupełniają się same, po kilka
dni na przebieg; --health-days 30 pobiera cały miesiąc od razu.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from habits.models import DailyHealth
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
        parser.add_argument('--health-report', action='store_true', help='Pokaż zapisane dni zdrowia z 30 dni (bez pobierania).')

    def people(self, user):
        """Osoby do obsłużenia. Nieznany login to błąd z listą loginów - wcześniej
        komenda kończyła się bez słowa i nie było wiadomo, że nic nie zrobiła."""
        users = get_user_model().objects.filter(is_active=True).order_by('username')
        if not user:
            return list(users)
        person = users.filter(username=user).first() or users.filter(username__iexact=user).first()
        if person is None:
            names = ', '.join(users.values_list('username', flat=True)) or '(brak)'
            raise CommandError(f'Nie ma użytkownika „{user}”. Loginy na stronie: {names}.')
        return [person]

    def not_connected(self, person):
        return f'{person}: konto Garmin nie jest połączone (brak tokenów w {garmin.token_dir(person)}).'

    def handle(self, *args, user=None, days=None, health_days=None, reparse=False, extras=None,
               retry_routes=False, inspect_route=False, health_report=False, **options):
        people = self.people(user)
        if health_report:
            shown = 0
            for person in people:
                connected = garmin.is_connected(person)
                if not user and not connected and not DailyHealth.objects.filter(user=person).exists():
                    continue
                self.stdout.write(f'== {person} (Garmin {"połączony" if connected else "NIE jest połączony"})')
                for line in garmin_health.report(person):
                    self.stdout.write(line)
                shown += 1
            if not shown:
                self.stdout.write('Nikt nie ma połączonego Garmina ani zapisanych danych zdrowotnych.')
            return
        if reparse:
            changed = garmin.reparse()
            garmin_health.reparse()
            self.stdout.write(f'Przeliczono treningi: {changed}; dni zdrowia przeliczone.')
            return
        if not settings.GARMIN_ENABLED:
            self.stdout.write(self.style.WARNING('GARMIN_ENABLED jest wyłączone - pomijam.'))
            return
        connected = [person for person in people if garmin.is_connected(person)]
        if user and not connected:
            self.stdout.write(self.style.WARNING(self.not_connected(people[0])))
            return
        if not connected:
            self.stdout.write(self.style.WARNING(
                f'Nikt nie ma połączonego Garmina (tokeny w {settings.GARMIN_TOKEN_DIR}/<id osoby>/) – '
                'połącz konto na stronie Aktywność → Połączenia.'
            ))
            return
        for person in connected:
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
            self.stdout.write(f'{person}: nowych treningów {added}; {garmin_health.coverage(person)}.')

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

