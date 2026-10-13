# accounts – logowanie, profil, konta wspólne, tagi UI

Brak własnych modeli: używa User i `finance.FinanceAccount`.
Widoki (`views.py`):
- signup – przy `ALLOW_PUBLIC_SIGNUP` albo gdy nie ma jeszcze użytkowników.
- profile – tworzenie i zarządzanie kontami wspólnymi.
- edit_shared_account, delete_shared_account.
- login/logout – widoki Django z szablonami `accounts/`.

Zależności: `finance.account_utils.ensure_personal_finance_account`, `finance.models.FinanceAccount`.
`templatetags/ui.py` jest używany w całym serwisie (`{% load ui %}`):
- `page_head` – nagłówek strony;
- `pl`, `pl_word` – odmiana;
- `zl` – kwoty;
- `undo_token`.
`context_processors.account_access` jest w każdym szablonie. Testy: `accounts/tests.py`.
