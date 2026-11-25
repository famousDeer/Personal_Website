# chores – zadania domowe

Zakładka „Zadania” pod `/zadania/` (namespace `chores`). Wspólna lista dla
zalogowanych domowników, jak w przypadku list zakupów. Domownicy mogą dodawać,
edytować, odhaczać, ponownie otwierać i usuwać zadania. Wykonanie zadania jest
wspólne. Widok „Moje” pokazuje otwarte zadania przypisane do bieżącego użytkownika.

Modele: `Chore` (nazwa, notatka, czas wykonania) i
`ChoreAssignment` (osoby odpowiedzialne). Formularz wymaga co najmniej jednego
aktywnego użytkownika. Jawny model pośredni pozwala odtworzyć przypisania razem
z zadaniem przy użyciu `utils.undo`.

`reminders.py` wysyła Web Push do przypisanych osób po zatwierdzeniu transakcji
tworzącej zadanie. Korzysta z subskrypcji aplikacji zakupów. Start
(`config/home.py`) pokazuje liczbę otwartych zadań przypisanych do bieżącej osoby.

Testy: `python manage.py test chores --settings=config.test_settings`.
