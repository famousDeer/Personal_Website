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

from ..constants import PANTRY_CATEGORY_OTHER
from ..models import PantryProduct, ProductCatalogEntry, ProductCatalogQuota
from . import categories as category_rules


OPEN_FOOD_FACTS_ROOT = 'https://world.openfoodfacts.org'
OPEN_FOOD_FACTS_SOURCE = ProductCatalogEntry.SOURCE_OPEN_FOOD_FACTS
HOUSEHOLD_SOURCE = ProductCatalogEntry.SOURCE_HOUSEHOLD
OPEN_FOOD_FACTS_FIELDS = (
    'code',
    'lang',
    'product_type',
    'product_name',
    'product_name_pl',
    'product_name_en',
    'generic_name',
    'generic_name_pl',
    'generic_name_en',
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

# Kategorie i reguły automatycznego wyboru kategorii są w bazie i edytują je
# domownicy (strona "Kategorie" w spiżarni). Stan początkowy i opis kolejności
# reguł: cooking/category_defaults.py, odczyt: cooking/services/categories.py.

# Domyślna kategoria, gdy ani tagi, ani nazwa nic nie mówią. Produkt z bazy
# kosmetyków jest kosmetykiem, a karma karmą - nawet bez szczegółów. Po kodzie
# kategorii, bo domownicy mogą zmienić jej nazwę.
PRODUCT_TYPE_DEFAULT_CATEGORY_CODES = {
    'beauty': 'kosmetyki',
    'petfood': 'zwierzeta',
}


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
    # Dla wpisu zapamiętanego w domu: wpis Open Food Facts z lokalnego cache,
    # z którego bierzemy zdjęcie, markę i opis. Nazwa i kategoria zawsze
    # pochodzą z `entry`.
    source_entry: ProductCatalogEntry | None = None

    @property
    def remembered(self):
        return bool(self.entry and self.entry.source == HOUSEHOLD_SOURCE)


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


def _category_from_tags(category_tags, food_allowed=True):
    match = category_rules.match_tags(_category_tags(category_tags), food_allowed=food_allowed)
    return match.category if match else ''


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

    rule_count = len(category_rules.taxonomy().tag_rules)

    def priority(raw_value):
        return category_rules.tag_rule_priority(_category_tags([raw_value]))

    recognized_values = [
        raw_value for raw_value in cleaned_values
        if priority(raw_value) < rule_count
    ]
    other_values = [
        raw_value for raw_value in cleaned_values
        if priority(raw_value) >= rule_count
    ]
    recognized_values.sort(key=priority)
    external_category = _clean_text(raw_external_category, max_length=255)
    if external_category and external_category not in seen:
        recognized_values.append(external_category)
    return recognized_values + other_values


_text_has_keyword = category_rules.text_has_keyword
_searchable_text = category_rules.searchable_text


def _category_from_text(searchable, food_allowed=True):
    match = category_rules.match_text(searchable, food_allowed=food_allowed)
    return match.category if match else ''


def suggest_category_from_name(name):
    """Kategoria na podstawie samej nazwy - dla produktów dodanych ręcznie."""
    return _category_from_text(_searchable_text(name)) or ''


def _suggest_category(
    product_name,
    description,
    external_category,
    category_tags=None,
    product_type='food',
):
    normalized_product_type = _clean_text(product_type, max_length=20).casefold() or 'food'
    pet_category = category_rules.category_for_code('zwierzeta')
    if normalized_product_type == 'petfood' and pet_category:
        return pet_category
    # Kategoria spożywcza dla produktu z bazy kosmetyków albo produktów
    # ogólnych byłaby błędem niezależnie od tego, co mówi nazwa
    # ("masło do ciała" to nie nabiał).
    food_allowed = normalized_product_type not in {'beauty', 'product'}

    tag_category = _category_from_tags(
        category_tags or _category_tags(external_category),
        food_allowed=food_allowed,
    )
    if tag_category:
        return tag_category

    searchable = _searchable_text(product_name, description, external_category)
    text_category = _category_from_text(searchable, food_allowed=food_allowed)
    if text_category:
        return text_category
    default_code = PRODUCT_TYPE_DEFAULT_CATEGORY_CODES.get(normalized_product_type)
    default_category = category_rules.category_for_code(default_code) if default_code else ''
    return default_category or PANTRY_CATEGORY_OTHER


def _source_timestamp(product):
    raw_timestamp = product.get('last_updated_t') or product.get('last_modified_t')
    try:
        timestamp = float(raw_timestamp)
        if timestamp <= 0:
            return None
        return datetime.fromtimestamp(timestamp, tz=datetime_timezone.utc)
    except (OverflowError, TypeError, ValueError):
        return None


def _pick_product_name(product):
    """Najlepsza dostępna nazwa i język, z którego pochodzi.

    `product_name` bez przyrostka jest w głównym języku produktu (`lang`),
    czyli dla towaru importowanego zwykle po niemiecku albo czesku. Dlatego
    kolejność jest: polska nazwa w dowolnym polu, potem angielska
    (zrozumiała dla każdego), dopiero na końcu nazwa w języku oryginału.
    Nazwa ogólna po polsku ("Mleko UHT 2%") jest lepsza od nazwy handlowej
    w obcym języku, więc wyprzedza angielską.
    """
    main_language = _clean_text(product.get('lang'), max_length=8).casefold()
    candidates = (
        ('product_name_pl', 'pl'),
        ('product_name', 'pl' if main_language == 'pl' else None),
        ('generic_name_pl', 'pl'),
        ('generic_name', 'pl' if main_language == 'pl' else None),
        ('product_name_en', 'en'),
        ('product_name', 'en' if main_language == 'en' else None),
        ('generic_name_en', 'en'),
        ('product_name', main_language),
        ('generic_name', main_language),
    )
    for field, language in candidates:
        if language is None:
            continue
        value = _clean_text(product.get(field), max_length=160)
        if value:
            return value, language
    return '', ''


def _normalize_product(product, lookup_barcode):
    canonical_barcode = _clean_text(product.get('code'), max_length=64) or lookup_barcode
    product_name, name_language = _pick_product_name(product)
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
        'name_language': name_language,
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


def category_suggestion_for_entry(entry):
    """Kategoria dla wpisu katalogu według bieżących reguł - bez zapisu."""
    if not entry or entry.status != ProductCatalogEntry.STATUS_FOUND:
        return ''
    if entry.source == HOUSEHOLD_SOURCE:
        # Wybór domownika jest ostateczny - reguły go nie nadpisują.
        return entry.suggested_category if category_rules.is_valid_category(entry.suggested_category) else ''
    category = _suggest_category(
        entry.product_name,
        entry.description,
        entry.external_category,
        category_tags=_category_tags(entry.external_category),
        product_type=entry.product_type,
    )
    return category if category_rules.is_valid_category(category) else PANTRY_CATEGORY_OTHER


def suggested_category_for_catalog_entry(entry):
    """Jak wyżej, a przy okazji odświeża zapisaną podpowiedź we wpisie.

    Reguły mapowania żyją w kodzie, więc po ich zmianie stare wpisy w cache
    same dostają nową kategorię przy pierwszym odczycie.
    """
    category = category_suggestion_for_entry(entry)
    if not category or entry.source == HOUSEHOLD_SOURCE:
        return category
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


def household_catalog_entry(barcode):
    return ProductCatalogEntry.objects.filter(
        source=HOUSEHOLD_SOURCE,
        lookup_barcode=str(barcode or ''),
        status=ProductCatalogEntry.STATUS_FOUND,
    ).first()


def cached_open_food_facts_entry(barcode):
    """Wpis Open Food Facts z lokalnej bazy, bez sięgania do sieci."""
    return ProductCatalogEntry.objects.filter(
        source=OPEN_FOOD_FACTS_SOURCE,
        lookup_barcode=str(barcode or ''),
        status=ProductCatalogEntry.STATUS_FOUND,
    ).first()


def remember_household_product(barcode, *, name, category, unit, quantity_per_scan):
    """Zapamiętuje dane produktu dla kodu kreskowego, dla całego domu.

    Wywoływane przy dodaniu produktu ze skanera i przy edycji produktu
    z kodem. Następny skan tego kodu - przez dowolnego domownika, także po
    usunięciu produktu ze spiżarni - podpowie dokładnie te dane zamiast
    tego, co zwraca Open Food Facts. Zwraca wpis albo None, gdy kodu nie ma.
    """
    barcode = str(barcode or '').strip()
    name = _clean_text(name, max_length=160)
    if not barcode or not name:
        return None
    category = category if category_rules.is_valid_category(category) else ''
    now = timezone.now()
    defaults = {
        'status': ProductCatalogEntry.STATUS_FOUND,
        'canonical_barcode': barcode,
        'product_name': name,
        'name_language': '',
        'suggested_category': category,
        'suggested_unit': unit or '',
        'suggested_quantity_per_scan': quantity_per_scan,
        'fetched_at': now,
        # Pamięć domu nie wygasa i nie jest odświeżana z sieci.
        'valid_until': None,
        'refresh_started_at': None,
        'retry_after': None,
    }
    entry, _ = ProductCatalogEntry.objects.update_or_create(
        source=HOUSEHOLD_SOURCE,
        lookup_barcode=barcode,
        defaults=defaults,
    )
    return entry


def lookup_product_catalog(barcode):
    """Return a local catalog result, refreshing it from Open Food Facts when needed.

    Pamięć domu ma pierwszeństwo: jeśli ktoś już dodał ten kod, jego nazwa
    i kategoria wygrywają i nie ma żadnego zapytania do sieci.
    """
    barcode = str(barcode or '')
    remembered = household_catalog_entry(barcode)
    if remembered:
        return CatalogLookupResult(
            status='found',
            cache_state='household',
            entry=remembered,
            source_entry=cached_open_food_facts_entry(barcode),
        )

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
