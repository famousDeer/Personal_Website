"""Tworzy parę kluczy do powiadomień push (VAPID).

Uruchom raz, wklej wynik do pliku .env i przebuduj kontener:

    docker compose run --rm web python manage.py generate_vapid_keys

Klucz prywatny zostaje na Pi - nie trafia do repozytorium ani na telefon.
Zmiana kluczy unieważnia zgody wydane przez telefony (trzeba włączyć
powiadomienia jeszcze raz).
"""
import base64

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Generuje klucze VAPID do powiadomień push.'

    def handle(self, *args, **options):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        private_key = ec.generate_private_key(ec.SECP256R1())
        private_raw = private_key.private_numbers().private_value.to_bytes(32, 'big')
        public_raw = private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        encode = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip('=')  # noqa: E731

        self.stdout.write('')
        self.stdout.write('Dopisz do pliku .env na Raspberry Pi:')
        self.stdout.write('')
        self.stdout.write(f'VAPID_PUBLIC_KEY={encode(public_raw)}')
        self.stdout.write(f'VAPID_PRIVATE_KEY={encode(private_raw)}')
        self.stdout.write('VAPID_SUBJECT=mailto:twoj@email')
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            'Klucz prywatny trzymaj tylko w .env. Po zmianie kluczy trzeba na nowo '
            'włączyć powiadomienia na każdym telefonie.'
        ))
