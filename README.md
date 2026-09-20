# Website-Finance
Self-hosted household dashboard for a home LAN, built with Django, PostgreSQL and
Bootstrap and running in Docker on a Raspberry Pi 4. Five modules: household budget,
brokerage portfolio, kitchen (recipes, pantry, shopping lists), car upkeep and habits.

Demo image:
<p align="center">
  <img src="img/presentation.gif" alt="preview" />
</p>

## Features

**Finance** (`finance`)
- Expenses and incomes by date, category/source and store, with monthly totals
- Personal and shared household accounts; a transfer to a shared account
  automatically creates the matching income on the other side
- Bank statement import from **Millennium** and **ING** CSV: duplicate detection by
  a stable transaction identity, category suggestions from account history and
  keyword rules, editable preview before anything is written
- Brokerage portfolio: XTB XLSX import (transactions, cash operations, position
  snapshots), FIFO cost basis, 19% capital gains tax with IKE accounts exempt,
  daily price history and portfolio value charts
- Market data from Yahoo Finance (bulk quotes), with Stooq and Alpha Vantage
  fallbacks and OpenFigi ISIN lookup; NBP for FX rates
- Travel log split into holiday and business trips, geocoded and shown on a map
- Reports and a dashboard with end-of-month spending projection

**Kitchen** (`cooking`)
- Recipes visible to everyone, editable only by their author, with structured steps
  and per-step ingredients
- Pantry with barcode scanning, product metadata cached from the Open Food Facts
  family (food, beauty, household products, pet food), and stock tracked both as
  exact quantity and as a number of packages
