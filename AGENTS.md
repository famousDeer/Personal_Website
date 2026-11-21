# Website-Finance — instrukcje dla Codex

## Wczytywanie kontekstu

- Przed pracą przeczytaj [CLAUDE.md](CLAUDE.md). To wspólne instrukcje projektu dla Claude Code i Codex: architektura, konwencje, komendy i zasady pracy. Stosuj je również w Codex.
- Przed zmianą modułu przeczytaj jego instrukcje:
  - finanse: [finance/CLAUDE.md](finance/CLAUDE.md);
  - kuchnia, paragony i dziennik kalorii: [cooking/CLAUDE.md](cooking/CLAUDE.md);
  - samochody: [cars/CLAUDE.md](cars/CLAUDE.md);
  - konta i wspólne tagi UI: [accounts/CLAUDE.md](accounts/CLAUDE.md);
  - sport, Garmin i Hevy: [habits/CLAUDE.md](habits/CLAUDE.md);
  - harmonogram czasu: [timelog/CLAUDE.md](timelog/CLAUDE.md).
- Przy zmianach obejmujących kilka modułów przeczytaj instrukcje każdego z nich. Dla wspólnych szablonów i plików statycznych uwzględnij ich odbiorców, w tym cache offline Sportu.
- Przy pierwszej pracy po przejęciu projektu przeczytaj [kontekst przekazania](docs/CODEX_CONTEXT.md). To zapis historyczny, a nie aktualna lista zadań ani upoważnienie do wdrożenia.
- Aktualny kod, konfiguracja i historia Git określają stan implementacji. Gdy starsza rozmowa lub notatka opisuje inny stan, sprawdź odpowiednie pliki przed zmianą. Wyniki dawnych testów nie zastępują bieżącej weryfikacji.

## Wspólne instrukcje i materiały Claude

- Utrzymuj zasady i opis modułów w istniejących plikach `CLAUDE.md`; nie twórz ich drugiej kopii w `AGENTS.md`. Ten plik służy do wskazania źródeł i wyjaśnienia różnic między narzędziami.
- Plik `.claude/settings.json` dotyczy Claude Code i nie ustanawia technicznych uprawnień Codex. Zachowaj jednak projektowe ograniczenia odczytu: pomijaj `.env` i jego warianty, migracje, bazy i dumpy, logi, `db_backups/`, `media/`, `private_media/`, `staticfiles/`, `static_cooking/img/`, `img/`, `vendor/`, `node_modules/`, środowiska wirtualne i cache Pythona. Migracje generuj przez `makemigrations` zgodnie z `CLAUDE.md`.
- Nie kopiuj surowej historii rozmów ani danych prywatnych do repozytorium. Jeśli potrzebny jest wcześniejszy kontekst, korzystaj z podsumowania lub odczytaj wskazaną rozmowę dotyczącą tego projektu.
- Notatki w Projekcie claude.ai wymienione w `CLAUDE.md` nie są automatycznie dostępne w tym repozytorium. Nie zakładaj znajomości ich treści.
- Projektowe skills Codex, gdy zostaną dodane, umieszczaj w `.agents/skills/<nazwa>/SKILL.md` wraz z potrzebnymi zasobami. Przy przenoszeniu sprawdź dostępność narzędzi i ścieżki; instrukcje lub uprawnienia Claude nie przenoszą się automatycznie.

## Weryfikacja i zakończenie pracy

- Szukaj przez `rg` i czytaj tylko fragmenty potrzebne do zadania.
- Dla zmian kodu uruchamiaj testy właściwego modułu, np. `python manage.py test cooking --settings=config.test_settings` lub odpowiedni moduł testów.
- Ścieżka z PostgreSQL: `docker compose run --rm web-test python manage.py test <app>`. CI również korzysta z PostgreSQL; wynik SQLite nie rozstrzyga zachowania specyficznego dla PostgreSQL.
- Dla zmian wyłącznie dokumentacyjnych sprawdź odnośniki, zgodność z plikami projektu i `git diff --check`; nie uruchamiaj aplikacji bez potrzeby.
- Zgodnie z `CLAUDE.md` commituj skończone i zweryfikowane zmiany, obejmując tylko pliki danego zadania. Nie pushuj ani nie wdrażaj bez prośby użytkownika.
