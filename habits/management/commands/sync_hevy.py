"""Pobiera zmiany z Hevy (API, Hevy Pro) dla osób, które wkleiły klucz API.

Uruchamiane z crona, np. co godzinę o nieokrągłej minucie (prośba Hevy):

    docker compose exec -T web python manage.py sync_hevy
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from habits.models import ActivitySettings
from habits.services import hevy


class Command(BaseCommand):
    help = 'Pobiera nowe, zmienione i usunięte treningi z Hevy.'

    def add_arguments(self, parser):
        parser.add_argument('--user', help='Tylko ta osoba (login).')

    def handle(self, *args, user=None, **options):
        if not settings.HEVY_ENABLED:
            self.stdout.write(self.style.WARNING('HEVY_ENABLED jest wyłączone - pomijam.'))
            return
        rows = ActivitySettings.objects.exclude(hevy_api_key='').select_related('user').filter(user__is_active=True)
        if user:
            rows = rows.filter(user__username=user)
        for row in rows:
            try:
                added = hevy.sync_user(row.user)
            except hevy.HevyError as exc:
                self.stderr.write(f'{row.user}: {exc}')
                continue
            self.stdout.write(f'{row.user}: nowych treningów {added}.')
