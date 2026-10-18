"""Zapasowy odczyt paragonu przez Claude API (model Haiku), na żądanie.

Pi czyta paragon Tesseractem. Gdy się nie uda albo sumy się nie zgadzają,
strona paragonu pokazuje przycisk „Odczytaj przez AI” - dopiero wtedy
zdjęcie wychodzi z domu. Model przepisuje paragon w stałym układzie, który
rozumie zwykły parser (receipt_parser.py), więc słownik domu, rabaty
i kontrola sumy działają tak samo jak przy odczycie lokalnym.

Klucz API w .env (ANTHROPIC_API_KEY); subskrypcja Claude go nie obejmuje -
API rozlicza się osobno w Claude Console. Koszt jednego paragonu na Haiku
to ułamek grosza (szacunek z cennika, 18.10.2026).
"""
from __future__ import annotations

import base64
import io
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

API_URL = 'https://api.anthropic.com/v1/messages'
API_VERSION = '2023-06-01'
MAX_LONG_EDGE = 2576          # więcej model i tak zmniejsza
MAX_OUTPUT_TOKENS = 4000

PROMPT = """Przepisz ten polski paragon fiskalny (jedno lub kilka zdjęć tego samego paragonu) dokładnie w poniższym układzie. Nie dodawaj komentarzy ani formatowania Markdown.

<nazwa sklepu, np. Lidl, Biedronka, Aldek>
NIP <10 cyfr, jeśli widać>
Data <RRRR-MM-DD> <GG:MM>
nr wydr.: <numer paragonu, jeśli widać>
PARAGON FISKALNY
<nazwa towaru dokładnie jak na paragonie>   <ilość> x <cena jednostkowa> <wartość> <litera PTU>
<opis rabatu dokładnie jak na paragonie>   -<kwota rabatu>
SUMA PLN <kwota do zapłaty>

Zasady:
- jedna pozycja w jednej linii; ilość z przecinkiem; towar ważony z dopiskiem kg, np. 0,754 kg x 5,99 4,52 C
- kwoty z przecinkiem i dwoma miejscami po przecinku
- rabat, kupon, „Taniej za 2” itp. w osobnej linii bezpośrednio pod pozycją, której dotyczy, kwota ze znakiem minus
- pomiń linie PTU, sumy VAT, płatność, kody i reklamy
- nieczytelny znak zastąp najbardziej prawdopodobnym tak, by wartości pozycji sumowały się do SUMA PLN"""


class ReceiptAiError(Exception):
    pass


def ai_enabled() -> bool:
    return bool(getattr(settings, 'RECEIPT_AI_ENABLED', False) and getattr(settings, 'ANTHROPIC_API_KEY', ''))


def _encode(path) -> dict:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert('RGB')
    image.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE))
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=85)
    return {
        'type': 'image',
        'source': {'type': 'base64', 'media_type': 'image/jpeg',
                   'data': base64.b64encode(buffer.getvalue()).decode('ascii')},
    }


def call_claude(paths) -> tuple[str, dict]:
    """Wysyła zdjęcia do Claude API. Zwraca (tekst paragonu, zużycie tokenów)."""
    if not ai_enabled():
        raise ReceiptAiError('Odczyt przez AI jest wyłączony (RECEIPT_AI_ENABLED / ANTHROPIC_API_KEY).')
    content = [_encode(path) for path in paths] + [{'type': 'text', 'text': PROMPT}]
    body = json.dumps({
        'model': settings.RECEIPT_AI_MODEL,
        'max_tokens': MAX_OUTPUT_TOKENS,
        'messages': [{'role': 'user', 'content': content}],
    }).encode('utf-8')
    request = urllib.request.Request(API_URL, data=body, method='POST', headers={
        'x-api-key': settings.ANTHROPIC_API_KEY,
        'anthropic-version': API_VERSION,
        'content-type': 'application/json',
    })
    try:
        with urllib.request.urlopen(request, timeout=settings.RECEIPT_AI_TIMEOUT) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = json.loads(exc.read().decode('utf-8')).get('error', {}).get('message', '')
        except (ValueError, AttributeError):
            pass
        logger.warning('Claude API: %s %s', exc.code, detail)
        if exc.code in (401, 403):
            raise ReceiptAiError('Klucz Claude API jest nieprawidłowy albo nie ma uprawnień.') from exc
        if exc.code == 400 and 'credit' in detail.lower():
            raise ReceiptAiError('Brak środków na koncie Claude API - doładuj kredyty w Claude Console.') from exc
        raise ReceiptAiError(f'Claude API odrzuciło zapytanie ({exc.code}).') from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ReceiptAiError('Brak połączenia z Claude API - spróbuj za chwilę.') from exc
    text = '\n'.join(block.get('text', '') for block in payload.get('content', []) if block.get('type') == 'text')
    if not text.strip():
        raise ReceiptAiError('Claude API nie zwróciło tekstu paragonu.')
    return text.strip(), payload.get('usage', {})
