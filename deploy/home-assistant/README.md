# Pralka i zmywarka w Home Assistant

Gniazdka Meross MSS315 dostarczają pomiar mocy. Home Assistant co minutę odczytuje
czujniki i wykrywa cykle. Po ich zakończeniu domowa strona wysyła Web Push na
urządzenia domowników z aktywnymi kontami i włączonymi powiadomieniami aplikacji zakupów.

## Gniazdka

1. Jeśli na Pi działa już inny system z Dockerem, zainstaluj obok strony
   [Home Assistant Container](https://www.home-assistant.io/installation/raspberrypi/#install-home-assistant-container).
   W przypadku tego wariantu odczyt mocy z MSS315 można skonfigurować przez
   [Meross LAN](https://github.com/krahabb/meross_lan). Dodaj integrację w HA
   i połącz ją z gniazdkami. Obecne parowanie w Apple Dom może zostać.
2. Jeśli masz już Home Assistant OS z obsługą Matter, możesz zamiast tego
   [udostępnić akcesoria w Matter](https://www.home-assistant.io/integrations/matter/#adding-a-matter-device-to-home-assistant).
   W ustawieniach danego gniazdka w Apple Dom wybierz „Włącz tryb parowania”.
   Dodaj gniazdko w HA jako urządzenie Matter już używane w innej aplikacji.
3. W narzędziach deweloperskich HA sprawdź, czy masz **dwie encje mocy** i czy
   ich wartości zmieniają się w trakcie pracy. Szukaj pomiaru w W lub kW.
   Jeżeli po Matter widać tylko przełącznik, użyj Meross LAN, który obsługuje MSS315.

## Połączenie ze stroną

1. Wygeneruj dedykowany klucz poleceniem `openssl rand -hex 32`. Zapisz go
   w produkcyjnym `.env` strony jako `HOME_ASSISTANT_APPLIANCE_TOKEN` i przy
   wdrożeniu odtwórz kontener `web`, aby wczytał zmienną.
2. Skopiuj [appliance_cycle.yaml](appliance_cycle.yaml) do katalogu konfiguracji HA
   jako `blueprints/automation/website-finance/appliance_cycle.yaml`.
   Skopiuj [website_finance.yaml](website_finance.yaml) do `packages/website_finance.yaml`.
3. Jeśli nie masz jeszcze [pakietów](https://www.home-assistant.io/docs/configuration/packages/),
   dodaj do istniejącego klucza `homeassistant` w `configuration.yaml`:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

4. Dopisz do `secrets.yaml` Home Assistant poniższe wartości, zastępując adres
   strony, klucz i nazwy encji. Zostaw `{{ appliance }}` w adresie:

   ```yaml
   website_finance_appliance_url: "https://ADRES-STRONY/cooking/api/appliances/{{ appliance }}/finished/"
   website_finance_appliance_authorization: "Bearer TU_WKLEJ_KLUCZ"
   website_finance_washer_power: sensor.pralka_power
   website_finance_dishwasher_power: sensor.zmywarka_power
   ```

5. Użyj adresu HTTPS z certyfikatem zaufanym w **Home Assistant**. Przy lokalnym
   certyfikacie Caddy pobierz plik CA z `https://ADRES-STRONY/certyfikat`
   zgodnie z [instrukcją strony](../../README.md#https-and-the-camera-in-the-pantry-scanner). Dla HA
   Container dołącz certyfikat Caddy do kopii publicznego pakietu CA HA
   (jego ścieżkę w kontenerze podaje `python -m certifi`). Zapisz połączony
   plik PEM w katalogu konfiguracji HA. W kontenerze HA ustaw
   `REQUESTS_CA_BUNDLE=/config/website-finance-ca.pem`, wskazując ten plik.
6. Sprawdź konfigurację HA i uruchom go ponownie. W narzędziach deweloperskich
   możesz wywołać `rest_command.website_finance_appliance_finished` z danymi
   `appliance: washer` i `cycle_id: 1`, żeby wysłać próbne powiadomienie do
   domowników. Odpowiedź 200 oznacza wysyłkę na co najmniej jedno urządzenie
   lub wcześniej obsłużony identyfikator. Status 503 oznacza, że żaden telefon
   nie przyjął powiadomienia albo wysyłka Web Push jest wyłączona.

## Strojenie cyklu

Domyślne progi to 20 W startu i 3 W spoczynku. Rozpoczęcie wymaga dwóch
kolejnych odczytów co najmniej na poziomie progu startu. Powiadomienie wymaga 7 minut niskiego
poboru dla pralki lub 15 minut dla zmywarki. Po pierwszym pełnym cyklu można
zmienić `start_watts`, `idle_watts` i `idle_minutes` osobno dla obu automatów
w `website_finance.yaml`. Jeżeli moc spada nisko podczas suszenia, ustaw dłuższy
czas niż ta przerwa. Wykrycie końca jest możliwe wtedy, gdy pobór po zakończeniu
różni się od poboru w trakcie dłuższych przerw w cyklu.

Brak pomiaru i przerwa w odczytach przerywają odliczanie spoczynku. Po nieudanej
wysyłce HA próbuje ponownie w kolejnych minutach, gdy urządzenie pozostaje
w spoczynku. Strona rozpoznaje ponowione zgłoszenie tego samego cyklu osobno dla
pralki i zmywarki. Automatyzacja wyłącznie odczytuje czujniki mocy i zgłasza
zakończenie cyklu.
