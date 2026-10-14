"""Strona „Analiza domowa” i szczegóły miesiąca (finance.household)."""
from datetime import date

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from .account_utils import ensure_personal_finance_account
from .household import (
    DEFAULT_PERIOD,
    PERIOD_CHOICES,
    build_household_analysis,
    build_month_detail,
    save_category_kinds,
)


def _months(value):
    try:
        months = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PERIOD
    return months if months in PERIOD_CHOICES else DEFAULT_PERIOD


def _month_url(month, months):
    url = reverse('finance:household_month', args=[month.year, month.month])
    return url + (f'?okres={months}' if months != DEFAULT_PERIOD else '')


class HouseholdAnalysisView(LoginRequiredMixin, View):
    template_name = 'finance/household_analysis.html'

    def get(self, request):
        months = _months(request.GET.get('okres'))
        context = build_household_analysis(request.user, months)
        context['my_account'] = ensure_personal_finance_account(request.user)
        # Kliknięcie miesiąca na wykresie albo w liście pod nim: szczegóły.
        for row in context['monthly']:
            row['url'] = _month_url(row['month'], months)
        context['chart']['urls'] = [row['url'] for row in context['monthly']]
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


class HouseholdMonthView(LoginRequiredMixin, View):
    """Jeden miesiąc analizy domowej na tle średniej z poprzednich miesięcy."""

    template_name = 'finance/household_month.html'

    def get(self, request, year, month):
        try:
            selected = date(year, month, 1)
        except ValueError:
            raise Http404('Nie ma takiego miesiąca.')
        if selected > timezone.localdate().replace(day=1):
            raise Http404('Ten miesiąc jeszcze się nie zaczął.')
        months = _months(request.GET.get('okres'))
        context = build_month_detail(request.user, selected)
        back = reverse('finance:household_analysis')
        context.update({
            'months': months,
            'back_url': back + (f'?okres={months}' if months != DEFAULT_PERIOD else ''),
            'previous_url': _month_url(context['previous'], months),
            'next_url': _month_url(context['next'], months) if context['next'] else '',
        })
        return render(request, self.template_name, context)
