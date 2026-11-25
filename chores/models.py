from django.conf import settings
from django.db import models


class Chore(models.Model):
    title = models.CharField('Nazwa zadania', max_length=160)
    notes = models.TextField('Notatka', blank=True)
    due_on = models.DateField('Termin', blank=True, null=True)
    assignees = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through='ChoreAssignment',
        related_name='household_chores', verbose_name='Odpowiedzialni',
    )
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'household_chores'

    def __str__(self):
        return self.title


class ChoreAssignment(models.Model):
    chore = models.ForeignKey(Chore, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        db_table = 'household_chore_assignments'
        constraints = [
            models.UniqueConstraint(fields=['chore', 'user'], name='unique_household_chore_assignment'),
        ]
