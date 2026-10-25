# timelog – harmonogram dnia (śledzenie czasu)

Cel: wiedzieć, na co idzie dzień i ile czasu to „strata”. Per `request.user`, namespace `timelog`, adres `/czas/`, w menu „Czas”.

Modele (`models.py`):
- Category – nazwa, ikona (`bi-*` z listy `ICONS` w views), kolor `--cat-1..8`, `value` good/neutral/waste (strata liczy się w podsumowaniu), `auto` sleep/sport (kategoria dla wpisów z zegarka, najwyżej jedna na rodzaj), `is_archived` (kategoria z wpisami się nie usuwa – PROTECT, jest ukrywana). Pierwsze wejście tworzy zestaw `DEFAULT_CATEGORIES`.
- Entry – `start`/`end` (bez końca = stoper biegnie, najwyżej jeden), `source` manual/timer/garmin/shortcut, `external_id` (`sleep:<data>`, `workout:<uid>`; unikalne per user).
- TimelogSettings – `api_token` dla Skrótów iOS, `dismissed` (external_id usuniętych wpisów z zegarka – import ich nie odtwarza).

`services.py`:
- `start_timer` (zamyka biegnący; ta sama kategoria = bez zmian; przełączenie w ciągu 60 s poprawia kategorię zamiast tworzyć wpis), `stop_timer`.
- `flatten` – wpisy mogą się nakładać; przy liczeniu wygrywa wpis, który zaczął się później. `day_data`/`period_summary` liczą tylko czas do „teraz”, dzień = doba lokalna (23/25 h przy zmianie czasu, podziałka z `scale`).
- `import_automatic(user, od, do)` – sen z `DailyHealth.raw['sleep']['dailySleepDTO']` (`sleepStart/EndTimestampGMT`) i treningi z `Workout` (data + `start_time` + `duration_s`). Wołany przy otwarciu dnia i podsumowania (bez crona, to tylko zapytania do bazy). Sen przycina stoper/skrót zostawiony na noc. Edytowany wpis dostaje `source=manual` i import go nie rusza; trening usunięty w Nawykach znika.

Widoki: `DayView` (stoper, pasek doby, wpisy + luki z „Uzupełnij”, dopisywanie wstecz – koniec przed początkiem = po północy), `SummaryView` (`?okres=tydzien|miesiac&dzien=`; strata/dzień, % czasu poza snem, porównanie z poprzednim okresem), `CategoriesView`, `AutomationView` (klucz i instrukcja Skrótów), edycja/usuwanie wpisu (`delete_with_undo`).
API dla Skrótów (bez sesji, CSRF wyłączone, klucz tylko w nagłówku `Authorization: Bearer` lub `X-Timelog-Token`): `api/start/` (`category` – nazwa bez wielkości liter i polskich znaków albo id), `api/stop/` (z `category` zatrzymuje tylko, gdy ta kategoria biegnie), `api/status/`. JSON albo formularz.

Statyczne: `static/timelog/css/timelog.css`, `static/timelog/js/timelog.js` (licznik stopera, odświeżenie po powrocie do karty).
Testy: `python manage.py test timelog --settings=config.test_settings`.
