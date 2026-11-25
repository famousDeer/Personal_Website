from django.db.models import Count
from django.urls import reverse
from django.utils import timezone

from cooking.models import PushSubscription
from cooking.services.polish import polish_count
from cooking.services.push import notify

from .models import ChoreAssignment


def send_chore_reminders(write, dry_run=False):
    today = timezone.localdate()
    subscriptions = PushSubscription.objects.filter(user__is_active=True)
    due = (
        ChoreAssignment.objects.filter(
            chore__due_on__lte=today, chore__completed_at__isnull=True,
            user_id__in=subscriptions.values('user_id'),
        ).values('user_id').annotate(total=Count('chore_id'))
    )
    title = 'Zadania domowe'
    url = reverse('chores:index') + '?widok=moje'
    sent = 0
    for row in due:
        user_id = row['user_id']
        body = f'Do zrobienia: {polish_count(row["total"], "zadanie", "zadania", "zadań")} z terminem na dziś lub wcześniejszym.'
        write(f'{title} (użytkownik {user_id}): {body}')
        if not dry_run:
            sent += notify(
                kind='zadania', title=title, body=body, url=url,
                once_per=f'{today.isoformat()}:{user_id}',
                subscriptions=subscriptions.filter(user_id=user_id),
            )
    return sent
