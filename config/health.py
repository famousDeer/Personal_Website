"""Odpowiedź dla healthchecka Dockera (`GET /healthz`).

Middleware stoi na początku listy, przed CommonMiddleware, bo ten sprawdza
nagłówek Host względem ALLOWED_HOSTS. Healthcheck w kontenerze pyta
http://127.0.0.1:8000, a tego adresu nie musi być w ALLOWED_HOSTS.

Zwraca 200, gdy Django działa i baza odpowiada, w przeciwnym razie 503.
Nie zdradza nic poza "ok"/"db".
"""
from django.db import connection
from django.http import HttpResponse

HEALTH_PATH = '/healthz'


class HealthCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path != HEALTH_PATH:
            return self.get_response(request)
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
        except Exception:
            response = HttpResponse('db', status=503, content_type='text/plain')
        else:
            response = HttpResponse('ok', content_type='text/plain')
        response['Cache-Control'] = 'no-store'
        return response
