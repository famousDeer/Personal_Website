import uuid

from django.conf import settings
from django.db import models


def new_uid():
    return str(uuid.uuid4())


class Workout(models.Model):
    """Jeden trening: z zegarka Garmin albo wpisany ręcznie (także offline).

    `uid` nadaje telefon przy zapisie bez połączenia - po nim serwer rozpoznaje
    ponowną wysyłkę tej samej zmiany, więc synchronizacja jest idempotentna.
    """

    SWIM = 'swim'
    RUN = 'run'
    GYM = 'gym'
    OTHER = 'other'
    # Kolejność = kolejność kolorów kategorii (--cat-1..4) na wykresach.
    KIND_CHOICES = [
        (SWIM, 'Pływanie'),
        (RUN, 'Bieganie'),
        (GYM, 'Siłownia'),
        (OTHER, 'Inna aktywność'),
    ]

    MANUAL = 'manual'
    GARMIN = 'garmin'
    SOURCE_CHOICES = [(MANUAL, 'Ręcznie'), (GARMIN, 'Garmin')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='workouts')
    uid = models.CharField(max_length=36, unique=True, default=new_uid)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    date = models.DateField()
    start_time = models.TimeField(null=True, blank=True)
    title = models.CharField(max_length=120, blank=True)
    duration_s = models.PositiveIntegerField(null=True, blank=True)
    distance_m = models.PositiveIntegerField(null=True, blank=True)
    avg_hr = models.PositiveSmallIntegerField(null=True, blank=True)
    calories = models.PositiveIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=MANUAL)
    garmin_id = models.BigIntegerField(null=True, blank=True, unique=True)
    garmin_type = models.CharField(max_length=60, blank=True)
    # Usunięty trening z Garmina zostaje w bazie jako ślad, żeby kolejna
    # synchronizacja nie pobrała go ponownie.
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'habits_workouts'
        ordering = ['-date', '-start_time', '-id']
        indexes = [models.Index(fields=['user', 'date'], name='habits_workout_user_date')]

    def __str__(self):
        return f'{self.get_kind_display()} {self.date}'


class WorkoutSet(models.Model):
    """Seria na siłowni: ćwiczenie, powtórzenia, ciężar."""

    workout = models.ForeignKey(Workout, on_delete=models.CASCADE, related_name='sets')
    position = models.PositiveSmallIntegerField(default=0)
    exercise = models.CharField(max_length=80)
    reps = models.PositiveSmallIntegerField(null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = 'habits_workout_sets'
        ordering = ['position', 'id']

    def __str__(self):
        return f'{self.exercise} {self.reps or "-"} × {self.weight_kg or "-"} kg'


class ActivitySettings(models.Model):
    """Cel tygodniowy i stan połączenia z Garminem - jeden wiersz na osobę."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='activity_settings')
    weekly_goal = models.PositiveSmallIntegerField(default=3)
    garmin_synced_at = models.DateTimeField(null=True, blank=True)
    garmin_error = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'habits_settings'

    @classmethod
    def for_user(cls, user):
        return cls.objects.get_or_create(user=user)[0]
