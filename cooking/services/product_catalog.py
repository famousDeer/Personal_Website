"""Server-side Open Food Facts lookup with a durable, shared cache."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from ..constants import PANTRY_CATEGORIES
from ..models import PantryProduct, ProductCatalogEntry, ProductCatalogQuota


OPEN_FOOD_FACTS_ROOT = 'https://world.openfoodfacts.org'
OPEN_FOOD_FACTS_SOURCE = ProductCatalogEntry.SOURCE_OPEN_FOOD_FACTS
OPEN_FOOD_FACTS_FIELDS = (
    'code',
    'lang',
    'product_type',
    'product_name',
    'product_name_pl',
    'generic_name',
    'generic_name_pl',
    'brands',
    'quantity',
    'product_quantity',
    'product_quantity_unit',
    'categories',
    'categories_tags',
    'ingredients_text',
    'ingredients_text_pl',
    'selected_images',
    'image_front_small_url',
    'image_front_url',
    'last_updated_t',
    'last_modified_t',
)
SUPPORTED_BARCODE_PATTERN = re.compile(r'^\d{8,14}$')
ALLOWED_API_HOSTS = {
    'world.openfoodfacts.org',
    'world.openbeautyfacts.org',
    'world.openpetfoodfacts.org',
    'world.openproductsfacts.org',
}
ALLOWED_IMAGE_HOSTS = {
    'images.openfoodfacts.org',
    'images.openbeautyfacts.org',
    'images.openpetfoodfacts.org',
    'images.openproductsfacts.org',
}
PRODUCT_TYPE_HOSTS = {
    'food': 'world.openfoodfacts.org',
    'beauty': 'world.openbeautyfacts.org',
    'petfood': 'world.openpetfoodfacts.org',
    'product': 'world.openproductsfacts.org',
}
MAX_JSON_RESPONSE_BYTES = 128 * 1024
REFRESH_LOCK_SECONDS = 30

# Open Food Facts returns canonical category tags together with their taxonomy
# ancestors.  The order below is intentional: a specific preparation or storage
# form wins over the broader ingredient family (for example canned fish is a
# preserve and ice cream is frozen food).
CATEGORY_TAG_RULES = (
    ('Chemia domowa', frozenset({
        'cleaning-products', 'household-cleaning-products', 'household-cleaners',
        'detergents', 'laundry-detergents', 'dishwashing-products', 'soaps',
    })),
    ('Mrożonki', frozenset({
        'frozen-foods', 'frozen-desserts', 'frozen-pizzas',
        'frozen-ready-made-meals', 'frozen-vegetables', 'frozen-fruits',
        'ice-creams', 'ice-creams-and-sorbets',
    })),
    ('Konserwy', frozenset({
        'canned-foods', 'canned-plant-based-foods', 'canned-vegetables',
        'canned-fruits', 'canned-meats', 'canned-fishes', 'preserves', 'pickles',
    })),
    ('Przyprawy', frozenset({
        'spices', 'herbs', 'seasonings', 'salts', 'condiments', 'sauces',
        'vinegars',
    })),
    ('Nabiał', frozenset({
        'dairies', 'dairy-products', 'fermented-dairy-products',
        'fermented-milk-products', 'milks', 'cheeses', 'yogurts', 'butters',
        'creams', 'eggs', 'dairy-desserts', 'milk-based-beverages',
    })),
    ('Napoje', frozenset({
        'beverages', 'waters', 'juices', 'juices-and-nectars', 'fruit-juices',
        'soft-drinks', 'sodas', 'coffees', 'teas', 'alcoholic-beverages',
        'non-alcoholic-beverages', 'plant-based-beverages',
    })),
    ('Mięso i ryby', frozenset({
        'meats', 'meat-based-products', 'poultry', 'poultries', 'fishes',
        'seafood', 'molluscs', 'crustaceans', 'charcuteries', 'sausages',
    })),
    ('Warzywa i owoce', frozenset({
        'fruits', 'vegetables', 'fresh-fruits', 'fresh-vegetables',
        'fruits-and-vegetables-based-foods', 'fruit-and-vegetable-based-foods',
        'mushrooms', 'potatoes',
    })),
    ('Produkty suche', frozenset({
        'cereal-grains', 'cereal-products', 'pastas', 'rices', 'rice', 'flours',
        'breads', 'breakfast-cereals', 'legumes', 'pulses', 'nuts', 'seeds',
        'sugars', 'confectioneries', 'snacks', 'chocolates', 'spreads',
        'biscuits-and-cakes', 'candies',
    })),
)

CATEGORY_TEXT_RULES = (
    ('Chemia domowa', (
        'detergent*', 'cleaner*', 'cleaning', 'laundry', 'dishwashing', 'soap*',
        'środek czyszczący', 'proszek do prania', 'płyn do naczyń',
    )),
    ('Mrożonki', ('frozen', 'mrożon*', 'ice cream', 'lody')),
    ('Konserwy', ('canned', 'preserve*', 'konserw*', 'puszk*')),
    ('Przyprawy', ('spice*', 'seasoning*', 'herb*', 'przypraw*', 'zioł*')),
    ('Nabiał', (
        'dairy', 'dairies', 'milk*', 'cheese*', 'yogurt*', 'yoghurt*', 'butter*',
        'mleko', 'nabiał', 'ser', 'sery', 'sera', 'serów', 'jogurt*', 'śmietan*',
    )),
    ('Napoje', (
        'beverage*', 'drink*', 'juice*', 'water', 'waters', 'soda*', 'napój',
        'napoje', 'napoj*', 'sok', 'soki', 'soków', 'woda', 'wody',
    )),
    ('Mięso i ryby', (
        'meat*', 'fish*', 'seafood', 'mięso', 'ryb*', 'chicken*', 'poultry',
    )),
    ('Warzywa i owoce', (
        'fruit*', 'vegetable*', 'owoc*', 'warzyw*', 'apple*', 'banana*',
        'tomato*',
    )),
    ('Produkty suche', (
        'pasta*', 'rice', 'rices', 'cereal*', 'flour*', 'sugar*', 'snack*',
        'chocolate*', 'spread*', 'makaron*', 'ryż', 'mąk*', 'cukier', 'kasz*',
        'płatk*',
    )),
)


class CatalogProductNotFound(Exception):
    """The upstream catalog has no product for the barcode."""


class CatalogUnavailable(Exception):
    """The upstream catalog could not be reached safely."""

    def __init__(self, retry_after_seconds=60):
        super().__init__('Product catalog unavailable')
        self.retry_after_seconds = max(1, int(retry_after_seconds))


@dataclass(frozen=True)
class CatalogLookupResult:
    status: str
    cache_state: str
    entry: ProductCatalogEntry | None = None


class _OpenFactsRedirectHandler(HTTPRedirectHandler):
    max_redirections = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved_url = urljoin(req.full_url, newurl)
        parsed = urlparse(resolved_url)
        if parsed.scheme != 'https' or parsed.hostname not in ALLOWED_API_HOSTS:
            raise CatalogUnavailable()
        return super().redirect_request(req, fp, code, msg, headers, resolved_url)


_URL_OPENER = build_opener(_OpenFactsRedirectHandler())


def _clean_text(value, max_length=None):
    if not isinstance(value, str):
        return ''
    cleaned = ' '.join(value.split()).strip()
    if max_length:
        return cleaned[:max_length]
    return cleaned


def _first_text(product, fields, max_length=None):
    for field in fields:
        value = _clean_text(product.get(field), max_length=max_length)
        if value:
            return value
    return ''


def _safe_retry_after(headers):
    try:
        return min(3600, max(60, int(headers.get('Retry-After', '60'))))
    except (TypeError, ValueError):
        return 60


def _read_limited(response, max_bytes):
    content_length = response.headers.get('Content-Length')
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise CatalogUnavailable()
        except ValueError:
            raise CatalogUnavailable() from None
    payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise CatalogUnavailable()
    return payload


def _open_url(url, max_bytes, accept):
    request = Request(
        url,
        headers={
            'Accept': accept,
            'User-Agent': settings.OPEN_FOOD_FACTS_USER_AGENT,
        },
    )
    try:
        with _URL_OPENER.open(request, timeout=settings.OPEN_FOOD_FACTS_TIMEOUT) as response:
            return _read_limited(response, max_bytes), response.headers.get('Content-Type', '')
    except HTTPError as exc:
        if exc.code == 404:
            raise CatalogProductNotFound() from exc
        raise CatalogUnavailable(_safe_retry_after(exc.headers)) from exc
    except CatalogUnavailable:
        raise
    except (TimeoutError, URLError, OSError) as exc:
        raise CatalogUnavailable() from exc


def _request_product(barcode):
    query = urlencode({
        'product_type': 'all',
        'cc': 'pl',
        'lc': 'pl',
        'tags_lc': 'pl',
        'fields': ','.join(OPEN_FOOD_FACTS_FIELDS),
    })
    url = f'{OPEN_FOOD_FACTS_ROOT}/api/v3.6/product/{quote(barcode, safe="")}.json?{query}'
    raw_payload, _ = _open_url(url, MAX_JSON_RESPONSE_BYTES, 'application/json')
    try:
        payload = json.loads(raw_payload.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogUnavailable() from exc
    if not isinstance(payload, dict):
        raise CatalogUnavailable()

    result = payload.get('result')
    result_id = result.get('id') if isinstance(result, dict) else ''
    if result_id == 'product_not_found' or payload.get('status') == 'failure':
        raise CatalogProductNotFound()
    if result_id != 'product_found' and payload.get('status') != 'success':
        raise CatalogUnavailable()
    if not isinstance(payload.get('product'), dict):
        raise CatalogUnavailable()
    return payload['product']


def _safe_image_url(value):
    cleaned = _clean_text(value, max_length=500)
    if not cleaned:
        return ''
    parsed = urlparse(cleaned)
    if parsed.scheme != 'https' or parsed.hostname not in ALLOWED_IMAGE_HOSTS:
        return ''
    return cleaned


def _selected_front_image(product):
    selected_images = product.get('selected_images')
    if isinstance(selected_images, dict):
        front = selected_images.get('front')
        if isinstance(front, dict):
            for size in ('small', 'thumb', 'display'):
                variants = front.get(size)
                if not isinstance(variants, dict):
                    continue
                language_order = ('pl', _clean_text(product.get('lang'), max_length=8), 'en')
                for language in language_order:
                    if language:
                        safe_url = _safe_image_url(variants.get(language))
                        if safe_url:
                            return safe_url
                for value in variants.values():
                    safe_url = _safe_image_url(value)
                    if safe_url:
                        return safe_url
    return _safe_image_url(product.get('image_front_small_url')) or _safe_image_url(
        product.get('image_front_url')
    )


def _normalized_quantity(product):
    raw_value = product.get('product_quantity')
    raw_unit = _clean_text(product.get('product_quantity_unit'), max_length=10).casefold()
    try:
        quantity = Decimal(str(raw_value))
        if not quantity.is_finite() or quantity <= 0:
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError):
        return Decimal('1.00'), PantryProduct.UNIT_PACKAGE

    conversions = {
        'g': (Decimal('1'), PantryProduct.UNIT_GRAM),
        'kg': (Decimal('1'), PantryProduct.UNIT_KILOGRAM),
        'ml': (Decimal('1'), PantryProduct.UNIT_MILLILITER),
        'cl': (Decimal('10'), PantryProduct.UNIT_MILLILITER),
        'dl': (Decimal('100'), PantryProduct.UNIT_MILLILITER),
        'l': (Decimal('1'), PantryProduct.UNIT_LITER),
    }
    conversion = conversions.get(raw_unit)
    if not conversion:
        return Decimal('1.00'), PantryProduct.UNIT_PACKAGE
    quantity = (quantity * conversion[0]).quantize(Decimal('0.01'))
    if quantity <= 0 or quantity > Decimal('99999999.99'):
        return Decimal('1.00'), PantryProduct.UNIT_PACKAGE
    return quantity, conversion[1]


def _category_tags(value):
    if isinstance(value, str):
        values = value.split(',')
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        return []

    tags = []
    seen = set()
    for value in values:
        cleaned = _clean_text(value, max_length=120).casefold()
        if not cleaned:
            continue
        cleaned = re.sub(r'^[a-z]{2,3}:', '', cleaned)
        cleaned = re.sub(r'[^a-z0-9]+', '-', cleaned).strip('-')
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            tags.append(cleaned)
    return tags


def _category_from_tags(category_tags):
    tags = set(_category_tags(category_tags))
    for category, known_tags in CATEGORY_TAG_RULES:
        if tags & known_tags:
            return category
    return ''


def _category_tag_storage_values(value, raw_external_category=''):
    if not isinstance(value, list):
        return []
    cleaned_values = []
    seen = set()
    for raw_value in value:
        cleaned = _clean_text(raw_value, max_length=120)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        cleaned_values.append(cleaned)

    def priority(raw_value):
        normalized_tags = set(_category_tags([raw_value]))
        for index, (_, known_tags) in enumerate(CATEGORY_TAG_RULES):
            if normalized_tags & known_tags:
                return index
        return len(CATEGORY_TAG_RULES)

    recognized_values = [
        raw_value for raw_value in cleaned_values
        if priority(raw_value) < len(CATEGORY_TAG_RULES)
    ]
    other_values = [
        raw_value for raw_value in cleaned_values
        if priority(raw_value) == len(CATEGORY_TAG_RULES)
    ]
    recognized_values.sort(key=priority)
    external_category = _clean_text(raw_external_category, max_length=255)
    if external_category and external_category not in seen:
        recognized_values.append(external_category)
    return recognized_values + other_values


def _text_has_keyword(searchable, keyword):
    normalized_keyword = ' '.join(keyword.rstrip('*').casefold().split())
    if not normalized_keyword:
        return False
    if keyword.endswith('*'):
        return any(token.startswith(normalized_keyword) for token in searchable.split())
    return f' {normalized_keyword} ' in f' {searchable} '


def _suggest_category(
    product_name,
    description,
    external_category,
    category_tags=None,
    product_type='food',
):
    normalized_product_type = _clean_text(product_type, max_length=20).casefold() or 'food'
    if normalized_product_type in {'beauty', 'petfood'}:
        return 'Inne'

    tag_category = _category_from_tags(category_tags or _category_tags(external_category))
    if tag_category and (
        normalized_product_type != 'product' or tag_category == 'Chemia domowa'
    ):
        return tag_category

    searchable = f'{product_name} {description} {external_category}'.casefold()
    searchable = ' '.join(re.sub(r'[^\wąćęłńóśźż]+', ' ', searchable).split())
    rules = CATEGORY_TEXT_RULES
    if normalized_product_type == 'product':
        rules = CATEGORY_TEXT_RULES[:1]
    for category, keywords in rules:
        if any(_text_has_keyword(searchable, keyword) for keyword in keywords):
            return category
    return 'Inne'


def _source_timestamp(product):
    raw_timestamp = product.get('last_updated_t') or product.get('last_modified_t')
    try:
        timestamp = float(raw_timestamp)
        if timestamp <= 0:
            return None
        return datetime.fromtimestamp(timestamp, tz=datetime_timezone.utc)
    except (OverflowError, TypeError, ValueError):
        return None


def _normalize_product(product, lookup_barcode):
    canonical_barcode = _clean_text(product.get('code'), max_length=64) or lookup_barcode
    product_name = _first_text(
        product,
        ('product_name_pl', 'generic_name_pl', 'product_name', 'generic_name'),
        max_length=160,
    )
    description = _first_text(product, ('generic_name_pl', 'generic_name'), max_length=2000)
    if description.casefold() == product_name.casefold():
        description = ''
    ingredients = _first_text(
        product,
        ('ingredients_text_pl', 'ingredients_text'),
        max_length=4000,
    )
    brand = _clean_text(product.get('brands'), max_length=160)
    raw_external_category = _clean_text(product.get('categories'), max_length=255)
    raw_category_tags = product.get('categories_tags')
    category_tags = _category_tags(raw_category_tags)
    external_category = _clean_text(
        ', '.join(_category_tag_storage_values(raw_category_tags, raw_external_category)),
        max_length=255,
    )
    if not external_category:
        external_category = raw_external_category
    quantity, unit = _normalized_quantity(product)
    product_type = _clean_text(product.get('product_type'), max_length=20) or 'food'
    product_host = PRODUCT_TYPE_HOSTS.get(product_type, PRODUCT_TYPE_HOSTS['food'])
    return {
        'canonical_barcode': canonical_barcode,
        'product_type': product_type,
        'product_name': product_name,
        'brand': brand,
        'description': description,
        'ingredients': ingredients,
        'external_category': external_category,
        'suggested_category': _suggest_category(
            product_name,
            description,
            raw_external_category,
            category_tags=category_tags,
            product_type=product_type,
        ),
        'quantity_text': _clean_text(product.get('quantity'), max_length=80),
        'suggested_quantity_per_scan': quantity,
        'suggested_unit': unit,
        'image_source_url': _selected_front_image(product),
        'attribution_url': f'https://{product_host}/product/{quote(canonical_barcode, safe="")}',
        'source_updated_at': _source_timestamp(product),
    }


def suggested_category_for_catalog_entry(entry):
    if not entry or entry.status != ProductCatalogEntry.STATUS_FOUND:
        return ''
    category = _suggest_category(
        entry.product_name,
        entry.description,
        entry.external_category,
        category_tags=_category_tags(entry.external_category),
        product_type=entry.product_type,
    )
    if category not in PANTRY_CATEGORIES:
        category = 'Inne'
    if entry.suggested_category != category:
        ProductCatalogEntry.objects.filter(pk=entry.pk).update(
            suggested_category=category,
            updated_at=timezone.now(),
        )
        entry.suggested_category = category
    return category


def _result_from_entry(entry, cache_state):
    if entry.status == ProductCatalogEntry.STATUS_FOUND:
        suggested_category_for_catalog_entry(entry)
        status = 'found' if entry.product_name else 'found_incomplete'
    elif entry.status == ProductCatalogEntry.STATUS_NOT_FOUND:
        status = 'not_found'
    else:
        status = 'unavailable'
    return CatalogLookupResult(status=status, cache_state=cache_state, entry=entry)


def _claim_refresh(barcode, now):
    with transaction.atomic():
        entry, _ = ProductCatalogEntry.objects.get_or_create(
            source=OPEN_FOOD_FACTS_SOURCE,
            lookup_barcode=barcode,
        )
        entry = ProductCatalogEntry.objects.select_for_update().get(pk=entry.pk)
        if entry.valid_until and entry.valid_until > now:
            return entry, False
        if entry.retry_after and entry.retry_after > now:
            return entry, False
        lock_cutoff = now - timedelta(seconds=REFRESH_LOCK_SECONDS)
        if entry.refresh_started_at and entry.refresh_started_at > lock_cutoff:
            return entry, False
        entry.refresh_started_at = now
        entry.save(update_fields=['refresh_started_at', 'updated_at'])
        return entry, True


def _take_quota_slot(now):
    limit = max(1, int(settings.OPEN_FOOD_FACTS_RATE_LIMIT))
    with transaction.atomic():
        quota, _ = ProductCatalogQuota.objects.get_or_create(
            source=OPEN_FOOD_FACTS_SOURCE,
        )
        quota = ProductCatalogQuota.objects.select_for_update().get(pk=quota.pk)
        if now - quota.window_started_at >= timedelta(minutes=1):
            quota.window_started_at = now
            quota.request_count = 0
        if quota.request_count >= limit:
            quota.save(update_fields=['window_started_at', 'request_count'])
            return False
        quota.request_count += 1
        quota.save(update_fields=['window_started_at', 'request_count'])
        return True


def _release_refresh(entry, retry_after_seconds=60):
    now = timezone.now()
    ProductCatalogEntry.objects.filter(pk=entry.pk).update(
        refresh_started_at=None,
        retry_after=now + timedelta(seconds=retry_after_seconds),
        updated_at=now,
    )
    entry.refresh_started_at = None
    entry.retry_after = now + timedelta(seconds=retry_after_seconds)


def _cache_catalog_image(entry):
    if entry.image or not entry.image_source_url:
        return
    max_bytes = max(1024, int(settings.OPEN_FOOD_FACTS_IMAGE_MAX_BYTES))
    try:
        raw_image, _ = _open_url(entry.image_source_url, max_bytes, 'image/jpeg,image/png,image/webp')
        with Image.open(BytesIO(raw_image)) as image:
            image_format = (image.format or '').upper()
            width, height = image.size
            if image_format not in {'JPEG', 'PNG', 'WEBP'} or width < 1 or height < 1 or width > 4096 or height > 4096:
                return
            image.verify()
        extension = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp'}[image_format]
        digest = hashlib.sha256(entry.image_source_url.encode('utf-8')).hexdigest()[:12]
        filename = f'{entry.lookup_barcode}-{digest}.{extension}'
        entry.image.save(filename, ContentFile(raw_image), save=True)
    except (CatalogProductNotFound, CatalogUnavailable, UnidentifiedImageError, OSError, ValueError):
        return


def _store_found(entry, product):
    now = timezone.now()
    normalized = _normalize_product(product, entry.lookup_barcode)
    for field, value in normalized.items():
        setattr(entry, field, value)
    entry.status = ProductCatalogEntry.STATUS_FOUND
    entry.fetched_at = now
    cache_days = 30 if entry.product_name else 1
    entry.valid_until = now + timedelta(days=cache_days)
    entry.refresh_started_at = None
    entry.retry_after = None
    entry.save()
    _cache_catalog_image(entry)
    return entry


def _store_not_found(entry):
    now = timezone.now()
    entry.status = ProductCatalogEntry.STATUS_NOT_FOUND
    entry.fetched_at = now
    entry.valid_until = now + timedelta(hours=6)
    entry.refresh_started_at = None
    entry.retry_after = None
    entry.save(update_fields=[
        'status',
        'fetched_at',
        'valid_until',
        'refresh_started_at',
        'retry_after',
        'updated_at',
    ])
    return entry


def lookup_product_catalog(barcode):
    """Return a local catalog result, refreshing it from Open Food Facts when needed."""
    barcode = str(barcode or '')
    now = timezone.now()
    entry = ProductCatalogEntry.objects.filter(
        source=OPEN_FOOD_FACTS_SOURCE,
        lookup_barcode=barcode,
    ).first()

    if entry and entry.valid_until and entry.valid_until > now:
        return _result_from_entry(entry, 'hit')
    if not SUPPORTED_BARCODE_PATTERN.fullmatch(barcode):
        return CatalogLookupResult(status='unsupported', cache_state='skipped', entry=entry)
    if not settings.OPEN_FOOD_FACTS_ENABLED:
        if entry and entry.status == ProductCatalogEntry.STATUS_FOUND:
            return _result_from_entry(entry, 'stale')
        return CatalogLookupResult(status='unavailable', cache_state='disabled', entry=entry)
    if entry and entry.retry_after and entry.retry_after > now:
        if entry.status == ProductCatalogEntry.STATUS_FOUND:
            return _result_from_entry(entry, 'stale')
        return CatalogLookupResult(status='unavailable', cache_state='backoff', entry=entry)

    entry, refresh_claimed = _claim_refresh(barcode, now)
    if not refresh_claimed:
        entry.refresh_from_db()
        if entry.valid_until and entry.valid_until > now:
            return _result_from_entry(entry, 'hit')
        if entry.status == ProductCatalogEntry.STATUS_FOUND:
            return _result_from_entry(entry, 'stale')
        return CatalogLookupResult(status='unavailable', cache_state='refreshing', entry=entry)

    if not _take_quota_slot(now):
        _release_refresh(entry, retry_after_seconds=60)
        if entry.status == ProductCatalogEntry.STATUS_FOUND:
            return _result_from_entry(entry, 'stale')
        return CatalogLookupResult(status='unavailable', cache_state='rate_limited', entry=entry)

    try:
        product = _request_product(barcode)
    except CatalogProductNotFound:
        return _result_from_entry(_store_not_found(entry), 'refreshed')
    except CatalogUnavailable as exc:
        _release_refresh(entry, retry_after_seconds=exc.retry_after_seconds)
        if entry.status == ProductCatalogEntry.STATUS_FOUND:
            return _result_from_entry(entry, 'stale')
        return CatalogLookupResult(status='unavailable', cache_state='error', entry=entry)

    return _result_from_entry(_store_found(entry, product), 'refreshed')
