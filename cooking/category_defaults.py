"""Domyślne kategorie spiżarni i reguły automatycznego przypisywania.

Od migracji cooking.0021 kategorie i reguły żyją w bazie (PantryCategory,
PantryCategoryRule) i domownicy edytują je na stronie "Kategorie" w spiżarni.
Ten moduł to stan początkowy: z niego migracja wypełnia bazę, a przycisk
"Przywróć domyślne reguły" odtwarza reguły. Kod aplikacji nie czyta stąd
kategorii bezpośrednio - tylko przez cooking.services.categories.

Moduł jest importowany przez migrację, więc musi zostać czystymi danymi
(bez importów modeli).
"""

# (kod, nazwa, grupa). Kod łączy kategorię z regułami domyślnymi także po
# zmianie nazwy przez użytkownika.
GROUP_FOOD = 'food'
GROUP_HOME = 'home'
GROUP_LABELS = {GROUP_FOOD: 'Spożywcze', GROUP_HOME: 'Dom'}

DEFAULT_CATEGORIES = (
    ('pieczywo', 'Pieczywo', GROUP_FOOD),
    ('nabial', 'Nabiał', GROUP_FOOD),
    ('mieso-ryby', 'Mięso i ryby', GROUP_FOOD),
    ('warzywa-owoce', 'Warzywa i owoce', GROUP_FOOD),
    ('mrozonki', 'Mrożonki', GROUP_FOOD),
    ('produkty-suche', 'Produkty suche', GROUP_FOOD),
    ('slodycze', 'Słodycze i przekąski', GROUP_FOOD),
    ('przyprawy', 'Przyprawy', GROUP_FOOD),
    ('konserwy', 'Konserwy', GROUP_FOOD),
    ('napoje', 'Napoje', GROUP_FOOD),
    ('chemia', 'Chemia domowa', GROUP_HOME),
    ('kosmetyki', 'Kosmetyki i higiena', GROUP_HOME),
    ('papierowe', 'Artykuły papierowe', GROUP_HOME),
    ('zwierzeta', 'Dla zwierząt', GROUP_HOME),
    ('leki', 'Leki i apteczka', GROUP_HOME),
)

