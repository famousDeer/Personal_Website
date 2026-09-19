# Kategorie spiżarni pogrupowane tak, jak pokazują się w formularzach.
#
# Nazwy są zapisywane jako zwykły tekst w PantryProduct.category,
# ShoppingListItem.category i RecipeStepIngredient.category, więc zmiana
# istniejącej nazwy wymaga migracji danych. Nowe kategorie można dopisywać
# bez niej - istniejące produkty zachowują swoje.
PANTRY_CATEGORY_OTHER = 'Inne'

PANTRY_CATEGORY_GROUPS = (
    ('Spożywcze', (
        'Pieczywo',
        'Nabiał',
        'Mięso i ryby',
        'Warzywa i owoce',
        'Mrożonki',
        'Produkty suche',
        'Słodycze i przekąski',
        'Przyprawy',
        'Konserwy',
        'Napoje',
    )),
    ('Dom', (
        'Chemia domowa',
        'Kosmetyki i higiena',
        'Artykuły papierowe',
        'Dla zwierząt',
        'Leki i apteczka',
    )),
)

PANTRY_CATEGORIES = tuple(
    category for _, categories in PANTRY_CATEGORY_GROUPS for category in categories
) + (PANTRY_CATEGORY_OTHER,)

# Tymczasowo wyłączone na czas nauki użytkowników. Zmień na True, aby ponownie
# włączyć automatyczną akcję po zeskanowaniu produktu.
PANTRY_SCANNER_AUTO_ACTION_ENABLED = False
PANTRY_SCANNER_AUTO_ACTION_TIMEOUT_MS = 5000
