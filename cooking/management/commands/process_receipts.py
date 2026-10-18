"""Cron: dokończ odczyt paragonów przerwany restartem i posprzątaj zdjęcia
po usuniętych paragonach.

    docker compose exec -T web python manage.py process_receipts
"""
from django.core.management.base import BaseCommand

from cooking.services.receipts import cleanup_orphan_photos, resume_stuck_receipts


class Command(BaseCommand):
    help = 'Dokańcza zawieszone odczyty paragonów i usuwa osierocone zdjęcia.'

    def handle(self, *args, **options):
        resumed = resume_stuck_receipts()
        removed = cleanup_orphan_photos()
        self.stdout.write(f'Dokończone odczyty: {resumed}, usunięte zdjęcia: {removed}')
