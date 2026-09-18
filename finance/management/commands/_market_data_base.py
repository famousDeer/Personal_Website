"""Wspólna obsługa wyboru użytkowników dla komend danych rynkowych."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class MarketDataCommand(BaseCommand):
    """Baza dla komend, które przetwarzają dane rynkowe użytkownik po użytkowniku.

    Komendy danych rynkowych odpytują zewnętrznych dostawców, więc nie powinny
    działać w cyklu request/response. Uruchamiane z crona przechodzą po
    użytkownikach, którzy naprawdę mają instrumenty, i nigdy nie przerywają
    całego przebiegu z powodu jednego użytkownika.
    """

    def add_arguments(self, parser):
        parser.add_argument(
            '--user',
            action='append',
            dest='users',
            default=None,
            metavar='NAZWA',
            help=(
                'Nazwa użytkownika do przetworzenia. Można podać wielokrotnie. '
                'Domyślnie: wszyscy użytkownicy mający instrumenty maklerskie.'
            ),
        )

    def resolve_users(self, usernames):
        User = get_user_model()
        if usernames:
            users = list(User.objects.filter(username__in=usernames))
            missing = sorted(set(usernames) - {user.username for user in users})
            if missing:
                raise CommandError(f'Nie znaleziono użytkowników: {", ".join(missing)}.')
            return users

        return list(
            User.objects
            .filter(brokerage_instruments__isnull=False)
            .distinct()
            .order_by('username')
        )

    def run_for_users(self, users, handler):
        """Uruchamia ``handler(user)`` dla każdego użytkownika, izolując błędy.

        Zwraca liczbę użytkowników, dla których przebieg się nie udał. Pojedynczy
        padnięty dostawca nie może zatrzymać nocnego przebiegu dla pozostałych.
        """
        if not users:
            self.stdout.write('Brak użytkowników z instrumentami maklerskimi. Nic do zrobienia.')
            return 0

        failures = 0
        for user in users:
            try:
                handler(user)
            except Exception as exc:  # noqa: BLE001 - cron nie może przerwać na jednym użytkowniku
                failures += 1
                self.stderr.write(self.style.ERROR(f'{user.username}: {exc}'))
        return failures
