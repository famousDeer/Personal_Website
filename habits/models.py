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
    HEVY = 'hevy'
    SOURCE_CHOICES = [(MANUAL, 'Ręcznie'), (GARMIN, 'Garmin'), (HEVY, 'Hevy')]

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
    # Siłownia z Hevy; trening z Garmina i z Hevy w tym samym czasie to jeden
    # wiersz z oboma identyfikatorami (czas i tętno z zegarka, serie z Hevy).
    # Import z pliku CSV ma identyfikator "csv:<skrót>".
    hevy_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    # Szczegóły do widoku treningu (strefy tętna, okrążenia, Training Effect,
    # pływanie...) - liczone z garmin_raw, idą do telefonu w snapshocie.
    details = models.JSONField(default=dict, blank=True)
    # Surowe podsumowanie z Garmina: pozwala przeliczyć `details` bez
    # ponownego pobierania (sync_garmin --reparse). Nie idzie do telefonu.
    garmin_raw = models.JSONField(null=True, blank=True)
    # Okrążenia i trasa pobierane osobnym zapytaniem (stopniowo, cron).
    extras_synced = models.BooleanField(default=False)
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


class WorkoutRoute(models.Model):
    """Trasa GPS treningu (uproszczona, kilkaset punktów) - tylko z połączeniem."""

    workout = models.OneToOneField(Workout, on_delete=models.CASCADE, related_name='route')
    points = models.JSONField(default=list)   # [[szerokość, długość], ...]

    class Meta:
        db_table = 'habits_workout_routes'


class WorkoutSet(models.Model):
    """Seria na siłowni: ćwiczenie, powtórzenia, ciężar."""

    workout = models.ForeignKey(Workout, on_delete=models.CASCADE, related_name='sets')
    position = models.PositiveSmallIntegerField(default=0)
    exercise = models.CharField(max_length=80)
    reps = models.PositiveSmallIntegerField(null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # Ćwiczenia na czas (deska) zamiast powtórzeń.
    duration_s = models.PositiveIntegerField(null=True, blank=True)
    # Rodzaj serii z Hevy: '' (zwykła), warmup, failure, dropset. Rozgrzewki
    # nie liczą się do rekordów ani postępu.
    set_type = models.CharField(max_length=10, blank=True)

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
    health_synced_at = models.DateTimeField(null=True, blank=True)
    # Klucz API Hevy (Hevy Pro) - zwykły tekst w bazie, jak hasła aplikacji
    # w innych integracjach domowych; widzi go tylko właściciel.
    hevy_api_key = models.CharField(max_length=64, blank=True)
    hevy_synced_at = models.DateTimeField(null=True, blank=True)
    hevy_error = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'habits_settings'

    @classmethod
    def for_user(cls, user):
        return cls.objects.get_or_create(user=user)[0]


class DailyHealth(models.Model):
    """Dzień z zegarka: sen, tętno spoczynkowe, HRV, Body Battery, kroki, stres
    oraz wskaźniki formy (VO2max, status treningowy, gotowość, prognozy biegu)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='daily_health')
    date = models.DateField()
    steps = models.PositiveIntegerField(null=True, blank=True)
    step_goal = models.PositiveIntegerField(null=True, blank=True)
    resting_hr = models.PositiveSmallIntegerField(null=True, blank=True)
    stress_avg = models.PositiveSmallIntegerField(null=True, blank=True)
    body_battery_high = models.PositiveSmallIntegerField(null=True, blank=True)
    body_battery_low = models.PositiveSmallIntegerField(null=True, blank=True)
    intensity_minutes = models.PositiveIntegerField(null=True, blank=True)
    sleep_s = models.PositiveIntegerField(null=True, blank=True)
    sleep_deep_s = models.PositiveIntegerField(null=True, blank=True)
    sleep_rem_s = models.PositiveIntegerField(null=True, blank=True)
    sleep_score = models.PositiveSmallIntegerField(null=True, blank=True)
    hrv_avg = models.PositiveSmallIntegerField(null=True, blank=True)
    hrv_status = models.CharField(max_length=20, blank=True)
    readiness = models.PositiveSmallIntegerField(null=True, blank=True)
    readiness_level = models.CharField(max_length=20, blank=True)
    vo2max = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    training_status = models.CharField(max_length=40, blank=True)
    race_5k_s = models.PositiveIntegerField(null=True, blank=True)
    race_10k_s = models.PositiveIntegerField(null=True, blank=True)
    race_half_s = models.PositiveIntegerField(null=True, blank=True)
    race_marathon_s = models.PositiveIntegerField(null=True, blank=True)
    # Surowe odpowiedzi (przycięte do podsumowań) - do przeliczenia pól bez
    # ponownego pobierania, gdy Garmin zmieni nazwy.
    raw = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'habits_daily_health'
        ordering = ['-date']
        constraints = [models.UniqueConstraint(fields=['user', 'date'], name='habits_daily_health_user_date')]

    def __str__(self):
        return f'{self.user} {self.date}'