- One pantry for the whole household: every logged-in user sees and edits all
  products, stock, forecasts and shopping lists (see [Shared pantry](#shared-pantry))
- Full product edit (name, barcode, category, unit, package size, stock, threshold,
  photo, notes) and permanent delete; a unit change converts the movement history,
  a stock change is recorded as a correction
- Household memory for barcodes: the name and category someone confirms when adding
  or editing a product become the suggestion for every later scan of that code, for
  every user, even after the product is deleted (see [Pantry catalog](#pantry-catalog))
- Fixed Polish category list for food and household goods (drugstore, cleaning,
  paper, pets, medicines), mapped from the Open Facts taxonomies
- Consumption forecasting per product: regular and intermittent demand models,
  outlier capping, weekday factors, confidence scoring and a suggested buy date
  aligned to your usual shopping day
- Shopping lists generated from the forecast or entered by hand; checking off a
  product that is in the pantry restocks it immediately, completing a list adds the rest
- Offline shopping mode for phones (installable web app): check off, add, change
  quantities and delete items without a connection to the home server; changes sync
  when the phone is back on the home network (see [Offline shopping mode](#offline-shopping-mode))

**Car** (`cars`)
- Fuel log with computed consumption, service history with itemised parts
- Tyre sets tracked as mount/removal periods with driven distance
- Service history export to PDF

**Accounts** (`accounts`) — login, registration that can be closed after setup,
shared household account management.

**Habits** (`habits`) — models and CRUD only; not in active use yet.

## Tech stack
- Python 3.11, Django 5.2
- PostgreSQL 15
- Bootstrap 5, Chart.js
- Docker + Docker Compose
- Gunicorn (prod-like run in Docker), WhiteNoise for static files
- pandas / numpy (statement import), reportlab (PDF), pillow (images),
  django-countries (travel), djangorestframework (two authenticated endpoints)
- No Celery or Redis: scheduled work runs through `manage.py` commands and cron.
  All external API clients are written on the standard library (`urllib`).

## Quick start (Docker, recommended)
Prerequisites:
- Docker and Docker Compose installed

1) Create .env in the project root:
```dotenv
SECRET_KEY=change-me
# Use 0 on a home server. With DEBUG=1 every error page shows a full traceback
# including your settings, to anyone who can reach the host.
DEBUG=0
ALLOW_PUBLIC_SIGNUP=1
ALLOWED_HOSTS=localhost,127.0.0.1,0.0.0.0,192.168.1.115,raspberrypi.local
CSRF_TRUSTED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000,http://192.168.1.115:8000,http://raspberrypi.local:8000

# Database connection. Django reads these, and docker-compose.yml uses the same
# values to initialise the Postgres container, so the two cannot drift apart.
DATABASE_NAME=finance_db
DATABASE_USER=finance
DATABASE_PASSWORD=choose-a-real-password
DATABASE_HOST=db
DATABASE_PORT=5432

# Brokerage market data
ALPHA_VANTAGE_API_KEY=
OPENFIGI_API_KEY=
STOOQ_API_KEY=

# HTTPS in front of the app (caddy service); comma-separated, each also in ALLOWED_HOSTS
HTTPS_SITES=raspberrypi.local, 192.168.1.115
# The Pi's IP from the list above (see "HTTPS and the camera" for why it is separate)
HTTPS_IP=192.168.1.115

# Pantry catalog (optional; defaults work without these entries)
OPEN_FOOD_FACTS_ENABLED=1
OPEN_FOOD_FACTS_USER_AGENT=WebsiteFinance/1.0 (https://github.com/famousDeer/Personal_Website)
```

2) Start the stack:
```bash
docker compose up -d --build
```
- App: http://localhost:8000
- DB (Postgres): bound to `127.0.0.1:5433` on the host only, so the database is not
  reachable from the rest of the home network. Connect with
  `docker compose exec db psql -U finance finance_db`, or over an SSH tunnel.

> **Changing DATABASE_PASSWORD later:** Postgres only applies `POSTGRES_PASSWORD`
> when it initialises an empty data directory. On an existing volume you must change
> it inside the database as well, otherwise Django will fail to authenticate:
> ```bash
> docker compose exec db psql -U finance -d finance_db \
>   -c "ALTER USER finance WITH PASSWORD 'the-new-password';"
> ```
> Then update `.env` to match and run `docker compose up -d`.

3) Create an admin user:
```bash
docker compose exec web python manage.py createsuperuser
```

The web service runs:
- Migrations: python manage.py migrate
- Collect static: python manage.py collectstatic --noinput
- Server: gunicorn --bind 0.0.0.0:8000 config.wsgi:application

## Local development
The `web-dev` service runs the Django dev server instead of Gunicorn:
```bash
docker compose up web-dev --build
```
It deliberately does **not** run `makemigrations`. Generating migrations inside a
container without the source mounted writes them into a throwaway image layer, where
they are lost on the next rebuild while the database has already moved on. Generate
them explicitly instead, so they land in version control:
```bash
docker compose run --rm web-dev python manage.py makemigrations
```

### A code change is not visible until the image is rebuilt

`web-dev` does **not** bind-mount the source. The code reaches the container only
through `COPY . .` in the Dockerfile, so the running container is a frozen snapshot
taken at build time. Editing a template, a stylesheet or a view changes nothing in the
browser until you rebuild:

```bash
docker compose up web-dev --build
```

This catches people out, because a normal Django dev setup reloads templates on save.
Here it cannot: there is nothing connecting the file you edited to the container.

Two build stamps make the state visible instead of guessable. Paste this into the
browser console on any page:

```js
[document.querySelector('meta[name=app-build]').content,
 getComputedStyle(document.documentElement).getPropertyValue('--ui-build')]
```

| Value | Lives in | Stale means |
|---|---|---|
| `app-build` | `templates/base.html` | the **container** is running old code → `docker compose up web-dev --build` |
| `--ui-build` | `static/css/ui.css` | the **browser** is serving a cached stylesheet → hard reload (`Cmd+Shift+R`) |

Both should read `2026-09-19-d`. If they disagree with each other, the container is
fresh but the browser is not.

### Live reload

Copy the ready-made override and the rebuild step goes away:

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
```

The file documents the one prerequisite on macOS: the project directory has to be
fully downloaded from iCloud (**Keep Downloaded** in Finder), otherwise the mount hits
the placeholder problem described below.

> **Do not bind-mount the source if the repository lives in iCloud Drive**, OneDrive, Dropbox or any
> other macOS File Provider folder. Files that are not materialised locally are only
> placeholders, and reading one through a Docker bind mount fails with
> `OSError: [Errno 35] Resource deadlock avoided` — the container dies on startup as
> soon as Django imports a file that happens to be evicted.
>
> Keeping a git repository in a sync folder is worth avoiding for its own sake: the
> provider can evict objects under `.git/`, and two machines syncing the same working
> tree can corrupt it. Move the checkout to a plain local path such as `~/dev/` and
> the bind mount, `git` and Docker all behave normally.

## Tests
```bash
# Postgres, same as CI
docker compose run --rm web-test

# or locally against SQLite, without Docker
python manage.py test --settings=config.test_settings
```
`config/test_settings.py` switches to SQLite and disables outbound calls to Nominatim
and Open Food Facts, so the suite never touches the network. CI additionally runs
`python manage.py makemigrations --check --dry-run` to catch uncommitted migrations.

## HTTPS and the camera in the pantry scanner

Browsers expose the live camera (`getUserMedia`) only to secure contexts: HTTPS or
`localhost`. On `http://192.168.x.x` the API does not exist at all, so the scanner can
only fall back to taking a photo of the barcode. Measured in Chromium on the same page:

| Origin | `isSecureContext` | `navigator.mediaDevices.getUserMedia` |
|---|---|---|
| `http://localhost` | true | function |
| `http://<LAN IP>` | false | undefined |

The `caddy` service in `docker-compose.yml` puts HTTPS in front of the app with a
certificate from Caddy's own local certificate authority (`tls internal`). No domain,
no account and no internet connection are needed.

On the Raspberry Pi:

```bash
docker compose up -d --build web caddy
```

- `https://raspberrypi.local` and `https://192.168.1.115` serve the app. Change the list
  with `HTTPS_SITES` in `.env` (comma-separated); every name must also be in
  `ALLOWED_HOSTS`.
- `HTTPS_IP` must be the Pi's IP from that list. A browser opening an IP address sends
  no server name (SNI), so Caddy picks the certificate by the address the connection
  arrived on — inside Docker that is the container's own 172.x address, which has no
  certificate, and the browser shows `ERR_SSL_PROTOCOL_ERROR`. `HTTPS_IP` becomes
  Caddy's `default_sni` and fixes that. Names such as `raspberrypi.local` do send SNI
  and are unaffected.
- `http://<address>/certyfikat` is a page for phones with the certificate download and
  step-by-step instructions for iPhone and Android. Each phone trusts the certificate
  once; after that the scanner opens the camera immediately.
- Everything else on port 80 redirects to HTTPS. Port 8000 keeps working over plain
  HTTP; the scanner there shows a link to the HTTPS version.
- `web` runs with `BEHIND_HTTPS_PROXY=1`, which sets `SECURE_PROXY_SSL_HEADER`. Without
  it Django sees the proxied request as plain HTTP and every form fails the CSRF
  origin check (`https://host` vs `http://host`) with a 403.

> **The `caddy_data` volume holds the certificate authority.** Deleting it creates a
> new one, and every phone has to install the certificate again. Phones that trust
> this authority would accept any certificate it signs, so treat the Pi (and that
> volume) as you would any device that holds a key — do not copy it elsewhere.

If Caddy fails with `address already in use`, something else on the Pi (for example
Pi-hole) already uses port 80 or 443.

## Pantry catalog

A barcode is looked up in this order:

1. **Household memory** (`ProductCatalogEntry` with `source="household"`). Written when
   a product is added from the scanner, added by hand with a barcode, or edited. It
   never expires and never calls the network.
2. **Open Food Facts** API v3 with `product_type=all`, cached locally for 30 days.

No free barcode database covers Polish drugstore and household goods with Polish
names (tested in September 2026: Open Food Facts found 3 of 8 typical products from
Polish shops, the only complete source costs $99/month and requires deleting all
cached data on cancellation). So the app treats its own confirmed data as the source
of truth: a household buys the same things repeatedly, and each code needs to be
corrected once.

**Names.** Preference is Polish name, then Polish generic name, then English, then
the product's original language. When the suggestion is not Polish, the scanner does
not offer one-tap adding; the form opens with the foreign name selected, so typing
replaces it.

**Categories** live in `cooking/constants.py` as `PANTRY_CATEGORY_GROUPS`. Existing
names are stored as plain text on products, so renaming one needs a data migration;
adding one does not. Mapping rules are in `cooking/services/product_catalog.py`: tag
rules keyed on Open Facts taxonomy nodes (the API returns every ancestor of a tag, so
mid-level nodes are enough), then Polish/English/German/Czech keyword rules for
products without usable tags.

After upgrading an existing installation, run once:

```bash
docker compose exec web python manage.py learn_pantry_catalog --dry-run   # preview
docker compose exec web python manage.py learn_pantry_catalog
```

It re-derives cached Open Food Facts suggestions with the current rules, moves
products out of `Inne` (or no category) when a better category is found, and
remembers every existing barcoded product for the whole household. Categories that
were chosen deliberately, and existing household entries, are never overwritten.

## Shared pantry

Since September 2026 the pantry and shopping lists belong to the household, not to a
user. `PantryProduct.created_by` and `ShoppingList.created_by` only record who added
the entry (the database column is still `user_id`). One product per name (case
insensitive) and per barcode is enforced by database constraints.

**Upgrading an installation that still has per-user pantries.** Migration `0011`
merges duplicates between users, and the web container runs migrations on start, so
look at the plan and take a backup first:

```bash
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backup-before-shared-pantry.sql
git pull
docker compose build web
docker compose run --rm web python manage.py preview_shared_pantry   # read-only
docker compose up -d web caddy                                       # migrates on start
```

`preview_shared_pantry` works before the migration and changes nothing. Merge rules
(`cooking/services/pantry_sharing.py`): the same barcode, or the same name with at most
one barcode, is one product. The oldest product is kept; stock and package counts are
summed after unit conversion (g/kg, ml/l), movement history and shopping-list items
are moved over. Products that cannot be merged (two different barcodes under one
name, or units such as pieces and grams) keep both entries and the newer one gets the
author's username in its name, e.g. `Cukier (ania)`.

**Editing** (`EditPantryProductView`, side effects in
`cooking/services/pantry_editing.py`):
- g↔kg and ml↔l unit changes convert stock, threshold, package size and every movement
  in the history, so the forecast stays correct. Other unit changes keep the numbers,
  which is what fixing a wrongly chosen unit needs.
- A stock change is saved as an `adjust` movement ("Korekta") with the signed
  difference. The forecast uses it to reconstruct past stock but never counts it as
  consumption or purchase.
- Items on shopping lists that are not completed follow the new name, category and
  (when it cannot be converted) unit. Completed lists stay as history.
- A new barcode is remembered for the household like any other edit.

**Deleting** (`DeletePantryProductView`) needs a ticked confirmation and removes the
product, its photo and its movement history. Shopping-list items stay and lose only
the link; completing such a list creates the product again. The household memory of
the barcode stays unless "forget the barcode" is ticked.

## Offline shopping mode

`/cooking/shopping/app/` is a small installable web app (PWA) for the phone. The
server is only reachable on the home network, so in a shop the phone is effectively
offline. The app keeps the active lists and a queue of changes in IndexedDB and syncs
whenever the server answers.

**On the phone** (needs the HTTPS setup above, always the same address, e.g.
`https://192.168.1.115/cooking/shopping/app/`; offline data is stored per address):
- iPhone: open it in Safari, tap Share → "Add to Home Screen", start it from the icon
  and log in once. Only the home-screen app keeps data permanently; a Safari tab can
  lose it after a week without visiting the site.
- Android: open it in Chrome and use "Install" (the app offers it too).

**How it works**
- `cooking/shopping_app_views.py`: the shell page (no data, no login needed, so it can
  be cached), `sw.js` rendered by Django with hashed static URLs, the manifest and the
  JSON API (`api/snapshot/`, `api/sync/`, `api/lists/<id>/complete/`). The API answers
  401 instead of redirecting to the login page, and extends the session at most once a
  day, so a phone used at home at least every two weeks stays logged in.
- The service worker's version is a hash of the app's files, so a deployment that
  changes any of them replaces the cached app on the next start at home. Its scope is
  `/cooking/shopping/app/` only; the rest of the site is not affected.
- `cooking/services/shopping_sync.py`: each queued operation (`item.add`,
  `item.set_purchased`, `item.set_quantity`, `item.delete`) has a UUID, and the server
  stores the result (`ShoppingSyncOperation`), so a batch resent after a dropped
  connection changes nothing twice. Check-offs carry the state, not a toggle. The later
  change wins by device time, separately for "purchased" and for quantity. Changes to
  lists completed or deleted in the meantime are skipped and the phone shows why.
- Checking off an item whose product is in the pantry restocks it at once (a purchase
  movement linked to the item); unchecking removes that movement, and a quantity change
  corrects it. Completing a list only adds items that did not restock the pantry yet,
  and creates missing products as before. Completing needs a connection.
- Items get a stable `uuid`, generated on the phone for items added offline
  (migrations `0013` and `0014`).

iOS has no background sync, so the app syncs when opened, when brought back to the
foreground, after each change and every 30 s while open.

## Scheduled tasks (cron)
Market data refresh and price history synchronisation are also available as
management commands, so they do not have to run inside a web request:
```bash
# Current prices of open positions
python manage.py refresh_market_data
python manage.py refresh_market_data --user dawid       # selected users only
python manage.py refresh_market_data --dividends        # also sync dividends (uses Alpha Vantage quota)

# Incremental daily OHLC history
python manage.py sync_price_history
python manage.py sync_price_history --force             # ignore stored coverage and error backoff
```
Without `--user` they process every user who owns brokerage instruments. A failure for
one user is reported on stderr and does not stop the run for the others.

Example crontab on the Pi:
```cron
# Prices hourly during GPW trading hours
0 9-18 * * 1-5 cd /path/to/Website-Finance && docker compose exec -T web python manage.py refresh_market_data
# History once a day after the close
30 22 * * 1-5 cd /path/to/Website-Finance && docker compose exec -T web python manage.py sync_price_history
# Dividends once a week (Alpha Vantage has a daily quota)
0 7 * * 6 cd /path/to/Website-Finance && docker compose exec -T web python manage.py refresh_market_data --dividends
```

## Project structure
```
Website-Finance/
├─ config/                    # Django project (settings, urls, wsgi, test_settings)
├─ deploy/                    # Caddyfile (HTTPS) and the phone certificate page
├─ finance/                   # Budget, brokerage portfolio, travel
│  ├─ bank_import.py          # Millennium / ING CSV import
│  ├─ brokerage.py            # Portfolio summary, FIFO, capital gains tax
│  ├─ brokerage_import.py     # XTB XLSX reader (no openpyxl)
│  ├─ market_data.py          # Yahoo / Stooq / Alpha Vantage / OpenFigi / NBP
│  ├─ portfolio_history.py    # Day-by-day portfolio valuation
│  ├─ investment_funding.py   # Links budget expenses to broker deposits
│  ├─ management/commands/    # refresh_market_data, sync_price_history
│  ├─ templates/finance/
│  └─ static/                 # css/style.css, js/
├─ cooking/                   # Recipes, pantry, shopping lists
│  ├─ constants.py            # Pantry category list and groups
│  ├─ management/commands/    # learn_pantry_catalog, preview_shared_pantry
│  ├─ services/
│  │  ├─ pantry_editing.py    # Full edit side effects: unit conversion, corrections, list items
│  │  ├─ pantry_forecast.py   # Consumption forecasting
│  │  ├─ pantry_quantities.py # Quantity, unit and package helpers shared by views and sync
│  │  ├─ pantry_sharing.py    # Merging per-user pantries into one (migration 0011)
│  │  ├─ shopping_sync.py     # Offline shopping: operations from the phone, pantry restocking
│  │  ├─ polish.py            # Polish plural forms in messages
│  │  └─ product_catalog.py   # Household memory, Open Food Facts cache, category mapping
│  ├─ shopping_app_views.py   # Offline shopping mode: shell, service worker, manifest, API
│  └─ storage.py              # Private media storage for user photos
├─ cars/                      # Fuel, services, tyres
│  └─ pdf_utils.py            # Service history PDF
├─ accounts/                  # Auth, shared household accounts
├─ habits/                    # Not in active use yet
├─ templates/                 # base.html, home.html, error pages
├─ media/                     # Public uploads (recipe and catalog images)
├─ private_media/             # Owner-only product photos, outside the public tree
├─ manage.py
├─ Dockerfile
├─ docker-compose.yml
└─ .env
```

## Environment variables
Required at minimum:
- SECRET_KEY: Django secret key (use a strong, unique value in production). Required
  when DEBUG is off; startup fails without it rather than falling back to a known key
- DEBUG: 1 for development, 0 for production. `/media/` is served regardless of this
  flag, so turning DEBUG off does not break recipe or product images
- ALLOW_PUBLIC_SIGNUP: 1 to allow new users to register; set 0 on a home server after creating accounts
- ALLOWED_HOSTS: comma-separated list (include your domain/IP in prod)
- CSRF_TRUSTED_ORIGINS: comma-separated origins with scheme and port, e.g. `http://192.168.1.115:8000,http://raspberrypi.local:8000`
- CSRF_TRUSTED_PORTS: optional comma-separated local ports used to auto-build CSRF origins from ALLOWED_HOSTS; default `8000`

Database variables expected by settings (example):
- DATABASE_NAME, DATABASE_USER, DATABASE_PASSWORD, DATABASE_HOST, DATABASE_PORT
- The legacy aliases DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT are still accepted.
- DATABASE_URL takes precedence over all of the above when set.
- `docker-compose.yml` reads DATABASE_NAME / DATABASE_USER / DATABASE_PASSWORD from the
  same `.env` to initialise the Postgres container, so no credentials live in the repo.

Cache:
- Gunicorn runs several workers, and Django's default in-memory cache is per process.
  The portfolio value history is therefore cached on the filesystem, which all workers
  in the container share.
- CACHE_BACKEND: optional, default `django.core.cache.backends.filebased.FileBasedCache`.
  Point it at Redis or Memcached if you ever add one.
- CACHE_LOCATION: optional, default `/tmp/website-finance-cache`
- CACHE_TIMEOUT: optional, default `300` seconds
- CACHE_MAX_ENTRIES: optional, default `1000`
- Cache keys embed a version derived from your transaction, snapshot and price data, so
  a stale chart can never be served after an import; restarting the container is safe.

Brokerage market data:
- Current quotes for open XTB positions are fetched in provider-safe Yahoo Finance batches; no key is required.
- ALPHA_VANTAGE_API_KEY: optional legacy fallback and explicitly requested dividend synchronization; ordinary price refreshes do not consume its daily quota
- OPENFIGI_API_KEY: ISIN to ticker/instrument mapping
- STOOQ_API_KEY: optional historical Stooq CSV fallback; imported XTB instruments use Yahoo Finance history by default

Pantry product catalog:
- OPEN_FOOD_FACTS_ENABLED: optional, defaults to `1`; set `0` to disable external lookups while keeping the local cache
- OPEN_FOOD_FACTS_USER_AGENT: identifies this installation to Open Food Facts; use `AppName/Version (contact URL or email)`
- OPEN_FOOD_FACTS_TIMEOUT: upstream request timeout in seconds, default `2.5`
- OPEN_FOOD_FACTS_RATE_LIMIT: shared server-side request budget per minute, default `12` (below the public API limit)
- Product metadata is cached in PostgreSQL and product thumbnails in `media/pantry_catalog_images`. Open Food Facts data is ODbL and images are CC BY-SA; attribution is shown in the pantry UI.
- Photos taken by a user for products missing a catalog image stay outside the public media tree in `private_media/pantry_product_images` and are served only to the product owner.
- Docker mounts `private_media/` as a persistent application volume, so user photos survive container rebuilds.
- Pantry stock keeps both the exact total quantity (for example `1000 ml`) and the number of scanned packages/items (for example `5 szt.`).

## Troubleshooting
- Page not loading: run docker compose logs -f web and docker compose logs -f db
- Static files not styled: confirm collectstatic ran and DEBUG/WhiteNoise configuration is correct
- Cannot connect to DB: ensure DATABASE_HOST=db and DATABASE_PORT=5432 in .env when using Docker.
  If you changed DATABASE_PASSWORD on an existing volume, also run the `ALTER USER`
  command shown in the Quick start section — Postgres keeps the password it was
  initialised with.
- `docker compose` refuses to start with a message about DATABASE_PASSWORD: the variable
  is missing from `.env`. The database credentials are intentionally not hardcoded.
- CSRF verification failed: open the app consistently with one address, e.g. always `http://192.168.1.115:8000` or always `http://raspberrypi.local:8000`, and include that exact origin in `CSRF_TRUSTED_ORIGINS`
- Pantry live camera unavailable on a phone: browsers require HTTPS for camera streams (localhost is the development exception). Put the app behind an HTTPS reverse proxy and add its `https://` origin to `CSRF_TRUSTED_ORIGINS`; the scanner still offers photo and manual-code fallbacks over HTTP.
- Pantry product was not recognized: Open Food Facts is community-maintained and does not contain every code. The scanner keeps the manual form available when a product is missing or the catalog is temporarily unavailable.

## Deployment notes (Raspberry Pi)
- This project runs on ARM via Docker (Postgres 15-alpine and Python slim images support ARM)
- You can automate hourly updates using a cron job that runs git pull and docker compose up -d --build
- Set `DEBUG=0` in `.env`. Media files are served independently of that flag, so images
  keep working; leaving DEBUG on exposes tracebacks with your settings to anyone on the LAN
- The database port is bound to `127.0.0.1` only. Keep it that way, or put the port behind
  a firewall rule if you genuinely need access from another machine
- The app speaks plain HTTP, so session cookies travel unencrypted on the LAN. Behind an
  HTTPS reverse proxy, set `SESSION_COOKIE_SECURE=1`, `CSRF_COOKIE_SECURE=1` and add the
  `https://` origin to `CSRF_TRUSTED_ORIGINS`. This is also what the pantry camera needs
- Prefer the `manage.py` commands above over the in-app refresh buttons for routine
  updates. Market data calls are synchronous, and with two Gunicorn workers a slow
  provider can occupy half the server's capacity

## License
MIT (or your preferred license)
