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
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

EXPOSE 8000

# Nie uruchamiamy manage.py podczas builda
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "config.wsgi:application"]
