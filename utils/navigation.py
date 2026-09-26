"""Powrót po akcji dokładnie tam, skąd przyszła.

Formularze akcji (zużycie w spiżarni, odhaczenie na liście, usunięcie
wydatku) wysyłają ukryte pole ``next`` z adresem bieżącej strony razem
z filtrami (``?widok=grupy&q=jogurt``, ``?month=2026-09``). Po zapisie
serwer wraca pod ten adres i dokleja kotwicę elementu, więc przeglądarka
przewija do niego zamiast na górę strony.

Adres z formularza jest danymi od użytkownika: przyjmujemy tylko ścieżkę
w tej samej aplikacji, nigdy adres innej strony (open redirect).
"""
from django.shortcuts import redirect, resolve_url
from django.utils.http import url_has_allowed_host_and_scheme


def safe_next(request, fallback, *, prefix=None):
    """Adres z pola ``next`` albo ``fallback``, gdy pole jest puste lub obce.

    ``prefix`` zawęża dozwolone adresy do jednej części aplikacji (np. akcja
    w spiżarni wraca tylko do spiżarni).
    """
    candidate = (request.POST.get('next') or request.GET.get('next') or '').strip()
    if (
        candidate.startswith('/')
        and not candidate.startswith('//')
        and '\\' not in candidate
        and url_has_allowed_host_and_scheme(
            candidate,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
        and (prefix is None or candidate.startswith(prefix))
    ):
        return candidate.split('#', 1)[0]
    return resolve_url(fallback)


def redirect_back(request, fallback, *, anchor=None, prefix=None, **fallback_kwargs):
    """Przekierowanie pod ``next`` (albo ``fallback``) z kotwicą ``#anchor``."""
    fallback_url = resolve_url(fallback, **fallback_kwargs) if fallback_kwargs else fallback
    url = safe_next(request, fallback_url, prefix=prefix)
    if anchor:
        url = f'{url}#{anchor}'
    return redirect(url)
