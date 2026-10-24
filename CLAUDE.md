# Website-Finance – domowa strona (Django)

Aplikacja domowa w sieci lokalnej: finanse (budżet, import wyciągów, konta maklerskie, GEM, podróże), kuchnia (przepisy, wspólna spiżarnia, listy zakupów offline, push), samochód. UI i komentarze po polsku.
Stack: Python 3.12, Django 5.2, DRF, PostgreSQL 15, Gunicorn + WhiteNoise, Caddy (HTTPS), Docker Compose na Raspberry Pi 4 (2 GB RAM, karta SD). Bez Celery/Redis – zadania okresowe to komendy `manage.py` z crona. Cache plikowy (`/tmp/website-finance-cache`).

## Mapa
- `config/` – settings (env z `.env`, `RUNNING_TESTS`, flagi `*_ENABLED`), urls, `test_settings.py` (SQLite, bez sieci), `/healthz`.
- `finance/` – budżet, import wyciągów, maklerka, GEM, podróże → `finance/CLAUDE.md`.
- `cooking/` – przepisy, spiżarnia, listy zakupów, push → `cooking/CLAUDE.md`.
- `cars/` – auta, tankowania, serwisy (PDF), opony → `cars/CLAUDE.md`.
- `accounts/` – logowanie, rejestracja, profil, konta wspólne; tagi szablonów `ui` → `accounts/CLAUDE.md`.
- `habits/` – aktywność fizyczna (pływanie, bieganie, siłownia), Garmin, tryb offline → `habits/CLAUDE.md`.
- `utils/` – `undo.py` (usuwanie z „Cofnij”), `navigation.py` (`redirect_back`), `tools.py` (`parse_decimal`, `parse_date_input`, `month_start`).
- `templates/` – `base.html`, `partials/page_head.html`; `static/css/tokens.css` (kolory, motyw), `app.css`, `ui.css`; `static/js/charts.js` (`AppCharts`).
- `deploy/` – `update.sh`, `reboot.sh`, `Caddyfile`. `db_backups/` – backup na Pi → Google Drive. **Pomijaj.**

## Komendy
```bash
docker compose up -d --build                           # produkcja (web uruchamia migrate przy starcie)
docker compose up web-dev --build                      # serwer deweloperski
docker compose logs --tail 50 web                      # logi – zawsze z --tail
docker compose run --rm web-dev python manage.py makemigrations <app>
docker compose run --rm web-test python manage.py test finance   # Postgres jak w CI
python manage.py test finance --settings=config.test_settings     # lokalnie, SQLite
python manage.py test finance.tests.test_gem -v 1 2>&1 | tail -30 # pojedynczy moduł
docker compose exec -T web python manage.py sync_price_history    # cron: historia cen + GEM
docker compose exec -T web python manage.py sync_garmin           # cron: nowe treningi z Garmina
```

## Konwencje (widoczne w repo)
- Widoki klasowe (`View`) z `@method_decorator(login_required, ...)` albo `LoginRequiredMixin`. Logika poza widokami: `cooking/services/*.py`, `finance/*.py` (np. `bank_import.py`, `gem.py`).
- Finanse są per aktywne konto (`get_active_finance_account(request)`), maklerka per `request.user`. Spiżarnia i listy zakupów są wspólne dla całego domu.
- Usuwanie bez „Na pewno?”: `utils.undo.delete_with_undo(...)` + komunikat z „Cofnij”. Powrót po akcji: `redirect_back`.
- Komunikaty `django.contrib.messages` po polsku. Liczby i daty w formacie pl (`USE_THOUSAND_SEPARATOR`). Odmiana: `{{ n|pl:"rachunek,rachunki,rachunków" }}`.
- Szablony: `{% load ui %}`, nagłówek `{% page_head ... %}{% endpage_head %}`, partiale w `<app>/templates/<app>/partials/`. Haki JS przez atrybuty `data-*`. JS per funkcja w `<app>/static/js/`, bez bundlera.
- CSS: tokeny z `tokens.css`, ciemny motyw przez `[data-theme="dark"]`. Wykresy Chart.js przez `AppCharts`: jedna oś, kolor = tożsamość serii.
- Modele mają jawne `db_table`. Zewnętrzne API mają flagę w settings i są wyłączone w testach (`GEM_PRICES_ENABLED`, `OPEN_FOOD_FACTS_ENABLED`, `TRAVEL_GEOCODING_ENABLED`, `GARMIN_ENABLED`).
- Testy: `finance/tests/test_*.py`, `cooking/tests*.py`, `habits/tests/test_*.py`, `<app>/tests.py`. Sieć zawsze mockowana (`unittest.mock.patch`).

## Zasady pracy (oszczędzaj tokeny)
- Najpierw ten plik i `CLAUDE.md` appki. Czytaj tylko pliki potrzebne do zadania. Duże pliki (`finance/views.py` ~2400 l., `cooking/views.py`, `finance/templates/finance/brokerage.html`) przeszukuj `grep -n` i czytaj fragmenty (offset/limit).
- Nie czytaj migracji, `staticfiles/`, `media/`, `vendor/`, dumpów `*.sql`, `db_backups/`, `.env` (blokuje to `.claude/settings.json`). Nowa migracja: `makemigrations`, nie pisz jej ręcznie.
- Testuj tylko zmienianą appkę albo moduł testów; pełny zestaw dopiero przed wdrożeniem.
- Przycinaj wyjścia: `| tail -30`, `| head -50`, `git --no-optional-locks status --short`, `docker compose logs --tail 50`.
- db_backups zostawiaj bez zmian. Nie commituj ani nie pushuj bez prośby.
- Notatki projektowe (analizy, plany) są w Projekcie claude.ai, w `claude/*.md` – nie dubluj ich tutaj.
