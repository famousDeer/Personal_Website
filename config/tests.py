from unittest import mock

from django.db.utils import OperationalError
from django.test import TestCase, override_settings


@override_settings(ALLOWED_HOSTS=['raspberrypi.local'])
class HealthCheckTests(TestCase):
    def test_ok_without_allowed_host(self):
        # Healthcheck Dockera pyta 127.0.0.1, którego nie ma w ALLOWED_HOSTS.
        response = self.client.get('/healthz', HTTP_HOST='127.0.0.1:8000')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'ok')
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_other_paths_still_check_host(self):
        response = self.client.get('/accounts/login/', HTTP_HOST='127.0.0.1:8000')
        self.assertEqual(response.status_code, 400)

    def test_database_down_is_503(self):
        with mock.patch('config.health.connection.cursor', side_effect=OperationalError):
            response = self.client.get('/healthz', HTTP_HOST='127.0.0.1:8000')
        self.assertEqual(response.status_code, 503)
