"""Pobiera z Garmin Connect nowe treningi, okrążenia i trasy (po kilka na raz)
oraz dane zdrowotne - dla osób, które połączyły konto (strona Połączenia).

Uruchamiane z crona na Pi, np. co godzinę:

    docker compose exec -T web python manage.py sync_garmin
    docker compose exec -T web python manage.py sync_garmin --user dawid --days 90
    docker compose exec -T web python manage.py sync_garmin --health-days 30
    docker compose exec -T web python manage.py sync_garmin --reparse   # bez sieci: pola z zapisanych odpowiedzi
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

    def handle(self, *args, user=None, days=None, health_days=None, reparse=False, **options):
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
            try:
                added = garmin.sync_all(person, days=days, health_days=health_days, force_health=bool(health_days))
            except garmin.GarminError as exc:
                self.stderr.write(f'{person}: {exc}')
                continue
            self.stdout.write(f'{person}: nowych treningów {added}.')
