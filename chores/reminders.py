from django.urls import reverse

from cooking.models import PushSubscription
from cooking.services.push import notify


def notify_chore_created(chore):
    subscriptions = PushSubscription.objects.filter(
        user__is_active=True, user__household_chores=chore,
    )
    return notify(
        kind='zadania',
        title='Nowe zadanie domowe',
        body=chore.title,
        url=reverse('chores:index', query={'widok': 'moje'}) + f'#zadanie-{chore.pk}',
        tag=f'zadanie-{chore.pk}',
        subscriptions=subscriptions,
    )
