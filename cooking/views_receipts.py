"""Paragony w spiżarni: zdjęcie → odczyt → „Sprawdź” → „Dodaj do spiżarni”."""
import json
import mimetypes
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from utils.undo import delete_with_undo

from .models import PantryProduct, ProductGroup, Receipt, ReceiptAlias, ReceiptLine, ReceiptPhoto
from .services.pantry_quantities import parse_pantry_decimal
from .services.polish import polish_count
from .services.receipt_ocr import ReceiptOcrError, compress_upload, is_pdf, pdf_pages
from .services.receipt_parser import parse_receipt
from .services.receipt_prices import chart_data, price_summaries, price_summary
from .services.receipts import (
    ACTION_ADD, ACTION_IGNORE, ACTION_SKIP, LineDecision, apply_ocr_text, confirm_receipt,
    default_quantity, receipt_unpack_items, scan_receipt_barcode, start_ai_reading, start_processing,
)
from .services.receipt_ai import ai_enabled
from .services.pantry_batches import date_undated_stock, parse_expiry, quick_dates
from .views import normalize_pantry_barcode

MAX_PHOTOS = 4
MAX_PHOTO_BYTES = 15 * 1024 * 1024


class ReceiptListView(LoginRequiredMixin, View):
    template_name = 'cooking/receipts.html'

    def get(self, request):
        receipts = Receipt.objects.select_related('store').prefetch_related('lines')[:40]
        return render(request, self.template_name, {'receipts': receipts})

    def post(self, request):
        photos = request.FILES.getlist('photos')[:MAX_PHOTOS]
        text = (request.POST.get('text') or '').strip()
        if not photos and not text:
            messages.error(request, 'Dodaj zdjęcie paragonu albo wklej jego tekst.')
            return redirect('cooking:receipts')
        prepared = []
        pdf_texts = []
        for upload in photos:
            if upload.size > MAX_PHOTO_BYTES:
                messages.error(request, 'Plik jest większy niż 15 MB.')
                return redirect('cooking:receipts')
            try:
                if is_pdf(upload):
                    pdf_text, pages = pdf_pages(upload)
                    pdf_texts.append(pdf_text)
                    prepared.extend(pages)
                else:
                    prepared.append(compress_upload(upload))
            except ReceiptOcrError as exc:
                messages.error(request, str(exc))
                return redirect('cooking:receipts')
        prepared = prepared[:MAX_PHOTOS]
        # PDF z warstwą tekstową czytamy bez OCR - tekst jest dokładny.
        if not text and pdf_texts and parse_receipt('\n'.join(pdf_texts)).lines:
            text = '\n'.join(pdf_texts)

        receipt = Receipt.objects.create(created_by=request.user)
        for position, content in enumerate(prepared, start=1):
            photo = ReceiptPhoto(receipt=receipt, position=position)
            photo.image.save(f'{receipt.uuid}-{position}.jpg', content, save=True)
        if text:
            apply_ocr_text(receipt, text, engine='pdf' if pdf_texts and not request.POST.get('text') else 'text')
        else:
            start_processing(receipt)
        return redirect('cooking:receipt-detail', receipt_uuid=receipt.uuid)


def _datalist_names():
    names = list(PantryProduct.objects.order_by('name').values_list('name', flat=True))
    names += list(ProductGroup.objects.order_by('name').values_list('name', flat=True))
    return sorted(set(names), key=str.lower)


def _line_rows(receipt):
    aliases = {}
    for alias in ReceiptAlias.objects.filter(
        normalized_text__in=[line.normalized_text for line in receipt.lines.all()],
    ).order_by('store_id'):
        if alias.store_id in (receipt.store_id, None):
            aliases.setdefault(alias.normalized_text, alias)
    rows = []
    for line in receipt.lines.select_related('product', 'group', 'movement'):
        alias = aliases.get(line.normalized_text)
        multiplier = alias.multiplier if alias and line.match_status == ReceiptLine.MATCH_AUTO else 1
        target = line.group or line.product
        if line.match_status == ReceiptLine.MATCH_IGNORED:
            action = ACTION_IGNORE
        elif line.match_status == ReceiptLine.MATCH_SKIPPED or (
            target is None and (line.paid_price <= 0 or 'KAUCJA' in line.normalized_text)
        ):
            # Kaucja za butelkę i gratisy za 0 zł zwykle nie idą do spiżarni.
            action = ACTION_SKIP
        else:
            action = ACTION_ADD
        rows.append({
            'line': line,
            'target': target.name if target else '',
            'quantity': default_quantity(line, multiplier),
            'action': action,
            'kind': (
                'known' if line.match_status in (ReceiptLine.MATCH_AUTO, ReceiptLine.MATCH_CONFIRMED)
                else 'ignored' if line.match_status == ReceiptLine.MATCH_IGNORED
                else 'suggested' if target else 'new'
            ),
        })
    return rows


