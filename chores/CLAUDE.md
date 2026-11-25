# chores – zadania domowe

Zakładka „Zadania” pod `/zadania/` (namespace `chores`). Wspólna lista dla
zalogowanych domowników, jak w przypadku list zakupów. Domownicy mogą dodawać,
edytować, odhaczać, ponownie otwierać i usuwać zadania. Wykonanie zadania jest
wspólne. Widok „Moje” pokazuje otwarte zadania przypisane do bieżącego użytkownika.

Modele: `Chore` (nazwa, notatka, opcjonalny termin, czas wykonania) i
`ChoreAssignment` (osoby odpowiedzialne). Formularz wymaga co najmniej jednego
aktywnego użytkownika. Jawny model pośredni pozwala odtworzyć przypisania razem
z zadaniem przy użyciu `utils.undo`.

`reminders.py` korzysta z subskrypcji Web Push aplikacji zakupów. Istniejący cron
`send_reminders` (rodzaj `zadania`) raz dziennie wysyła zbiorcze przypomnienie
danemu użytkownikowi z otwartymi zadaniami na dziś i po terminie. Start
(`config/home.py`) pokazuje ich liczbę również bez włączonych powiadomień.

Testy: `python manage.py test chores --settings=config.test_settings`.