# Reguły tagów: mapowanie kategorii z rodziny Open Facts na kategorie spiżarni.
#
# `categories_tags` produktu zawiera tag kanoniczny RAZEM ze wszystkimi jego
# przodkami w taksonomii (np. chleb tostowy niesie też `en:breads`
# i `en:cereals-and-potatoes`), więc reguły wystarczy oprzeć na węzłach
# pośrednich. Kolejność ma znaczenie - wygrywa pierwsza pasująca reguła:
#   * produkty niespożywcze są rozpoznawane najpierw, bo ich tagi są
#     najbardziej jednoznaczne,
#   * forma przechowywania (mrożonka, konserwa) wygrywa z rodzajem
#     produktu - mrożony szpinak to mrożonka, a nie warzywo,
#   * chleb leży w taksonomii pod "zbożami", dlatego Pieczywo jest przed
#     Produktami suchymi,
#   * kawa nie leży pod `en:beverages`, więc ma własny tag w Napojach.
#
# Wszystkie tagi są zweryfikowane w taksonomiach openfoodfacts-server
# (taxonomies/{food,beauty,product,petfood}/categories.txt). Kilka tagów
# spoza taksonomii zostało celowo: pojawiają się w danych jako tagi
# niekanoniczne, gdy produkt niespożywczy trafi do bazy żywności.
DEFAULT_TAG_RULES = (
    ('Leki i apteczka', frozenset({
        'dietary-supplements', 'vitamins-supplements', 'medicine-drugs',
        'medicines', 'first-aid', 'medical-tape-bandages', 'adhesive-bandages',
        'medical-tests',
    })),
    ('Dla zwierząt', frozenset({
        'animals-pet-supplies', 'pet-supplies', 'pet-food', 'cat-food',
        'dog-food', 'dog-and-cat-food', 'dry-pet-food', 'wet-pet-food',
        'cat-litter',
    })),
    ('Artykuły papierowe', frozenset({
        'household-paper-products', 'toilet-papers', 'toilet-paper',
        'paper-towels', 'facial-tissues', 'paper-napkins',
    })),
    ('Chemia domowa', frozenset({
        'household-chemicals', 'household-cleaning-supplies',
        'household-cleaning-products', 'all-purpose-cleaners',
        'dish-detergent-soap', 'dishwasher-tablets', 'dishwasher-cleaners',
        'detergents', 'laundry-detergent', 'laundry-detergents',
        'liquid-detergent', 'bleach', 'fabric-softeners-dryer-sheets',
        'fabric-stain-removers', 'fabric-refreshers', 'drain-cleaners',
        'air-fresheners', 'pest-control', 'cleaning-products',
        'dishwashing-products', 'household-cleaners',
    })),
    ('Kosmetyki i higiena', frozenset({
        'personal-care', 'cosmetics', 'open-beauty-facts', 'hygiene',
        'diapering', 'baby-wipes', 'soaps', 'bar-soaps', 'liquid-soaps',
        'toothpastes', 'mouthwash', 'shampoos', 'shower-gels', 'deodorants',
        'intimate-hygiene', 'tampons',
    })),
    ('Mrożonki', frozenset({
        'frozen-foods', 'frozen-desserts', 'ice-creams-and-sorbets',
    })),
    ('Konserwy', frozenset({
        'canned-foods', 'pickles', 'fruit-and-vegetable-preserves',
    })),
    # Bułka tarta leży w taksonomii pod pieczywem, a w kuchni stoi przy mące.
    ('Produkty suche', frozenset({'bread-crumbs'})),
    ('Pieczywo', frozenset({'breads', 'viennoiseries'})),
    # Napoje roślinne i margaryna stoją w lodówce obok mleka i masła.
    ('Nabiał', frozenset({
        'dairies', 'eggs', 'dairy-substitutes', 'margarines',
    })),
    ('Mięso i ryby', frozenset({
        'meats-and-their-products', 'meats', 'poultries', 'sausages', 'hams',
        'seafood', 'fishes',
    })),
    ('Słodycze i przekąski', frozenset({
        'snacks', 'sweet-snacks', 'salty-snacks', 'confectioneries',
        'chocolates', 'candies', 'biscuits-and-cakes', 'biscuits-and-crackers',
        'chips-and-fries', 'crisps', 'popcorn', 'desserts', 'sweet-spreads',
    })),
    ('Napoje', frozenset({
        'beverages-and-beverages-preparations', 'beverages', 'waters',
        'juices-and-nectars', 'alcoholic-beverages', 'plant-based-beverages',
        'hot-beverages', 'coffees', 'teas', 'syrups',
        'cocoa-and-chocolate-powders',
    })),
    ('Warzywa i owoce', frozenset({
        'fruits', 'vegetables', 'fresh-fruits', 'fresh-vegetables', 'mushrooms',
        'potatoes', 'dried-fruits',
    })),
    # Olej stoi przy occie i sosach, dlatego trafia do Przypraw. Masło
    # i margarynę łapie wcześniej Nabiał, mimo że też są pod `en:fats`.
    ('Przyprawy', frozenset({
        'condiments', 'sauces', 'herbs-and-spices', 'spices', 'herbs', 'salts',
        'vinegars', 'broths', 'fats', 'vegetable-oils', 'olive-oils',
    })),
    ('Produkty suche', frozenset({
        'cereals-and-potatoes', 'cereals-and-their-products', 'cereal-grains',
        'pastas', 'rices', 'flours', 'groats', 'breakfast-cereals', 'legumes',
        'nuts', 'seeds', 'sugars', 'sweeteners', 'starches', 'cooking-helpers',
        'baking-powders', 'dried-products',
    })),
)


