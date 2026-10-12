"""Uzupełnia dzienną historię cen (OHLC) posiadanych instrumentów.

Ta sama logika stoi za przyciskiem synchronizacji historii w panelu maklerskim
(finance.views.SyncBrokeragePriceHistoryView). Synchronizacja jest przyrostowa:
instrument pamięta pokryty zakres dat, więc kolejne przebiegi dociągają tylko
brakujące sesje.

Przy okazji pobiera notowania ETF-ów strategii GEM (finance.gem) - raz na
przebieg, nie dla każdego użytkownika (--bez-gem pomija ten krok).

Przykład wpisu w crontabie na Raspberry Pi - raz na dobę po zamknięciu sesji:

    30 22 * * 1-5 cd /sciezka/do/Website-Finance && \\
        docker compose exec -T web python manage.py sync_price_history
"""
from finance.gem import sync_gem_prices
from finance.market_data import sync_price_history_for_user

from ._market_data_base import MarketDataCommand


class Command(MarketDataCommand):
    help = 'Uzupełnia dzienną historię cen instrumentów maklerskich.'

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            '--force',
            action='store_true',
            help=(
                'Pobiera historię od nowa, ignorując zapisany zakres pokrycia '
                'i blokadę ponownych prób po błędzie. Używaj po zmianie '
                'symbolu instrumentu albo gdy dane u dostawcy zostały poprawione.'
            ),
        )

        parser.add_argument(
            '--bez-gem',
            action='store_true',
            dest='skip_gem',
            help='Nie pobiera notowań ETF-ów strategii GEM.',
        )

    def handle(self, *args, **options):
        users = self.resolve_users(options.get('users'))
        force = options['force']

        if not options['skip_gem']:
            gem = sync_gem_prices(force=force)
            self.stdout.write(
                f"GEM: fundusze: {gem['synced']}, nowe sesje: {gem['created']}, zaktualizowane: {gem['updated']}"
            )
            for failure in gem['failed']:
                self.stderr.write(self.style.WARNING(f'  GEM {failure}'))

        def sync(user):
            result = sync_price_history_for_user(user, force=force)
            self.stdout.write(
                f"{user.username}: instrumenty: {result['instruments_synced']}, "
                f"nowe sesje: {result['points_created']}, "
                f"zaktualizowane: {result['points_updated']}, "
                f"już kompletne: {result['already_current']}"
            )
            for failure in result['failed']:
                self.stderr.write(self.style.WARNING(f'  {user.username}: {failure}'))

        failures = self.run_for_users(users, sync)
        if failures:
            self.stderr.write(
                self.style.ERROR(f'Nie udało się zsynchronizować historii dla {failures} użytkowników.')
            )