class ReceiptDetailView(LoginRequiredMixin, View):
    template_name = 'cooking/receipt_detail.html'

    def get(self, request, receipt_uuid):
        receipt = get_object_or_404(Receipt.objects.select_related('store', 'duplicate_of'), uuid=receipt_uuid)
        rows = _line_rows(receipt) if receipt.status in (Receipt.STATUS_REVIEW, Receipt.STATUS_DONE) else []
        counts = {kind: sum(1 for row in rows if row['kind'] == kind) for kind in ('known', 'suggested', 'new', 'ignored')}
        has_photos = receipt.photos.exclude(image='').exists()
        ai_available = (
            ai_enabled() and has_photos and receipt.ocr_engine != 'ai'
            and (receipt.status == Receipt.STATUS_FAILED
                 or (receipt.status == Receipt.STATUS_REVIEW and (not receipt.total_matches or receipt.error)))
        )
        return render(request, self.template_name, {
            'receipt': receipt,
            'rows': rows,
            'counts': counts,
            'photos': [photo for photo in receipt.photos.all() if photo.image],
            'names': _datalist_names() if receipt.status == Receipt.STATUS_REVIEW else [],
            'unpack_items': receipt_unpack_items(receipt) if receipt.status == Receipt.STATUS_DONE else [],
            'lines_total': receipt.lines_total,
            'ai_available': ai_available,
        })

    def post(self, request, receipt_uuid):
        receipt = get_object_or_404(Receipt, uuid=receipt_uuid)
        decisions = {}
        for line in receipt.lines.all():
            action = request.POST.get(f'action_{line.pk}', ACTION_SKIP)
            if action not in (ACTION_ADD, ACTION_SKIP, ACTION_IGNORE):
                action = ACTION_SKIP
            try:
                quantity = parse_pantry_decimal(request.POST.get(f'qty_{line.pk}'), default='0')
            except ValueError:
                quantity = Decimal('0')
            decisions[line.pk] = LineDecision(
                action=action, target=request.POST.get(f'target_{line.pk}', ''), quantity=quantity,
            )
        try:
            added, linked, ticked, warnings = confirm_receipt(receipt, decisions, user=request.user)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('cooking:receipt-detail', receipt_uuid=receipt.uuid)
        parts = [f'Dodano do spiżarni: {polish_count(added, "pozycja", "pozycje", "pozycji")}']
        if linked:
            parts.append(f'{polish_count(linked, "pozycja była", "pozycje były", "pozycji było")} już dodane skanem')
        if ticked:
            parts.append(f'odhaczono na liście zakupów: {ticked}')
        missing = len(receipt_unpack_items(receipt))
        if missing:
            parts.append(
                f'{polish_count(missing, "produkt czeka", "produkty czekają", "produktów czeka")} '
                'na kod kreskowy albo termin - „Rozpakuj zakupy”'
            )
        messages.success(request, '. '.join(parts) + '.')
        for warning in warnings[:5]:
            messages.warning(request, warning)
        return redirect('cooking:receipt-detail', receipt_uuid=receipt.uuid)


class ReceiptRetryView(LoginRequiredMixin, View):
    def post(self, request, receipt_uuid):
        receipt = get_object_or_404(Receipt, uuid=receipt_uuid)
        if receipt.status == Receipt.STATUS_DONE:
            messages.error(request, 'Zatwierdzonego paragonu nie da się odczytać ponownie.')
        elif not receipt.photos.exclude(image='').exists():
            messages.error(request, 'Ten paragon nie ma już zdjęcia.')
        else:
            start_processing(receipt)
        return redirect('cooking:receipt-detail', receipt_uuid=receipt.uuid)


class ReceiptDeleteView(LoginRequiredMixin, View):
    def post(self, request, receipt_uuid):
        receipt = get_object_or_404(Receipt, uuid=receipt_uuid)
        if receipt.status == Receipt.STATUS_DONE:
            messages.error(request, 'Paragon jest już w spiżarni - zmiany zrób w spiżarni.')
            return redirect('cooking:receipt-detail', receipt_uuid=receipt.uuid)
        delete_with_undo(request, receipt, 'Usunięto paragon.')
        return redirect('cooking:receipts')


