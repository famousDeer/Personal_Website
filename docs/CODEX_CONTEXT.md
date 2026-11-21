# Przekazanie Website-Finance z Claude Code do Codex

Stan notatki: 2026-11-21. Punkt odniesienia kodu: commit `9541c00` z 2026-11-14.

To skrót do rozpoczęcia pracy, nie pełna historia ani bieżący backlog. Szczegóły architektury i modułów pozostają w `CLAUDE.md` w katalogu głównym i w katalogach aplikacji. Nowe ustalenia dotyczące modułów zapisuj w tych wspólnych instrukcjach.

## Źródła i zakres

- Przeczytano główny i sześć modułowych plików `CLAUDE.md`, konfigurację testów, CI i zależności.
- Przejrzano końcowe wiadomości trzech lokalnych sesji Claude dotyczących kalorii, trendu portfela i paragonów; nie zaimportowano wszystkich rozmów.
- Historię znaleziono w `~/.claude/projects/-Users-dawid-Documents-Documents-Website-Finance/`. Identyfikatory sesji: `e61c00f5-16db-443e-9a56-b30757bfff66`, `7caf469a-7cbb-428f-8d92-540d6c7d5555`, `33fc1967-4e7a-4aa9-935e-0253d5af1426`. Ten katalog jest lokalnym źródłem, nie zależnością projektu.
- Plik `memory/MEMORY.md` tego projektu był pusty. W przeszukanych plikach repozytorium i lokalnego katalogu `~/.claude` nie znaleziono `SKILL.md` do przeniesienia.
- Nie odczytano notatek z Projektu claude.ai, o których wspomina główny `CLAUDE.md`.
- Obecność opisanych niżej funkcji potwierdzono w kodzie i historii Git. Podczas przekazania nie uruchamiano testów aplikacji i nie sprawdzano stanu wdrożenia na Raspberry Pi.

## Ostatnie zmiany obecne w repozytorium

### Kuchnia i dziennik kalorii

Commity: `4d8aaba`, `3b8f0da`, `33e4fe1`, `9541c00`.

- Wartości odżywcze produktów, obliczanie kalorii przepisów oraz osobisty dziennik posiłków.
- Baza `Food` niezależna od spiżarni, wyszukiwarka, własne produkty i integracja z Open Food Facts.
- Skanowanie kodów w dzienniku, wyszukiwanie w Open Food Facts po nazwie i obsługa dwóch formatów danych odżywczych API.
- Cele kalorii i makroskładników, kopiowanie poprzedniego dnia lub posiłku oraz edycja wpisów.

Istotne decyzje: wpis posiłku zachowuje wartości odżywcze z chwili dodania. Zmiana ilości skaluje zapisane wartości, a nie aktualne dane produktu. Kopiowanie dodaje kolejne wpisy, więc ponowne użycie może tworzyć duplikaty.

Punkty wejścia: `cooking/views_diary.py`, `cooking/models.py`, `cooking/services/nutrition.py`, `cooking/services/foods.py`, `cooking/services/product_catalog.py`, `cooking/CLAUDE.md`.

### Trend portfela i dywidendy

Commity: `d5e96b5`, `1b2e0d2`.

- Trend portfela rozróżnia wpłacone środki i zysk, aby doładowanie rachunku nie było prezentowane jako wypracowany zysk.
- Podsumowanie dywidend pokazuje liczbę wypłat, kwoty netto i brutto oraz podatek, z uwzględnieniem konta i zakresu dat.

Istotne ograniczenia opisane przy zmianie: procent zysku jest prostym stosunkiem do wpłat, a nie stopą zwrotu ważoną czasem. Przeliczanie walut korzysta z bieżących kursów. Przy braku historii wpłat widok wraca do zmiany wartości portfela.

Punkty wejścia: `finance/portfolio_history.py`, `finance/templates/finance/brokerage.html`, `finance/CLAUDE.md`.

### Paragony

Commity: `2f69d2b`, `78ef008`.

- Skanowanie kodów na etapie sprawdzania paragonu; kod ma pierwszeństwo przed aliasem produktu.
- Cofanie dodania do spiżarni na podstawie `Receipt.confirm_log`.
- Starsze paragony bez dziennika mają cofanie przybliżone, zachowujące aliasy i kody. Wcześniejsza wiadomość Claude o braku cofania starych paragonów została zastąpiona zmianą `78ef008`.

Punkty wejścia: `cooking/views_receipts.py`, `cooking/services/receipt_review_scan.py`, `cooking/services/receipt_undo.py`, `cooking/CLAUDE.md`.

## Historyczne wyniki i kwestie do ponownego sprawdzenia

Poniższe informacje pochodzą z podsumowań Claude z 12–14 listopada 2026. Nie są wynikami bieżącej weryfikacji ani potwierdzeniem, że problem nadal występuje.

- Po ostatnim etapie dziennika kalorii Claude raportował 488 testów kuchni i dwa niepowodzenia przypisane brakowi `cryptography` na Macu oraz testowi marki na SQLite. PostgreSQL nie został wówczas sprawdzony dla tego etapu. Przy następnej pracy odtwórz wynik i ustal przyczynę; nie traktuj tych testów jako dopuszczalnych porażek.
- Dla zmian portfela Claude raportował przechodzące testy maklerki, ale brak sprawdzenia wykresu i układu czterech kafelków w przeglądarce.
- Dla skanowania i cofania paragonów raportowano 455 testów kuchni na PostgreSQL (5 pominiętych). Po poprawce cofania starszych paragonów raportowano jedynie 61 testów paragonów.
- Skaner aparatu wymagał sprawdzenia na telefonie; lokalny podgląd nie potwierdza działania aparatu.
- Ostatnie podsumowanie kalorii wskazywało wdrożenie i jednorazowe `sync_nutrition` jako przyszłe czynności. Ich obecny stan jest nieznany. Nie uruchamiaj ich automatycznie na podstawie tej notatki.

## Kontynuowanie pracy

Najpierw przeczytaj `AGENTS.md` i właściwe `CLAUDE.md`, sprawdź aktualny `git status` i kod związany z zadaniem. Użytkownik określa następny cel; wymienione wyżej zaległe sprawdzenia nie oznaczają polecenia wykonania wszystkich prac naraz.
