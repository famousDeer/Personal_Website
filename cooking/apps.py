from django.apps import AppConfig


class CookingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cooking'

    def ready(self):
        from django.db.models.signals import post_delete, post_save

        from .models import PantryCategory, PantryCategoryRule
        from .services.categories import invalidate

        # Zmiana także spoza strony "Kategorie" (panel admina, shell) ma od
        # razu działać w podpowiedziach kategorii.
        def _invalidate(**kwargs):
            invalidate()

        for model in (PantryCategory, PantryCategoryRule):
            post_save.connect(_invalidate, sender=model, dispatch_uid=f'pantry-taxonomy-save-{model.__name__}')
            post_delete.connect(_invalidate, sender=model, dispatch_uid=f'pantry-taxonomy-delete-{model.__name__}')
