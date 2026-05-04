from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .account_utils import ensure_personal_finance_account


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_personal_finance_account(sender, instance, created, **kwargs):
    if created:
        ensure_personal_finance_account(instance)
