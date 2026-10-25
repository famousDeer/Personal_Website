from django.conf import settings
from django.db import models


class Category(models.Model):
    """Na co idzie czas: praca, sen, telefon... Własne kategorie każdej osoby.

    `value` mówi, czy to czas dobrze spędzony - z tego liczy się „strata
    czasu” w podsumowaniu. `auto` wskazuje kategorię dla wpisów z zegarka.
    """

    GOOD = 'good'
    NEUTRAL = 'neutral'
    WASTE = 'waste'
    VALUE_CHOICES = [(GOOD, 'Wartościowy'), (NEUTRAL, 'Neutralny'), (WASTE, 'Strata czasu')]

    SLEEP = 'sleep'
    SPORT = 'sport'
    AUTO_CHOICES = [('', '—'), (SLEEP, 'Sen z zegarka'), (SPORT, 'Treningi z Nawyków')]

    # Kolor = numer koloru kategorii z tokens.css (--cat-1..8).
    COLOR_CHOICES = [(n, str(n)) for n in range(1, 9)]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='time_categories')
    name = models.CharField(max_length=40)
    icon = models.CharField(max_length=40, default='bi-circle')
    color = models.PositiveSmallIntegerField(choices=COLOR_CHOICES, default=1)
    value = models.CharField(max_length=10, choices=VALUE_CHOICES, default=NEUTRAL)
    auto = models.CharField(max_length=10, choices=AUTO_CHOICES, blank=True)
    position = models.PositiveSmallIntegerField(default=0)
    # Ukryta kategoria znika z przycisków, ale stare wpisy zostają.
    is_archived = models.BooleanField(default=False)

    class Meta:
        db_table = 'timelog_categories'
        ordering = ['position', 'id']
        constraints = [models.UniqueConstraint(fields=['user', 'name'], name='timelog_category_user_name')]

    def __str__(self):
        return self.name


class Entry(models.Model):
    """Odcinek czasu w jednej kategorii. Bez końca = stoper właśnie biegnie.

    Wpisy z zegarka mają `external_id` (`sleep:<data>`, `workout:<uid>`) -
    po nim import rozpoznaje, co już dodał. Edycja takiego wpisu zmienia
    źródło na ręczne i import przestaje go nadpisywać.
    """

    MANUAL = 'manual'
    TIMER = 'timer'
    GARMIN = 'garmin'
    SHORTCUT = 'shortcut'
    SOURCE_CHOICES = [(MANUAL, 'Ręcznie'), (TIMER, 'Stoper'), (GARMIN, 'Zegarek'), (SHORTCUT, 'Skrót')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='time_entries')
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='entries')
    start = models.DateTimeField()
    end = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=MANUAL)
    external_id = models.CharField(max_length=80, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'timelog_entries'
        ordering = ['start', 'id']
        indexes = [models.Index(fields=['user', 'start'], name='timelog_entry_user_start')]
        constraints = [
            models.UniqueConstraint(fields=['user', 'external_id'], name='timelog_entry_user_external'),
        ]

    def __str__(self):
        return f'{self.category} {self.start:%Y-%m-%d %H:%M}'


class TimelogSettings(models.Model):
    """Klucz dla Skrótów iOS i wpisy z zegarka, które ktoś usunął."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='timelog_settings')
    # Klucz w nagłówku zapytań ze Skrótów (api/start, api/stop). Pusty = API wyłączone.
    api_token = models.CharField(max_length=64, blank=True, db_index=True)
    # external_id usuniętych wpisów z zegarka - import nie dodaje ich ponownie.
    dismissed = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = 'timelog_settings'

    @classmethod
    def for_user(cls, user):
        return cls.objects.get_or_create(user=user)[0]
