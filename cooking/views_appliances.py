import hashlib
import json
import secrets

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .models import PushSubscription, SentNotification
from .services.push import notify

MESSAGES = {
    'washer': ('Pranie zakończone', 'Można wyjąć pranie z pralki.'),
    'dishwasher': ('Zmywanie zakończone', 'Można opróżnić zmywarkę.'),
}


@method_decorator(csrf_exempt, name='dispatch')
class ApplianceFinishedView(View):
    def post(self, request, appliance):
        token = settings.HOME_ASSISTANT_APPLIANCE_TOKEN
        if not token or appliance not in MESSAGES:
            return JsonResponse({'error': 'not_found'}, status=404)

        header = request.headers.get('Authorization', '').encode('utf-8')
        if not secrets.compare_digest(header, f'Bearer {token}'.encode('utf-8')):
            return JsonResponse({'error': 'unauthorized'}, status=401)

        if request.content_type != 'application/json':
            return JsonResponse({'error': 'expected_json'}, status=415)
        raw = request.read(257)
        if len(raw) > 256:
            return JsonResponse({'error': 'body_too_large'}, status=413)
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            return JsonResponse({'error': 'invalid_json'}, status=400)
        cycle_id = data.get('cycle_id') if isinstance(data, dict) else None
        if type(cycle_id) is not int or not 0 < cycle_id < 2**63:
            return JsonResponse({'error': 'invalid_cycle'}, status=400)

        title, body = MESSAGES[appliance]
        kind = f'appliance-{appliance}'
        fingerprint = hashlib.sha256(str(cycle_id).encode()).hexdigest()
        with transaction.atomic():
            receipt, created = SentNotification.objects.get_or_create(kind=kind, fingerprint=fingerprint)
            if not created:
                return JsonResponse({'status': 'already_sent'})

            sent = notify(
                kind=kind, title=title, body=body, url=reverse('index'), tag=f'{kind}-{cycle_id}',
                subscriptions=PushSubscription.objects.filter(user__is_active=True),
            )
            if not sent:
                # HA ponowi zgłoszenie. Zachowujemy zmiany w nieaktualnych subskrypcjach.
                receipt.delete()
                return JsonResponse({'error': 'push_unavailable'}, status=503)

        return JsonResponse({'status': 'sent', 'devices': sent})
