"""Jednorazowe połączenie konta Garmin Connect z osobą na stronie.

Interaktywnie (potrzebny terminal, flaga -it):

    docker compose exec -it web python manage.py garmin_login --user <login>

Pyta o e-mail, hasło i - przy włączonym 2FA - kod z SMS/aplikacji. Hasło nie
jest zapisywane; na dysku (GARMIN_TOKEN_DIR/<id osoby>/) zostają tylko tokeny,
ważne mniej więcej rok. Potem komenda pobiera treningi z ostatniego roku.
"""
from getpass import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from habits.services import garmin


class Command(BaseCommand):
    help = 'Łączy konto Garmin Connect (zapisuje tokeny) i pobiera historię treningów.'

    def add_arguments(self, parser):
        parser.add_argument('--user', required=True, help='Login osoby na stronie.')
        parser.add_argument('--days', type=int, default=365, help='Ile dni historii pobrać (domyślnie 365).')

    def handle(self, *args, user, days, **options):
        person = get_user_model().objects.filter(username=user).first()
        if person is None:
            raise CommandError(f'Nie ma użytkownika „{user}”.')
        email = input('E-mail konta Garmin: ').strip()
        password = getpass('Hasło Garmin (nie jest zapisywane): ')
        try:
            client = garmin.login(person, email, password, prompt_mfa=lambda: input('Kod 2FA: ').strip())
        except Exception as exc:
            raise CommandError(f'Logowanie do Garmina nie powiodło się: {exc}') from exc
        self.stdout.write(self.style.SUCCESS('Połączono z Garminem. Pobieram treningi…'))
        try:
            added = garmin.sync_user(person, days=days, client=client)
        except garmin.GarminError as exc:
            raise CommandError(f'Pobieranie treningów nie powiodło się: {exc}') from exc
        self.stdout.write(self.style.SUCCESS(f'Dodano treningów: {added}.'))
