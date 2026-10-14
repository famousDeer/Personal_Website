"""Strona „Analiza domowa” (finance.household)."""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from .account_utils import ensure_personal_finance_account
from .household import DEFAULT_PERIOD, PERIOD_CHOICES, build_household_analysis, save_category_kinds


def _months(value):
    try:
        months = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PERIOD
    return months if months in PERIOD_CHOICES else DEFAULT_PERIOD


class HouseholdAnalysisView(LoginRequiredMixin, View):
    template_name = 'finance/household_analysis.html'

    def get(self, request):
        months = _months(request.GET.get('okres'))
        context = build_household_analysis(request.user, months)
        context['my_account'] = ensure_personal_finance_account(request.user)
        return render(request, self.template_name, context)

    def post(self, request):
        months = _months(request.POST.get('okres'))
        action = request.POST.get('action')
        anchor = ''
        if action == 'share':
            account = ensure_personal_finance_account(request.user)
            account.share_in_household_analysis = request.POST.get('share') == '1'
            account.save(update_fields=['share_in_household_analysis'])
            messages.success(
                request,
                'Udostępniasz sumy ze swojego konta osobistego domownikom z kont wspólnych.'
                if account.share_in_household_analysis
                else 'Twoje konto osobiste nie jest już widoczne w analizie innych domowników.',
            )
            anchor = '#udostepnianie'
        elif action == 'kinds':
            changes = {}
            for key, value in request.POST.items():
                if key.startswith('kind_'):
                    category = request.POST.get(f'category_{key[5:]}', '')
                    if category:
                        changes[category] = value
            changed = save_category_kinds(changes, request.user)
            messages.success(
                request,
                f'Zapisano rodzaj {changed} kategorii. Klasyfikacja jest wspólna dla całego domu.'
                if changed else 'Bez zmian w klasyfikacji kategorii.',
            )
            anchor = '#kategorie'
        url = reverse('finance:household_analysis')
        if months != DEFAULT_PERIOD:
            url += f'?okres={months}'
        return redirect(url + anchor)
