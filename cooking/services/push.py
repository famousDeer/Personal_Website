"""Powiadomienia push na telefon (Web Push/VAPID).

Serwer stoi w domu i z zewnątrz jest niedostępny, ale powiadomienie nie idzie
prosto do telefonu: Pi wysyła je do serwera Apple albo Google (adres zapisany
w subskrypcji), a ten dostarcza je na telefon również poza domem.

Klucze VAPID generuje `python manage.py generate_vapid_keys`. Bez kluczy
wysyłka jest po prostu wyłączona - reszta aplikacji działa normalnie.
"""
import hashlib
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import PushSubscription, SentNotification

logger = logging.getLogger(__name__)

# Adresy, po których dostawca mówi "tej subskrypcji już nie ma".
GONE_STATUSES = (404, 410)
MAX_FAILURES = 5


def push_enabled():
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def _vapid_claims():
    return {'sub': settings.VAPID_SUBJECT}


def send_to_subscription(subscription, payload):
    """Wysyła jedno powiadomienie. Zwraca True, gdy się udało."""
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info={
                'endpoint': subscription.endpoint,
                'keys': {'p256dh': subscription.p256dh, 'auth': subscription.auth},
            },
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims=dict(_vapid_claims()),
            timeout=10,
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if status in GONE_STATUSES:
            # Telefon odinstalował aplikację albo cofnął zgodę.
            subscription.delete()
            return False
        subscription.failures += 1
        subscription.save(update_fields=['failures'])
        if subscription.failures >= MAX_FAILURES:
            subscription.delete()
        logger.warning('Nie udało się wysłać powiadomienia (%s): %s', status, exc)
        return False
    except Exception as exc:                      # np. brak sieci na Pi
        logger.warning('Błąd wysyłki powiadomienia: %s', exc)
        return False

    subscription.failures = 0
    subscription.last_success_at = timezone.now()
    subscription.save(update_fields=['failures', 'last_success_at'])
    return True


def notify(kind, title, body, url='', tag='', once_per=None, subscriptions=None):
    """Wysyła powiadomienie na wszystkie telefony domu.

    ``once_per`` (tekst) pilnuje, żeby ta sama treść nie przyszła drugi raz -
    zapisujemy jej odcisk w SentNotification. Zwraca liczbę telefonów,
    do których powiadomienie dotarło (albo 0, gdy pominięte).
    """
    if not push_enabled():
        return 0
    if once_per is not None:
        fingerprint = hashlib.sha256(once_per.encode('utf-8')).hexdigest()[:64]
        try:
            # Savepoint, żeby "to już wysyłaliśmy" nie psuło transakcji wywołującego.
            with transaction.atomic():
                SentNotification.objects.create(kind=kind, fingerprint=fingerprint)
        except IntegrityError:
            return 0

    payload = {'title': title, 'body': body, 'url': url, 'tag': tag or kind}
    targets = subscriptions if subscriptions is not None else PushSubscription.objects.all()
    return sum(1 for subscription in targets if send_to_subscription(subscription, payload))


def prune_notification_log(days=90):
    return SentNotification.objects.filter(sent_at__lt=timezone.now() - timedelta(days=days)).delete()[0]
