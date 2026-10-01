FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Wszystkie pakiety systemowe w JEDNEJ warstwie, razem z apt-get update.
# Ostatnia linia kasuje listy pakietów, żeby obraz był mniejszy - więc każde
# `apt install` dopisane w późniejszej warstwie (np. za pip install) nie
# znajdzie już żadnego pakietu i build kończy się "exit code: 100".
# Nowy pakiet systemowy dopisuj tutaj, do listy poniżej.
#   fonts-dejavu-core - polskie znaki w eksporcie PDF (reportlab)
#   curl              - używany na Raspberry Pi
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# --no-cache-dir: pip nie zostawia w obrazie kopii pobranych paczek.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# Pliki statyczne zbierane RAZ, przy buildzie, a nie przy każdym starcie
# kontenera (liczenie skrótów i kompresja ~1000 plików trwały na Pi kilkanaście
# sekund, w czasie których strona nie działała). Zmienne poniżej istnieją
# tylko na czas tego polecenia: collectstatic nie łączy się z bazą i nie
# potrzebuje prawdziwego SECRET_KEY. Plik .env nie trafia do obrazu
# (.dockerignore), prawdziwe ustawienia dochodzą przy starcie z env_file.
RUN SECRET_KEY=build-only-not-used-at-runtime \
    ALLOWED_HOSTS=localhost \
    DEBUG=0 \
    python manage.py collectstatic --noinput

EXPOSE 8000

# Właściwe polecenie startowe (migracje + gunicorn) jest w docker-compose.yml.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "config.wsgi:application"]
