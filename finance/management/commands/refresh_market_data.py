"""Odświeża bieżące ceny otwartych pozycji maklerskich.

Ta sama logika stoi za przyciskiem "Odśwież" w panelu maklerskim
(finance.views.RefreshBrokerageMarketDataView). Tutaj jest dostępna z crona,
żeby ceny były świeże bez czekania na request użytkownika.

Przykład wpisu w crontabie na Raspberry Pi - w dni robocze co godzinę
w godzinach sesji GPW:

    0 9-18 * * 1-5 cd /sciezka/do/Website-Finance && \\
        docker compose exec -T web python manage.py refresh_market_data
"""
from finance.market_data import refresh_market_data_for_user

from ._market_data_base import MarketDataCommand


class Command(MarketDataCommand):
    help = 'Odświeża ceny rynkowe otwartych pozycji maklerskich.'

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            '--dividends',
            action='store_true',
            help=(
                'Dodatkowo synchronizuje dywidendy przez Alpha Vantage. '
                'Zużywa dzienny limit klucza API, więc uruchamiaj rzadko '
                '(na przykład raz w tygodniu), nie przy każdym odświeżeniu cen.'
            ),
        )

    def handle(self, *args, **options):
        users = self.resolve_users(options.get('users'))
        refresh_dividends = options['dividends']

        def refresh(user):
            result = refresh_market_data_for_user(user, refresh_dividends=refresh_dividends)
            parts = [f"ceny: {result['updated_quotes']}"]
            if result['inactive_skipped']:
                parts.append(f"pominięto nieaktywne: {result['inactive_skipped']}")
            if result['dividends_checked']:
                parts.append(f"dywidendy: {result['updated_dividends']}")
            if result['merged_instruments']:
                parts.append(f"scalone instrumenty: {result['merged_instruments']}")
            self.stdout.write(
                f"{user.username}: {', '.join(parts)} ({result['source']})"
            )

            for failure in result['failed_quotes']:
                self.stderr.write(self.style.WARNING(f'  {user.username}: {failure}'))
            if result['dividends_checked']:
                for failure in result['failed_dividends']:
                    self.stderr.write(
                        self.style.WARNING(f'  {user.username} (dywidendy): {failure}')
                    )

        failures = self.run_for_users(users, refresh)
        if failures:
            self.stderr.write(
                self.style.ERROR(f'Nie udało się odświeżyć danych dla {failures} użytkowników.')
            )
