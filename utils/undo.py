"""Usuwanie z możliwością cofnięcia: „Usunięto · Cofnij”.

Częste usunięcia (pozycja listy zakupów, wydatek, przychód, tankowanie)
nie pytają już „Na pewno?”. Obiekt znika od razu, a na dole ekranu pojawia
się dymek z przyciskiem „Cofnij”. Pytanie przed każdym usunięciem uczy
klikać „Tak” bez czytania; cofnięcie naprawia pomyłkę wtedy, gdy się zdarzy.

Jak to działa:

1. ``delete_with_undo`` zbiera wszystko, co usunie baza (obiekt i rekordy
   usuwane kaskadowo - tym samym mechanizmem co panel administracyjny),
   zapisuje to w sesji jako JSON i dopiero wtedy usuwa.
2. Relacje ``SET_NULL`` (np. pozycje list wskazujące usuniętą grupę) też
   są zapamiętane, bo usunięcie zeruje je w innych rekordach.
3. ``UndoView`` odtwarza rekordy z tymi samymi kluczami i wartościami
   (zapis „raw”, bez nadpisywania dat utworzenia), przywraca wyzerowane
   powiązania i woła funkcję ``after_restore`` (np. przeliczenie sum
   miesiąca).

Kosz żyje w sesji użytkownika: cofnąć może tylko ta osoba, która usunęła,
przez 10 minut. Jeśli w międzyczasie ktoś utworzył kolidujący rekord
(np. ten sam wpis z importu banku), cofnięcie kończy się komunikatem,
a baza zostaje bez zmian.
"""
import datetime
import logging
import secrets
import time

from django.apps import apps
from django.contrib import messages
from django.contrib.admin.utils import NestedObjects
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core import serializers
from django.core.serializers.base import DeserializationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError, router, transaction
from django.utils.module_loading import import_string
from django.views import View

from utils.navigation import redirect_back

logger = logging.getLogger(__name__)

SESSION_KEY = 'undo_bin'
UNDO_SECONDS = 10 * 60
MAX_ENTRIES = 10
TOKEN_TAG_PREFIX = 'undo-token-'


class _ExactEncoder(DjangoJSONEncoder):
    """Czas z mikrosekundami - DjangoJSONEncoder obcina je do milisekund,
    a przywrócony rekord ma być identyczny z usuniętym."""

    def default(self, o):
        if isinstance(o, (datetime.datetime, datetime.time)):
            return o.isoformat()
        return super().default(o)


def _collect(obj, using):
    collector = NestedObjects(using=using)
    collector.collect([obj])
    if collector.protected:
        return None
    collector.sort()
    instances = [instance for group in collector.data.values() for instance in group]
    # Rekordy, którym usunięcie wyzeruje klucz obcy (SET_NULL / SET_DEFAULT).
    # Wartości trzeba odczytać teraz - po usunięciu są już nadpisane.
    updates = []
    for (field, _value), groups in collector.field_updates.items():
        for group in groups:
            for instance in group:
                current = getattr(instance, field.attname)
                updates.append([
                    instance._meta.label_lower,
                    str(instance.pk),
                    field.attname,
                    None if current is None else str(current),
                ])
    return instances, updates


def _bin(request):
    now = time.time()
    entries = [
        entry for entry in request.session.get(SESSION_KEY, [])
        if now - entry.get('at', 0) < UNDO_SECONDS
    ]
    return entries


def delete_with_undo(request, obj, message, *, after_restore=None, restored_message=None, anchor=None):
    """Usuwa ``obj`` (z kaskadą) i dodaje komunikat z przyciskiem „Cofnij”.

    ``after_restore`` to ścieżka do funkcji ``f(objects)`` wołanej po
    przywróceniu, np. przeliczenie sum miesiąca. ``anchor`` to kotwica,
    pod którą strona przewinie się po cofnięciu.
    """
    using = router.db_for_write(obj.__class__, instance=obj)
    collected = _collect(obj, using)
    if collected is None:
        # Chroniony rekord (PROTECT) - usunięcie i tak się nie uda, niech
        # zgłosi to zwykła ścieżka.
        obj.delete()
        messages.success(request, message)
        return
    instances, updates = collected
    payload = serializers.serialize('json', instances, cls=_ExactEncoder)
    obj.delete()

    token = secrets.token_urlsafe(9)
    entries = _bin(request)
    entries.append({
        'token': token,
        'at': time.time(),
        'using': using,
        'payload': payload,
        'updates': updates,
        'hook': after_restore,
        'restored': restored_message or 'Przywrócono.',
        'anchor': anchor,
    })
    request.session[SESSION_KEY] = entries[-MAX_ENTRIES:]
    messages.success(request, message, extra_tags=f'undo {TOKEN_TAG_PREFIX}{token}')


def restore(entry):
    """Odtwarza rekordy z wpisu kosza. Zwraca listę przywróconych obiektów."""
    using = entry['using']
    restored = []
    with transaction.atomic(using=using):
        for deserialized in serializers.deserialize('json', entry['payload'], using=using):
            # Zapis "raw": te same klucze i daty, bez sygnałów zmieniających dane.
            deserialized.save(using=using, save_m2m=False)
            restored.append(deserialized.object)
        for label, pk, attname, value in entry['updates']:
            model = apps.get_model(label)
            model._default_manager.using(using).filter(pk=pk).update(**{attname: value})
        if entry.get('hook'):
            import_string(entry['hook'])(restored)
    return restored


class UndoView(LoginRequiredMixin, View):
    def post(self, request, token):
        entries = _bin(request)
        entry = next((item for item in entries if item['token'] == token), None)
        if entry is None:
            messages.info(request, 'Tego nie da się już cofnąć - minęło za dużo czasu.')
            return redirect_back(request, 'index')

        request.session[SESSION_KEY] = [item for item in entries if item['token'] != token]
        try:
            restore(entry)
        except (DatabaseError, DeserializationError, LookupError) as exc:
            logger.warning('Cofnięcie usunięcia nie powiodło się: %s', exc)
            messages.error(
                request,
                'Nie udało się cofnąć: w międzyczasie zmieniły się powiązane dane.',
            )
            return redirect_back(request, 'index')
        messages.success(request, entry.get('restored') or 'Przywrócono.')
        return redirect_back(request, 'index', anchor=entry.get('anchor'))


def undo_token(message):
    """Token cofnięcia zapisany w tagach komunikatu (albo pusty tekst)."""
    for tag in str(getattr(message, 'extra_tags', '') or '').split():
        if tag.startswith(TOKEN_TAG_PREFIX):
            return tag[len(TOKEN_TAG_PREFIX):]
    return ''
