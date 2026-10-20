# cooking – przepisy, wspólna spiżarnia, listy zakupów, push

Modele: Recipe → RecipeStep, RecipeStepIngredient (`ingredient_items`, krok opcjonalny). PantryProduct (+ PantryMovement, ProductGroup, PantryCategory + PantryCategoryRule), ProductCatalogEntry / ProductCatalogQuota (cache Open Food Facts), ShoppingList → ShoppingListItem, ShopLayout, ShoppingSyncOperation (offline), PushSubscription, SentNotification, paragony: ReceiptStore, Receipt → ReceiptPhoto, ReceiptLine, ReceiptAlias (słownik domu: skrót z paragonu → produkt/grupa/pomijaj).
Przepisy edytuje tylko autor. Spiżarnia, kategorie i listy zakupów są wspólne dla całego domu.

Widoki:
- `views.py` (duży, czytaj grepem): przepisy (RecipeFormMixin, Add/Edit/Delete, CookView), spiżarnia (PantryListView, skaner kodów, zdjęcia), listy zakupów (Generate/AddShoppingSuggestions, ShoppingListDetailView).
- Strona spiżarni (`pantry.html`): pasek „dziś” (`partials/pantry_today.html`), przyklejony pasek szukania/chipów/sortowania (`pantry_toolbar.html`), kompaktowa lista domyślnie (`pantry_list.html` + `pantry_row.html`, −/+ i data w wierszu przez `data-partial`), kafelki `pantry_card.html` jako widok do wyboru i treść panelu szczegółów (offcanvas). Filtry, sortowanie, grupowanie i zapamiętanie (localStorage `spizarnia-lista`) w `static/cooking/js/pantry-list.js` na atrybutach `data-*` wiersza; flagi i klucze sortowania liczy `pantry_row_context` w `views.py`. Bez JS działa GET `q`/`status`/`category`. `?widok=grupy` – stary akordeon grup marek.
- `views_categories.py` – kategorie i reguły spiżarni.
- `views_receipts.py` – paragony: upload (zdjęcie/PDF)/wklejony tekst → odczyt → „Sprawdź” → spiżarnia; tryb „Zeskanuj kody” (`receipt-barcodes`, JS `receipt-barcodes.js`); ceny (`prices`, `product-prices`, `services/receipt_prices.py`).
- `shopping_app_views.py` – aplikacja zakupów offline (PWA, service worker, API snapshot/sync/complete, push).

Logika w `services/`:
- `categories.py` – reguły kategorii.
- `pantry_editing.py`, `pantry_quantities.py`, `pantry_sharing.py`.
- `pantry_batches.py` – terminy przydatności: `PantryBatch` (partie z datą), obowiązkowe dla kategorii z `requires_expiry` (nabiał); stan dalej w `current_quantity`, `reconcile` przycina partie od najkrótszego terminu (zużycie nie pyta o partię). „Zjedz najpierw”, „Uzupełnij termin”, push `termin` w `send_reminders`.
- `pantry_forecast.py` – prognoza zużycia i automatyczna lista.
- `product_catalog.py` – Open Food Facts, z limitem zapytań.
- `product_groups.py`.
- `recipe_form.py`, `recipe_pantry.py`, `recipe_units.py`.
- `receipt_parser.py` (czyste funkcje, próbki Lidl/Biedronka w testach), `receipt_ocr.py` (Tesseract `pol`, lokalnie; `RECEIPT_OCR_ENABLED`), `receipt_matching.py` (aliasy, propozycje), `receipts.py` (odczyt w wątku, zatwierdzanie, archiwum zdjęć). Odczyt zdjęć domyślnie przez `receipt_ai.py` (Claude API, Haiku; Tesseract tylko awaryjnie, gdy API nie odpowiada – wtedy przycisk „Odczytaj przez AI”; flaga `RECEIPT_AI_ENABLED` + `ANTHROPIC_API_KEY`, w testach wyłączony, HTTP mockowany); model zwraca tekst w układzie, który czyta ten sam parser. Paragon NIE tworzy wydatków w Finansach. Zdjęcia po zatwierdzeniu → `RECEIPT_ARCHIVE_DIR` → `deploy/receipts_upload.sh` (rclone move na Dysk Google).
- `shopping_sync.py`, `push.py` (pywebpush), `polish.py` (odmiana).

Komendy: `send_reminders` (cron), `process_receipts` (cron: zawieszone odczyty, osierocone zdjęcia), `learn_pantry_catalog`, `generate_vapid_keys`, `preview_shared_pantry`.
Testy: `cooking/tests.py`, `tests_categories.py`, `tests_recipe_form.py`, `tests_recipe_js.py` (Node), `tests_shopping_scan.py`, `tests_shopping_suggestions.py`, `tests_receipts.py`, `tests_pantry_list.py`.
Zależności: `utils` (undo, navigation). Zdjęcia użytkowników trafiają do `private_media` (`storage.py`). Domyślne kategorie: `category_defaults.py`.
