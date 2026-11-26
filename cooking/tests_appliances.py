from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import PushSubscription, SentNotification


@override_settings(
    HOME_ASSISTANT_APPLIANCE_TOKEN='test-token',
    VAPID_PUBLIC_KEY='publiczny',
    VAPID_PRIVATE_KEY='prywatny',
)
class ApplianceFinishedTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(username='anna')
        inactive = user_model.objects.create_user(username='nieaktywny', is_active=False)
        self.phone = self.subscribe(user, 'anna')
        self.subscribe(inactive, 'nieaktywny')
        self.client = Client(enforce_csrf_checks=True)

    def subscribe(self, user, name):
        return PushSubscription.objects.create(
            user=user, endpoint=f'https://push.example.test/{name}', p256dh='klucz', auth='sekret',
        )

    def post(self, appliance='washer', data=None, token='test-token', content_type='application/json'):
        return self.client.post(
            reverse('cooking:appliance-finished', args=[appliance]),
            {'cycle_id': 123} if data is None else data,
            content_type=content_type, headers={'Authorization': f'Bearer {token}'},
        )

    def test_separate_messages_and_one_notification_per_cycle(self):
        with patch('cooking.services.push.send_to_subscription', return_value=True) as send:
            self.assertEqual(self.post().json(), {'status': 'sent', 'devices': 1})
            self.assertEqual(self.post().json(), {'status': 'already_sent'})
            self.assertEqual(self.post('dishwasher').json(), {'status': 'sent', 'devices': 1})
            self.assertEqual(self.post(data={'cycle_id': 124}).json()['status'], 'sent')

        self.assertEqual(send.call_count, 3)
        self.assertEqual(
            [(call.args[0].pk, call.args[1]['title']) for call in send.call_args_list],
            [(self.phone.pk, 'Pranie zakończone'), (self.phone.pk, 'Zmywanie zakończone'),
             (self.phone.pk, 'Pranie zakończone')],
        )
        for call in send.call_args_list:
            self.assertEqual(call.args[1]['url'], reverse('index'))
            self.assertNotIn('app_badge', call.args[1])
        self.assertEqual(len({call.args[1]['tag'] for call in send.call_args_list}), 3)

    def test_retries_same_cycle_if_no_phone_received_it(self):
        with patch('cooking.services.push.send_to_subscription', side_effect=[False, True]) as send:
            self.assertEqual(self.post().status_code, 503)
            self.assertFalse(SentNotification.objects.exists())
            self.assertEqual(self.post().json(), {'status': 'sent', 'devices': 1})
            self.assertEqual(self.post().json(), {'status': 'already_sent'})
        self.assertEqual(send.call_count, 2)

    def test_rejects_unknown_appliance_bad_token_and_invalid_payload(self):
        with patch('cooking.services.push.send_to_subscription') as send:
            self.assertEqual(self.post('other').status_code, 404)
            self.assertEqual(self.post(token='wrong').status_code, 401)
            self.assertEqual(self.post(content_type='text/plain').status_code, 415)
            self.assertEqual(self.post(data='{' * 257).status_code, 413)
            for data in ('not JSON', '[]', {}, {'cycle_id': True}, {'cycle_id': -1}, {'cycle_id': '123'}):
                with self.subTest(data=data):
                    self.assertEqual(self.post(data=data).status_code, 400)
            send.assert_not_called()

    @override_settings(HOME_ASSISTANT_APPLIANCE_TOKEN='')
    def test_disabled_without_token(self):
        self.assertEqual(self.post().status_code, 404)
