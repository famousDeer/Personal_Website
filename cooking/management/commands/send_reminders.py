"""Wysyła powiadomienia push na telefony domowników.

Uruchamiane z crona na Pi, np. rano:

    docker compose exec -T web python manage.py send_reminders
    docker compose exec -T web python manage.py send_reminders --dry-run

Co wysyła:
* "spiżarnia" - produkty, których brakuje albo kończą się według prognozy;
  to samo, co proponuje automatyczna lista zakupów. Powtórzy się dopiero,
  gdy zmieni się zestaw produktów, więc nie przychodzi codziennie to samo;
* "lista" - przypomnienie o aktywnej liście: ile pozycji zostało. Idzie codziennie
  (raz dziennie na listę), a w dzień typowych zakupów ma tylko inny tytuł.

Bez kluczy VAPID komenda tylko mówi, że powiadomienia są wyłączone.
"""
from django.core.management.base import BaseCommand
from django.urls import reverse
from django.utils import timezone

from cooking.models import PushSubscription, ShoppingList
from cooking.services.polish import polish_count
from cooking.services.push import notify, prune_notification_log, push_enabled

MAX_NAMES = 4


class Command(BaseCommand):
    help = 'Wysyła powiadomienia o brakach w spiżarni i o liście zakupów.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Pokaż, co poszłoby na telefony.')
        parser.add_argument(
            '--kind', choices=['spizarnia', 'lista', 'wszystko'], default='wszystko',
            help='Ogranicz do jednego rodzaju powiadomienia.',
        )

    def handle(self, *args, dry_run=False, kind='wszystko', **options):
        if not push_enabled():
            self.stdout.write(self.style.WARNING(
                'Powiadomienia są wyłączone: brak kluczy VAPID w .env '
                '(uruchom: python manage.py generate_vapid_keys).'
            ))
            return
        phones = PushSubscription.objects.count()
        if not phones:
            self.stdout.write('Żaden telefon nie ma jeszcze włączonych powiadomień.')
            return

        sent = 0
        if kind in ('spizarnia', 'wszystko'):
            sent += self.pantry_reminder(dry_run)
        if kind in ('lista', 'wszystko'):
            sent += self.shopping_list_reminder(dry_run)
        if not dry_run:
            prune_notification_log()
        self.stdout.write(self.style.SUCCESS(
            f'{"[próba] " if dry_run else ""}Telefonów z powiadomieniami: {phones}; wysłanych powiadomień: {sent}'
        ))

    # --- spiżarnia ---------------------------------------------------------

    def pantry_reminder(self, dry_run):
        from cooking.views import build_shopping_suggestions

        suggestions = build_shopping_suggestions()
        if not suggestions:
            self.stdout.write('Spiżarnia: nic nie wymaga uzupełnienia.')
            return 0
        names = [suggestion['product'].name for suggestion in suggestions]
        shown = ', '.join(names[:MAX_NAMES])
        rest = len(names) - MAX_NAMES
        body = shown + (f' i {polish_count(rest, "inny", "inne", "innych")}' if rest > 0 else '')
        title = f'Do kupienia: {polish_count(len(names), "produkt", "produkty", "produktów")}'
        self.stdout.write(f'Spiżarnia: {title} - {body}')
        if dry_run:
            return 0
        return notify(
            kind='spizarnia',
            title=title,
            body=body,
            url=reverse('cooking:shopping-app'),
            # Ten sam zestaw produktów nie przypomni się drugi raz.
            once_per='|'.join(sorted(names)),
        )

    # --- lista zakupów -----------------------------------------------------

    def shopping_list_reminder(self, dry_run):
        from cooking.views import get_household_typical_shopping_weekday

        today = timezone.localdate()
        shopping_list = ShoppingList.objects.filter(status=ShoppingList.ACTIVE).order_by('-updated_at').first()
        if shopping_list is None:
            self.stdout.write('Lista: nie ma aktywnej listy.')
            return 0
        left = shopping_list.items.filter(is_purchased=False).count()
        if not left:
            self.stdout.write('Lista: wszystko już odhaczone.')
            return 0
        # Przypomnienie idzie codziennie; dzień typowych zakupów zmienia tylko tytuł.
        weekday = get_household_typical_shopping_weekday()
        shopping_day = weekday is not None and weekday == today.weekday()
        title = f'{"Zakupy dziś" if shopping_day else "Lista zakupów"}: {shopping_list.title}'
        body = f'Zostało {polish_count(left, "produkt", "produkty", "produktów")} na liście.'
        self.stdout.write(f'Lista: {title} - {body}')
        if dry_run:
            return 0
        return notify(
            kind='lista',
            title=title,
            body=body,
            url=reverse('cooking:shopping-app'),
            once_per=f'{shopping_list.id}:{today.isoformat()}',
        )
