# cooking – przepisy, wspólna spiżarnia, listy zakupów, push

Modele: Recipe → RecipeStep, RecipeStepIngredient (`ingredient_items`, krok opcjonalny). PantryProduct (+ PantryMovement, ProductGroup, PantryCategory + PantryCategoryRule), ProductCatalogEntry / ProductCatalogQuota (cache Open Food Facts), ShoppingList → ShoppingListItem, ShopLayout, ShoppingSyncOperation (offline), PushSubscription, SentNotification.
Przepisy edytuje tylko autor. Spiżarnia, kategorie i listy zakupów są wspólne dla całego domu.

Widoki:
- `views.py` (duży, czytaj grepem): przepisy (RecipeFormMixin, Add/Edit/Delete, CookView), spiżarnia (PantryListView, skaner kodów, zdjęcia), listy zakupów (Generate/AddShoppingSuggestions, ShoppingListDetailView).
- `views_categories.py` – kategorie i reguły spiżarni.
- `shopping_app_views.py` – aplikacja zakupów offline (PWA, service worker, API snapshot/sync/complete, push).

Logika w `services/`:
- `categories.py` – reguły kategorii.
- `pantry_editing.py`, `pantry_quantities.py`, `pantry_sharing.py`.
- `pantry_forecast.py` – prognoza zużycia i automatyczna lista.
- `product_catalog.py` – Open Food Facts, z limitem zapytań.
- `product_groups.py`.
- `recipe_form.py`, `recipe_pantry.py`, `recipe_units.py`.
- `shopping_sync.py`, `push.py` (pywebpush), `polish.py` (odmiana).

Komendy: `send_reminders` (cron), `learn_pantry_catalog`, `generate_vapid_keys`, `preview_shared_pantry`.
Testy: `cooking/tests.py`, `tests_categories.py`, `tests_recipe_form.py`, `tests_recipe_js.py` (Node), `tests_shopping_scan.py`, `tests_shopping_suggestions.py`.
Zależności: `utils` (undo, navigation). Zdjęcia użytkowników trafiają do `private_media` (`storage.py`). Domyślne kategorie: `category_defaults.py`.
