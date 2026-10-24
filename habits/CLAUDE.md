# habits – aktywność fizyczna (pływanie, bieganie, siłownia)

Cel strony: pomóc w regularności. Treningi z zegarka Garmin albo wpisane ręcznie, per `request.user`. Namespace URL `habits`, w menu „Nawyki”.

Modele (`models.py`): Workout (`kind` swim/run/gym/other – kolejność = kolory `--cat-1..4`; `uid` z telefonu, `source` manual/garmin, `garmin_id` unikalny, `is_deleted` tylko dla treningów z Garmina – ślad, żeby sync ich nie wskrzesił), WorkoutSet (ćwiczenie, powtórzenia, kg), ActivitySettings (cel tygodniowy w DNIACH z treningiem, stan Garmina).

Strona działa offline jak tryb zakupów w cooking:
- `views.py` – `ActivityView` (szablon `activity.html` bez danych, rozszerza `base.html`), `ActivityServiceWorkerView` (`activity_sw.js`, cache `aktywnosc-<wersja>`; wersja = skrót plików z `APP_STATIC_FILES` + szablonów, w tym `base.html` – nowy plik statyczny w base.html dopisz do listy), API: `snapshot`, `sync` (kolejka operacji `save`/`delete`/`goal`, każda osobno, idempotentne po `uid`), `garmin` (przycisk „Pobierz z Garmina”). API zwraca 401 JSON zamiast przekierowania.
- `services/workouts.py` – walidacja i zapis operacji, `activity_snapshot`. Zapis po usunięciu = „Cofnij”.
- `static/habits/js/activity-stats.js` – czyste funkcje (cel i seria tygodni, kalendarz, porównanie miesięcy, serie wykresów, rekordy, formatowanie, `parseDuration`); testy w Node: `tests/test_activity_js.py`. Statystyki liczy przeglądarka, bo obejmują treningi dodane offline.
- `static/habits/js/activity-app.js` – IndexedDB (`aktywnosc`/`kv`, klucz `state`), kolejka, synchronizacja, rysowanie, formularz (modal Bootstrap), wykresy przez `AppCharts.slot`.

Garmin (`services/garmin.py`): nieoficjalna biblioteka `garminconnect` (wymaga Pythona 3.12+), import leniwy – testy jej nie potrzebują. `garmin_login --user X` (interaktywnie, hasło nie jest zapisywane, tokeny w `GARMIN_TOKEN_DIR/<user.pk>/`, domyślnie `private_media/garmin`), `sync_garmin` z crona. Sync pobiera od ostatniego treningu z Garmina −3 dni (pierwszy raz 30 dni), nie nadpisuje już pobranych, pomija spacery; serie siłowe z `get_activity_exercise_sets` (ciężar w gramach), nazwy ćwiczeń po polsku w `EXERCISE_NAMES`. Błąd trafia do `ActivitySettings.garmin_error` i na stronę. Flaga `GARMIN_ENABLED` (w testach wyłączona, klient mockowany).

Testy: `python manage.py test habits --settings=config.test_settings`.
