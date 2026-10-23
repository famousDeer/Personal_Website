"""Oceny produktów spiżarni (PantryRating): zapis i podsumowanie dla listy.

Jedna ocena na produkt i domownika. Średnia z ocen domu pomaga zdecydować,
czy kupić produkt ponownie - widać ją w wierszu listy i w panelu szczegółów.
"""
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from ..models import PantryRating

NOTE_MAX_LENGTH = PantryRating._meta.get_field('note').max_length


def parse_stars(raw) -> int:
    try:
        stars = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError('Wybierz od 1 do 5 gwiazdek.')
    if not 1 <= stars <= 5:
        raise ValueError('Wybierz od 1 do 5 gwiazdek.')
    return stars


def save_rating(product, user, stars, note='') -> PantryRating:
    rating, _ = PantryRating.objects.update_or_create(
        product=product,
        user=user,
        defaults={'stars': parse_stars(stars), 'note': (note or '').strip()[:NOTE_MAX_LENGTH]},
    )
    return rating


def rating_summaries(product_ids, user) -> dict:
    """Podsumowanie ocen dla wielu produktów naraz (jedno zapytanie).

    Zwraca {product_id: {'average', 'count', 'ratings', 'mine'}}; produkty
    bez ocen nie mają wpisu. ``ratings`` to oceny domowników, najnowsze najpierw.
    """
    by_product = defaultdict(list)
    for rating in PantryRating.objects.filter(product_id__in=product_ids).select_related('user'):
        by_product[rating.product_id].append(rating)
    summaries = {}
    for product_id, ratings in by_product.items():
        average = Decimal(sum(rating.stars for rating in ratings)) / len(ratings)
        summaries[product_id] = {
            'average': average.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP),
            'count': len(ratings),
            'ratings': ratings,
            'mine': next((rating for rating in ratings if rating.user_id == user.pk), None),
        }
    return summaries


def merge_ratings(source, target):
    """Przy łączeniu duplikatów: ocena domownika przechodzi na produkt docelowy;
    gdy ocenił oba, zostaje nowsza."""
    target_ratings = {rating.user_id: rating for rating in PantryRating.objects.filter(product=target)}
    for rating in PantryRating.objects.filter(product=source):
        existing = target_ratings.get(rating.user_id)
        if existing is None:
            rating.product = target
            rating.save(update_fields=['product'])
        elif rating.updated_at > existing.updated_at:
            PantryRating.objects.filter(pk=existing.pk).update(
                stars=rating.stars, note=rating.note, updated_at=rating.updated_at,
            )