# Reguły tekstowe działają, gdy tagi nie rozstrzygają (produkt bez kategorii
# w bazie albo wpisany ręcznie). Gwiazdka oznacza dopasowanie początku słowa,
# a fraza z kilku słów musi wystąpić w całości. Oprócz polskich słów są
# najczęstsze angielskie, niemieckie i czeskie, bo właśnie takie nazwy
# przychodzą z bazy dla produktów importowanych.
#
# Kolejność chroni przed pułapkami wieloznaczności:
#   "tabletki do zmywarki" to chemia, zanim zadziała cokolwiek o lekach,
#   "herbatniki" to słodycze, zanim "herbat*" uzna je za napój,
#   "masło do ciała" i "mleczko czyszczące" nie trafią do nabiału,
#   "bułka tarta" nie trafi do pieczywa,
#   "sos pomidorowy" to przyprawa, zanim "pomidor*" uzna go za warzywo,
#   a Przyprawy są po Napojach, bo 'herb*' złapałby "herbatę".
DEFAULT_KEYWORD_RULES = (
    ('Chemia domowa', (
        'płyn do naczyń', 'do mycia naczyń', 'do mycia podłóg',
        'do mycia szyb', 'do czyszczenia', 'do zmywarki', 'do prania',
        'do płukania tkanin', 'proszek do prania', 'odplamiacz*', 'wybielacz*',
        'środek czyszczący', 'czyszcząc*', 'odtłuszczacz*', 'odkamieniacz*',
        'odświeżacz*', 'udrażniacz*', 'worki na śmieci', 'zmywak*',
        'detergent*', 'cleaner*', 'cleaning', 'laundry', 'dishwash*',
        'dish soap', 'bleach', 'waschmittel', 'spülmittel', 'reiniger',
        'weichspüler', 'prací', 'na nádobí',
    )),
    ('Artykuły papierowe', (
        'papier toaletowy', 'ręcznik papierowy', 'ręczniki papierowe',
        'ręcznik kuchenny', 'ręczniki kuchenne', 'chusteczki higieniczne',
        'serwetki', 'toilet paper', 'toilet tissue', 'paper towel*',
        'kitchen roll', 'toilettenpapier', 'küchenrolle', 'toaletní papír',
    )),
    ('Kosmetyki i higiena', (
        'szampon*', 'odżywka do włosów', 'pasta do zębów', 'szczoteczk*',
        'nić dentystyczna', 'płyn do płukania jamy ustnej', 'dezodorant*',
        'antyperspirant*', 'żel pod prysznic', 'mydło', 'mydła', 'mydeł',
        'do ciała', 'krem do rąk', 'krem do twarzy', 'balsam', 'balsamy',
        'micelarn*', 'podpask*', 'tampon*', 'wkładki higieniczne', 'pieluch*',
        'pieluszk*', 'chusteczki nawilżane', 'płatki kosmetyczne',
        'patyczki higieniczne', 'maszynk*', 'do golenia', 'woda toaletowa',
        'perfum*', 'shampoo*', 'toothpaste*', 'deodorant*', 'shower gel',
        'soap', 'body lotion', 'duschgel', 'zahnpasta', 'zubní pasta',
        'šampon', 'sprchový gel',
    )),
    ('Dla zwierząt', (
        'karma', 'karmy', 'dla kota', 'dla kotów', 'dla psa', 'dla psów',
        'żwirek', 'cat food', 'dog food', 'pet food', 'katzenfutter',
        'hundefutter', 'whiskas', 'pedigree', 'purina', 'sheba',
    )),
    ('Leki i apteczka', (
        'suplement diety', 'tabletki powlekane', 'tabletki musujące',
        'ibuprofen*', 'paracetamol*', 'na kaszel', 'na gardło', 'na ból',
        'przeciwbólow*', 'probiotyk*', 'witamina', 'magnez',
        'plastry opatrunkowe', 'plaster opatrunkowy', 'bandaż*',
        'woda utleniona', 'dietary supplement', 'nahrungsergänzungsmittel',
    )),
    ('Mrożonki', (
        'frozen', 'mrożon*', 'ice cream', 'lody', 'tiefkühl*', 'mražen*',
    )),
    ('Konserwy', (
        'canned', 'konserw*', 'w puszce', 'dżem*', 'konfitur*', 'marynowan*',
        'kiszon*',
    )),
    ('Produkty suche', ('bułka tarta', 'masło orzechowe', 'peanut butter')),
    ('Pieczywo', (
        'chleb*', 'bułk*', 'bagietk*', 'rogal*', 'croissant*',
        'pieczywo', 'bread', 'toast*', 'brot', 'brötchen', 'chléb',
    )),
    ('Nabiał', (
        'dairy', 'dairies', 'milk', 'cheese*', 'yogurt*', 'yoghurt*', 'butter',
        'mleko', 'mleka', 'nabiał', 'ser', 'sery', 'sera', 'serów', 'serek',
        'jogurt*', 'śmietan*', 'kefir*', 'maślank*', 'twaróg', 'twarożek',
        'masło', 'margaryn*', 'mozzarell*', 'jaja', 'jajka', 'milch', 'käse',
        'joghurt*', 'mléko', 'sýr',
    )),
    ('Mięso i ryby', (
        'meat*', 'fish', 'seafood', 'chicken*', 'poultry', 'mięso', 'mięsa',
        'ryb*', 'kiełbas*', 'szynk*', 'boczek', 'parówk*', 'kurczak*',
        'wędlin*', 'łosoś', 'łososia', 'tuńczyk*', 'śledź', 'śledzie',
        'makrel*', 'indyk*', 'pasztet*', 'fleisch', 'wurst', 'schinken',
    )),
    ('Słodycze i przekąski', (
        'czekolad*', 'baton*', 'cukierk*', 'żelk*', 'ciastk*', 'ciasteczk*',
        'herbatnik*', 'wafel*', 'wafl*', 'chips*', 'chrupk*', 'paluszk*',
        'popcorn', 'krakers*', 'pralin*', 'lizak*', 'chocolate*', 'candy',
        'candies', 'biscuit*', 'cookie*', 'crisps', 'snack*', 'schokolade',
        'kekse', 'čokolád*',
    )),
    ('Napoje', (
        'beverage*', 'drink*', 'juice*', 'water', 'waters', 'soda*', 'coffee',
        'tea', 'teas', 'beer', 'wine', 'napój', 'napoje', 'napoj*', 'sok',
        'soki', 'soków', 'nektar*', 'lemoniad*', 'woda', 'wody', 'kawa', 'kawy',
        'herbat*', 'piwo', 'piwa', 'wino', 'wina', 'syrop*', 'saft', 'wasser',
        'getränk*', 'kaffee', 'tee', 'bier', 'wein', 'nápoj', 'káva',
    )),
    ('Przyprawy', (
        'spice*', 'seasoning*', 'herb*', 'sauce*', 'vinegar*', 'oil',
        'ketchup*', 'mayonnaise', 'mustard', 'przypraw*', 'zioł*', 'sól',
        'soli', 'pieprz*', 'ocet', 'octu', 'olej*', 'oliwa', 'oliwy',
        'majonez*', 'musztard*', 'sos', 'sosy', 'sosu', 'bulion*', 'rosoł*',
        'salz', 'essig', 'gewürz*', 'koření',
    )),
    ('Warzywa i owoce', (
        'fruit*', 'vegetable*', 'owoc*', 'warzyw*', 'apple*', 'banana*',
        'tomato*', 'jabłk*', 'banan*', 'pomidor*', 'ziemniak*', 'marchew*',
        'cebul*', 'czosn*', 'sałat*', 'cytryn*', 'grzyb*', 'pieczark*', 'obst',
        'gemüse', 'kartoffel*', 'ovoce', 'zelenina',
    )),
    ('Produkty suche', (
        'pasta', 'pastas', 'rice', 'cereal*', 'flour*', 'sugar*', 'makaron*',
        'ryż', 'ryżu', 'mąk*', 'cukier', 'cukru', 'kasz*', 'płatk*',
        'soczewic*', 'fasol*', 'groch*', 'orzech*', 'drożdż*',
        'proszek do pieczenia', 'nasion*', 'mehl', 'zucker', 'nudeln', 'reis',
        'rýže', 'mouka',
    )),
)