class ReceiptPhotoView(LoginRequiredMixin, View):
    def get(self, request, receipt_uuid, photo_id):
        photo = ReceiptPhoto.objects.filter(pk=photo_id, receipt__uuid=receipt_uuid).first()
        if not photo or not photo.image:
            raise Http404
        try:
            image_file = photo.image.open('rb')
        except (FileNotFoundError, OSError):
            raise Http404 from None
        response = FileResponse(image_file, content_type=mimetypes.guess_type(photo.image.name)[0] or 'image/jpeg')
        response['Cache-Control'] = 'private, max-age=3600'
        response['X-Content-Type-Options'] = 'nosniff'
        return response



class PriceListView(LoginRequiredMixin, View):
    """Ceny wszystkich produktów kupionych z paragonem: ostatnia cena
    regularna, zmiana i najtańszy sklep."""

    template_name = 'cooking/prices.html'

    def get(self, request):
        query = (request.GET.get('q') or '').strip()
        summaries = price_summaries()
        products = PantryProduct.objects.filter(pk__in=summaries.keys()).order_by('name')
        if query:
            products = products.filter(name__icontains=query)
        rows = [{'product': product, 'price': summaries[product.pk]} for product in products]
        return render(request, self.template_name, {
            'rows': rows,
            'query': query,
            'up_count': sum(1 for row in rows if row['price'].trend == 'up'),
            'down_count': sum(1 for row in rows if row['price'].trend == 'down'),
        })


class ProductPriceView(LoginRequiredMixin, View):
    template_name = 'cooking/product_prices.html'

    def get(self, request, product_id):
        product = get_object_or_404(PantryProduct, pk=product_id)
        summary = price_summary(product)
        return render(request, self.template_name, {
            'product': product,
            'price': summary,
            'chart': chart_data(summary),
        })


class ReceiptBarcodesView(LoginRequiredMixin, View):
    """„Rozpakuj zakupy”: aparat działa cały czas, każdy zeskanowany kod sam
    trafia do produktu z paragonu, a przy nabiale od razu pada pytanie o termin
    (jedno dotknięcie daty)."""

    template_name = 'cooking/receipt_barcodes.html'

    def get(self, request, receipt_uuid):
        receipt = get_object_or_404(Receipt.objects.select_related('store'), uuid=receipt_uuid)
        return render(request, self.template_name, {
            'receipt': receipt,
            'remaining': receipt_unpack_items(receipt),
            'quick_dates': quick_dates(),
        })

    def post(self, request, receipt_uuid):
        receipt = get_object_or_404(Receipt, uuid=receipt_uuid)
        try:
            payload = json.loads(request.body or b'{}')
            product_id = int(payload.get('product_id') or 0) or None
            if payload.get('action') == 'date':
                product = get_object_or_404(PantryProduct, pk=product_id)
                expires_on = parse_expiry(payload.get('expires_on'))
                if expires_on is None:
                    raise ValueError('Wybierz datę przydatności.')
                date_undated_stock(product, expires_on)
                result = {'status': 'dated', 'product': product.name, 'product_id': product.pk,
                          'expires_on': expires_on.isoformat()}
            else:
                barcode = normalize_pantry_barcode(payload.get('barcode'))
                result = scan_receipt_barcode(receipt, barcode, product_id, merge=bool(payload.get('merge')))
                result['barcode'] = barcode
        except (ValueError, TypeError) as exc:
            return JsonResponse({'ok': False, 'error': str(exc) or 'Niepoprawny kod.'}, status=400)
        result['ok'] = True
        result['remaining'] = receipt_unpack_items(receipt)
        return JsonResponse(result)


class ReceiptAiView(LoginRequiredMixin, View):
    """„Odczytaj przez AI” - dopiero tu zdjęcie paragonu wychodzi z domu."""

    def post(self, request, receipt_uuid):
        receipt = get_object_or_404(Receipt, uuid=receipt_uuid)
        if not ai_enabled():
            messages.error(request, 'Odczyt przez AI nie jest skonfigurowany (ANTHROPIC_API_KEY w .env).')
        elif receipt.status in (Receipt.STATUS_DONE, Receipt.STATUS_PROCESSING):
            messages.error(request, 'Tego paragonu nie można teraz odczytać ponownie.')
        elif not receipt.photos.exclude(image='').exists():
            messages.error(request, 'Ten paragon nie ma zdjęcia.')
        else:
            start_ai_reading(receipt)
        return redirect('cooking:receipt-detail', receipt_uuid=receipt.uuid)
