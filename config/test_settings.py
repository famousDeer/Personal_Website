from .settings import *  # noqa: F403,F401

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'test_db.sqlite3',  # noqa: F405
    }
}

TRAVEL_GEOCODING_ENABLED = False
OPEN_FOOD_FACTS_ENABLED = False
