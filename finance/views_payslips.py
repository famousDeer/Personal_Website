"""Paski wypłaty: rok (sumy, drugi próg podatkowy), import PDF, karta miesiąca.

Paski są per ``request.user`` (jak maklerka), niezależnie od aktywnego konta.
Odczyt PDF – finance.payslip_import, wyliczenia – finance.payslips.
"""
from datetime import date

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateformat import format as format_date
from django.views import View

from utils.undo import delete_with_undo

from .forms import PayslipImportForm, PayslipTaxYearForm
from .models import Payslip, PayslipTaxYear
from .payslip_import import PayslipImportError, extract_pdf_text, parse_payslip_text, save_payslip
from .payslips import build_detail, build_year


def default_year(user):
    latest = Payslip.objects.filter(user=user).order_by('-period').values_list('period', flat=True).first()
    return latest.year if latest else date.today().year


class PayslipYearView(LoginRequiredMixin, View):
    template_name = 'finance/payslips.html'

    def get(self, request, year=None, form=None):
        year = year or default_year(request.user)
        context = build_year(request.user, year)
        context['form'] = form or PayslipImportForm()
        partner = context['settlement']['partner'] if context['settlement'] else None
        context['partner_form'] = PayslipTaxYearForm(instance=partner)
        return render(request, self.template_name, context)


class PayslipImportView(LoginRequiredMixin, View):
    def post(self, request):
        form = PayslipImportForm(request.POST, request.FILES)
        if not form.is_valid():
            return PayslipYearView().get(request, form=form)
        imported = []
        for uploaded in form.cleaned_data['file']:
            try:
                parsed = parse_payslip_text(extract_pdf_text(uploaded))
            except PayslipImportError as exc:
                messages.error(request, f'{uploaded.name}: {exc}')
                continue
            payslip, created = save_payslip(request.user, parsed)
            imported.append(payslip)
            label = format_date(payslip.period, 'F Y')
            messages.success(request, f'{"Dodano" if created else "Zaktualizowano"} pasek za {label}.')
            if abs(parsed.payout_difference) > 0.01:
                messages.warning(
                    request,
                    f'{label}: wypłata z paska różni się o {parsed.payout_difference} zł od wyliczonej ze składników – sprawdź pozycje.',
                )
        if len(imported) == 1:
            payslip = imported[0]
            return redirect('finance:payslip_detail', year=payslip.period.year, month=payslip.period.month)
        if imported:
            return redirect('finance:payslips_year', year=max(p.period for p in imported).year)
        return redirect('finance:payslips')


class PayslipDetailView(LoginRequiredMixin, View):
    def get(self, request, year, month):
        payslip = get_object_or_404(
            Payslip.objects.prefetch_related('items'), user=request.user, period=date(year, month, 1),
        )
        context = build_detail(payslip)
        context['back_url'] = reverse('finance:payslips_year', args=[year])
        return render(request, 'finance/payslip_detail.html', context)


class PayslipTaxYearView(LoginRequiredMixin, View):
    """Zapis dochodu i zaliczek małżonka do szacunku wspólnego rozliczenia."""

    def post(self, request, year):
        instance = PayslipTaxYear.objects.filter(user=request.user, year=year).first()
        form = PayslipTaxYearForm(request.POST, instance=instance)
        if form.is_valid():
            entry = form.save(commit=False)
            entry.user, entry.year = request.user, year
            entry.save()
            messages.success(request, f'Zapisano dane małżonka za {year} – szacunek rozliczenia przeliczony.')
        else:
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
        return redirect(f"{reverse('finance:payslips_year', args=[year])}#ps-settlement-title")


class PayslipDeleteView(LoginRequiredMixin, View):
    def post(self, request, year, month):
        payslip = get_object_or_404(Payslip, user=request.user, period=date(year, month, 1))
        delete_with_undo(request, payslip, f'Usunięto pasek za {format_date(payslip.period, "F Y")}.')
        return redirect('finance:payslips_year', year=year)
