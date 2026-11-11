"""Paski wypłaty: parser tekstu z enova365, import PDF, rok z drugim progiem, karta miesiąca."""
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from finance.models import Payslip, PayslipTaxYear
from finance.payslip_import import PayslipImportError, extract_pdf_text, parse_payslip_text, save_payslip
from finance.payslips import (
    build_settlement, build_year, payslip_hours, pit_scale, project_tax_year, tax_year_payslips,
)

User = get_user_model()
SAMPLE = (Path(__file__).parent / 'data' / 'payslip_enova.txt').read_text(encoding='utf-8')


def sample_for(month, income_32='3 725,70'):
    return (
        SAMPLE.replace('1...31.10.2026', f'1...30.{month:02d}.2026')
        .replace('Dochód 32%:                                   3 725,70', f'Dochód 32%:                                   {income_32}')
    )


class PayslipParserTests(TestCase):
    def test_reads_summary_fields(self):
        parsed = parse_payslip_text(SAMPLE)
        self.assertEqual(parsed.period, date(2026, 10, 1))
        self.assertEqual(parsed.employer, 'FIRMA TESTOWA SP. Z O. O.')
        self.assertEqual(parsed.zus_base, Decimal('13715.12'))
        self.assertEqual(parsed.zus_employee, Decimal('1880.35'))
        self.assertEqual(parsed.health, Decimal('1065.13'))
        self.assertEqual(parsed.tax_advance, Decimal('1965.00'))
        self.assertEqual(parsed.income_32, Decimal('3725.70'))
        self.assertEqual(parsed.ppk_employee, Decimal('274.30'))
        self.assertEqual(parsed.ppk_employer, Decimal('205.73'))
        self.assertEqual(parsed.employer_zus, Decimal('2321.97'))
        self.assertEqual(parsed.total_additions, Decimal('15413.91'))
        self.assertEqual(parsed.total_deductions, Decimal('1147.41'))
        self.assertEqual(parsed.transfer, Decimal('9081.72'))
        self.assertEqual(parsed.payout_difference, Decimal('0'))

    def test_reads_items_into_columns(self):
        items = {item.position: item for item in parse_payslip_text(SAMPLE).items}
        self.assertEqual(len(items), 14)
        self.assertEqual(items[1].deduction, Decimal('700.00'))
        self.assertEqual((items[2].hours, items[2].days, items[2].addition), ('128:00', 16, Decimal('7680.00')))
        self.assertEqual(items[4].name, 'Wynagrodzenie za nadgodziny')
        self.assertEqual(items[6].hours, '4:38')
        self.assertEqual(items[9].non_cash, Decimal('205.73'))
        self.assertEqual(items[11].name, 'XLE Dojazd / Obiad 2')
        self.assertEqual(items[11].addition, Decimal('300.00'))

    def test_rejects_other_documents(self):
        with self.assertRaises(PayslipImportError):
            parse_payslip_text('Faktura VAT nr 1/2026\nRazem 100,00')

    def test_password_protected_pdf_has_clear_error(self):
        failed = subprocess.CompletedProcess([], 1, stdout=b'', stderr=b'Command Line Error: Incorrect password')
        with mock.patch('finance.payslip_import.subprocess.run', return_value=failed):
            with self.assertRaisesMessage(PayslipImportError, 'hasłem'):
                extract_pdf_text(SimpleUploadedFile('pasek.pdf', b'%PDF-1.4'))


class PayslipViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='dawid', password='pass123')
        self.client.force_login(self.user)

    def upload(self, *texts):
        files = [SimpleUploadedFile(f'pasek{i}.pdf', b'%PDF-1.4', content_type='application/pdf') for i in range(len(texts))]
        with mock.patch('finance.views_payslips.extract_pdf_text', side_effect=list(texts)):
            return self.client.post(reverse('finance:payslip_import'), {'file': files})

    def test_import_creates_payslip_and_opens_month(self):
        response = self.upload(SAMPLE)
        self.assertRedirects(response, reverse('finance:payslip_detail', args=[2026, 10]))
        payslip = Payslip.objects.get(user=self.user)
        self.assertEqual(payslip.net, Decimal('9081.72'))
        self.assertEqual(payslip.items.count(), 14)

        detail = self.client.get(response.url)
        self.assertContains(detail, 'Od brutto do przelewu')
        self.assertContains(detail, 'Wynagrodzenie za nadgodziny')
        self.assertEqual(detail.context['overtime_hours'], Decimal('25.65'))
        self.assertEqual(detail.context['worked_hours'], Decimal('201.65'))

    def test_deviation_settlement_counts_as_overtime(self):
        payslip, _ = save_payslip(self.user, parse_payslip_text(
            SAMPLE.replace('Wynagrodzenie za nadgodziny', 'Rozliczenie odchyłek        ')
        ))
        self.assertEqual(payslip_hours(payslip), (Decimal('176'), Decimal('25.65')))

    def test_reimport_replaces_month(self):
        self.upload(SAMPLE)
        self.upload(SAMPLE)
        self.assertEqual(Payslip.objects.count(), 1)
        self.assertEqual(Payslip.objects.get().items.count(), 14)

    def test_bad_file_shows_error_and_imports_rest(self):
        response = self.upload('to nie pasek', sample_for(9, '0,00'))
        self.assertRedirects(response, reverse('finance:payslip_detail', args=[2026, 9]))
        self.assertEqual(Payslip.objects.count(), 1)

    def test_year_sums_and_crossed_threshold(self):
        self.upload(sample_for(9, '0,00'), SAMPLE)
        response = self.client.get(reverse('finance:payslips_year', args=[2026]))
        self.assertEqual(response.status_code, 200)
        totals = response.context['totals']
        self.assertEqual(totals['gross'], Decimal('30827.82'))
        self.assertEqual(totals['net'], Decimal('18163.44'))
        self.assertEqual(totals['hours'], Decimal('403.30'))
        self.assertEqual(response.context['rows'][9]['hours'], Decimal('201.65'))
        self.assertEqual(response.context['avg_gross'], Decimal('15413.91'))
        self.assertEqual(response.context['avg_net'], Decimal('9081.72'))
        self.assertContains(response, 'Średnio 9082\xa0zł / mies.')
        self.assertEqual(response.context['threshold_crossed_in'].period, date(2026, 10, 1))
        self.assertEqual(response.context['threshold_remaining'], Decimal('0'))
        self.assertContains(response, 'Przekroczony')

    def test_threshold_remaining_before_crossing(self):
        save_payslip(self.user, parse_payslip_text(sample_for(1, '0,00')))
        summary = build_year(self.user, 2026, today=date(2026, 3, 1))
        # Z zaliczki: (1965 + 300) / 12% = 18 875 dochodu
        self.assertEqual(summary['threshold_remaining'], Decimal('101125.00'))
        self.assertEqual([m.month for m in summary['missing']], [2])

    def test_tax_base_from_pit_advance_with_32_percent_part(self):
        payslip, _ = save_payslip(self.user, parse_payslip_text(SAMPLE))
        # (1965 + 300 − 20% × 3725,70) / 12% = 12 665,50; sprawdzenie: 12% × 8939,80 + 32% × 3725,70 − 300 = 1965
        self.assertEqual(payslip.tax_base_estimate, Decimal('12665.50'))

    def test_tax_base_falls_back_to_health_base_without_pit(self):
        payslip, _ = save_payslip(self.user, parse_payslip_text(SAMPLE))
        payslip.tax_advance = Decimal('0')
        # 11 834,77 + 205,73 − 300
        self.assertEqual(payslip.tax_base_estimate, Decimal('11740.50'))

    def test_other_user_cannot_see_or_delete(self):
        self.upload(SAMPLE)
        other = User.objects.create_user(username='obcy', password='pass123')
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse('finance:payslip_detail', args=[2026, 10])).status_code, 404)
        self.assertEqual(self.client.post(reverse('finance:payslip_delete', args=[2026, 10])).status_code, 404)
        self.assertEqual(Payslip.objects.count(), 1)

    def test_delete(self):
        self.upload(SAMPLE)
        response = self.client.post(reverse('finance:payslip_delete', args=[2026, 10]))
        self.assertRedirects(response, reverse('finance:payslips_year', args=[2026]))
        self.assertFalse(Payslip.objects.exists())


class PayslipSettlementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='dawid', password='pass123')
        self.client.force_login(self.user)

    def add(self, month, year=2026, income_32='0,00'):
        text = sample_for(month, income_32).replace('.10.2026', f'.{month:02d}.{year}').replace(
            f'.{month:02d}.2026', f'.{month:02d}.{year}')
        return save_payslip(self.user, parse_payslip_text(text))[0]

    def test_pit_scale(self):
        self.assertEqual(pit_scale(Decimal('20000')), Decimal('0'))
        self.assertEqual(pit_scale(Decimal('100000')), Decimal('8400'))
        self.assertEqual(pit_scale(Decimal('150000')), Decimal('20400'))

    def test_tax_year_follows_payment_date(self):
        december_before = self.add(12, year=2025)
        self.add(1)
        december = self.add(12)
        periods = [p.period for p in tax_year_payslips(self.user, 2026)]
        self.assertIn(december_before.period, periods)
        self.assertNotIn(december.period, periods)
        rows = build_year(self.user, 2026)['rows']
        self.assertIsNone(rows[11]['cumulative_tax_base'])

    def test_projection_fills_missing_months_until_november(self):
        self.add(9)
        self.add(10)
        # Dochód z każdego paska: (1965 + 300) / 12% = 18 875; brakuje listopada.
        projection = project_tax_year(2026, tax_year_payslips(self.user, 2026))
        self.assertEqual(projection['projected'], [date(2026, 11, 1)])
        self.assertEqual(projection['income'], Decimal('56625.00'))
        # Listopad poniżej progu: 18 875 × 12% − 300 = 1965
        self.assertEqual(projection['advances'], Decimal('5895.00'))

    def test_gaps_before_crossing_match_income_32(self):
        self.add(1)
        self.add(4, income_32='3 725,70')
        projection = project_tax_year(2026, tax_year_payslips(self.user, 2026))
        # Do końca kwietnia dochód = 120 000 + 3725,70 (część ponad próg w kwietniu).
        # Luty i marzec: 120 000 − (12 665,50 − 3725,70) − 18 875 = 92 185,20 razem.
        self.assertTrue(projection['calibrated'])
        self.assertEqual(projection['projected'][:2], [date(2026, 2, 1), date(2026, 3, 1)])
        self.assertEqual(len(projection['projected']), 9)  # luty, marzec, maj–listopad
        average = (Decimal('123725.70')) / 4
        self.assertEqual(projection['income'], (Decimal('123725.70') + 7 * average).quantize(Decimal('0.01')))

    def test_joint_settlement_uses_partner_income(self):
        self.add(10)
        response = self.client.post(reverse('finance:payslip_tax_year', args=[2026]), {'partner_income': '50000'})
        self.assertRedirects(response, reverse('finance:payslips_year', args=[2026]) + '#ps-settlement-title',
                             fetch_redirect_response=False)
        settlement = self.client.get(reverse('finance:payslips_year', args=[2026])).context['settlement']
        # Ty: październik + prognoza listopada = 37 750 dochodu, 3930 zaliczek;
        # małżonek: 50 000, zaliczki ze skali 2400.
        self.assertEqual(settlement['income'], Decimal('37750.00'))
        self.assertEqual(settlement['own_tax'], Decimal('930'))
        self.assertEqual(settlement['partner_paid'], Decimal('2400'))
        self.assertEqual(settlement['joint_tax'], 2 * pit_scale(Decimal('43875')))  # 2 × 1665
        self.assertEqual(settlement['joint_refund'], Decimal('3930') + Decimal('2400') - Decimal('3330'))

    def test_joint_benefit_when_one_income_crosses_threshold(self):
        self.add(10, income_32='3 725,70')
        settlement = build_settlement(self.user, 2026, tax_year_payslips(self.user, 2026))
        self.assertNotIn('joint_tax', settlement)
        PayslipTaxYear.objects.create(user=self.user, year=2026, partner_income=Decimal('50000'))
        own = Decimal('150000')
        with mock.patch('finance.payslips.project_tax_year', return_value={
            'income': own, 'advances': Decimal('20400'), 'projected': [], 'average': own / 12,
        }):
            settlement = build_settlement(self.user, 2026, [])
        self.assertEqual(settlement['joint_benefit'], Decimal('6000'))  # 20 400 + 2400 − 2 × 8400
        self.assertEqual(settlement['joint_refund'], Decimal('6000'))
