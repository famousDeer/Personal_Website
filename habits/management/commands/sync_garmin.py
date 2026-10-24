"""Pobiera nowe treningi z Garmin Connect dla osób połączonych przez garmin_login.

Uruchamiane z crona na Pi, np. co godzinę:

    docker compose exec -T web python manage.py sync_garmin
    docker compose exec -T web python manage.py sync_garmin --user dawid --days 90
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from habits.services import garmin


class Command(BaseCommand):
    help = 'Pobiera nowe treningi z Garmin Connect.'

    def add_arguments(self, parser):
        parser.add_argument('--user', help='Tylko ta osoba (login).')
        parser.add_argument('--days', type=int, help='Pobierz ostatnie N dni zamiast od ostatniego treningu.')

    def handle(self, *args, user=None, days=None, **options):
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
                added = garmin.sync_user(person, days=days)
            except garmin.GarminError as exc:
                self.stderr.write(f'{person}: {exc}')
                continue
            self.stdout.write(f'{person}: nowych treningów {added}.')
