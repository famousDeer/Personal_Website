# Website-Finance – domowa strona (Django)

Aplikacja domowa w sieci lokalnej: finanse (budżet, import wyciągów, konta maklerskie, GEM, podróże), kuchnia (przepisy, wspólna spiżarnia, listy zakupów offline, push), samochód, sport, harmonogram czasu. UI i komentarze po polsku.
Stack: Python 3.12, Django 5.2, DRF, PostgreSQL 15, Gunicorn + WhiteNoise, Caddy (HTTPS), Docker Compose na Raspberry Pi 4 (2 GB RAM, karta SD). Bez Celery/Redis – zadania okresowe to komendy `manage.py` z crona. Cache plikowy (`/tmp/website-finance-cache`).

## Mapa
- `config/` – settings (env z `.env`, `RUNNING_TESTS`, flagi `*_ENABLED`), urls, `test_settings.py` (SQLite, bez sieci), `/healthz`.
- `finance/` – budżet, import wyciągów, maklerka, GEM, podróże → `finance/CLAUDE.md`.
- `cooking/` – przepisy, spiżarnia, listy zakupów, push → `cooking/CLAUDE.md`.
- `cars/` – auta, tankowania, serwisy (PDF), opony → `cars/CLAUDE.md`.
- `accounts/` – logowanie, rejestracja, profil, konta wspólne; tagi szablonów `ui` → `accounts/CLAUDE.md`.
- `habits/` – moduł „Sport” (adres `/sport/`, namespace `sport`): aktywność fizyczna (pływanie, bieganie, siłownia), Garmin (treningi, zdrowie), Hevy, tryb offline → `habits/CLAUDE.md`.
- `timelog/` – harmonogram dnia: stoper, luki, podsumowanie i „strata czasu”, sen i treningi z `habits`, API dla Skrótów iOS → `timelog/CLAUDE.md`.
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
docker compose exec -T web python manage.py sync_garmin           # cron: treningi i zdrowie z Garmina
docker compose exec -T web python manage.py sync_hevy             # cron: siłownia z Hevy
```

## Konwencje (widoczne w repo)
- Widoki klasowe (`View`) z `@method_decorator(login_required, ...)` albo `LoginRequiredMixin`. Logika poza widokami: `cooking/services/*.py`, `finance/*.py` (np. `bank_import.py`, `gem.py`).
- Finanse są per aktywne konto (`get_active_finance_account(request)`), maklerka per `request.user`. Spiżarnia i listy zakupów są wspólne dla całego domu.
- Usuwanie bez „Na pewno?”: `utils.undo.delete_with_undo(...)` + komunikat z „Cofnij”. Powrót po akcji: `redirect_back`.
- Komunikaty `django.contrib.messages` po polsku. Liczby i daty w formacie pl (`USE_THOUSAND_SEPARATOR`). Odmiana: `{{ n|pl:"rachunek,rachunki,rachunków" }}`.
- Szablony: `{% load ui %}`, nagłówek `{% page_head ... %}{% endpage_head %}`, partiale w `<app>/templates/<app>/partials/`. Haki JS przez atrybuty `data-*`. JS per funkcja w `<app>/static/js/`, bez bundlera.
- CSS: tokeny z `tokens.css`, ciemny motyw przez `[data-theme="dark"]`. Wykresy Chart.js przez `AppCharts`: jedna oś, kolor = tożsamość serii.
- Modele mają jawne `db_table`. Zewnętrzne API mają flagę w settings i są wyłączone w testach (`GEM_PRICES_ENABLED`, `OPEN_FOOD_FACTS_ENABLED`, `TRAVEL_GEOCODING_ENABLED`, `GARMIN_ENABLED`, `HEVY_ENABLED`).
- Testy: `finance/tests/test_*.py`, `cooking/tests*.py`, `habits/tests/test_*.py`, `<app>/tests.py`. Sieć zawsze mockowana (`unittest.mock.patch`).

## Zasady pracy (oszczędzaj tokeny)
- Najpierw ten plik i `CLAUDE.md` appki. Czytaj tylko pliki potrzebne do zadania. Duże pliki (`finance/views.py` ~2400 l., `cooking/views.py`, `finance/templates/finance/brokerage.html`) przeszukuj `grep -n` i czytaj fragmenty (offset/limit).
- Nie czytaj migracji, `staticfiles/`, `media/`, `vendor/`, dumpów `*.sql`, `db_backups/`, `.env` (blokuje to `.claude/settings.json`). Nowa migracja: `makemigrations`, nie pisz jej ręcznie.
- Testuj tylko zmienianą appkę albo moduł testów; pełny zestaw dopiero przed wdrożeniem.
- Przycinaj wyjścia: `| tail -30`, `| head -50`, `git --no-optional-locks status --short`, `docker compose logs --tail 50`.
- db_backups zostawiaj bez zmian. Nie commituj ani nie pushuj bez prośby.
- Notatki projektowe (analizy, plany) są w Projekcie claude.ai, w `claude/*.md` – nie dubluj ich tutaj.

# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
