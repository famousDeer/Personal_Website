from decimal import Decimal
from io import BytesIO
import os
from xml.sax.saxutils import escape

from django.utils import timezone
import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN_X = 16 * mm
MARGIN_TOP = 20 * mm
MARGIN_BOTTOM = 18 * mm
CONTENT_WIDTH = PAGE_WIDTH - (2 * MARGIN_X)
SERVICE_CARD_WIDTH = CONTENT_WIDTH
SERVICE_CARD_PADDING = 5 * mm
SERVICE_INNER_WIDTH = SERVICE_CARD_WIDTH - (2 * SERVICE_CARD_PADDING)
SERVICE_PRICE_WIDTH = 34 * mm
SERVICE_NUMBER_WIDTH = 12 * mm
REPORTLAB_FONT_DIR = os.path.join(os.path.dirname(reportlab.__file__), "fonts")

PALETTE = {
    "ink": colors.HexColor("#172033"),
    "muted": colors.HexColor("#647084"),
    "light_muted": colors.HexColor("#8a94a6"),
    "line": colors.HexColor("#d8dee8"),
    "soft_line": colors.HexColor("#edf1f6"),
    "paper": colors.HexColor("#f7f9fc"),
    "white": colors.white,
    "primary": colors.HexColor("#244f8f"),
    "primary_dark": colors.HexColor("#17365f"),
    "primary_soft": colors.HexColor("#e7eef8"),
    "success": colors.HexColor("#287a53"),
    "danger": colors.HexColor("#a6424c"),
    "danger_soft": colors.HexColor("#fdecef"),
    "warning": colors.HexColor("#996d28"),
}

FONT_PATHS = {
    "regular": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/local/share/fonts/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        os.path.join(REPORTLAB_FONT_DIR, "Vera.ttf"),
    ],
    "bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        os.path.join(REPORTLAB_FONT_DIR, "VeraBd.ttf"),
    ],
}


def _first_existing_font(paths):
    for path in paths:
        try:
            with open(path, "rb"):
                return path
        except OSError:
            continue
    return None


def _register_fonts():
    regular_path = _first_existing_font(FONT_PATHS["regular"])
    bold_path = _first_existing_font(FONT_PATHS["bold"])

    if regular_path:
        pdfmetrics.registerFont(TTFont("AppSans", regular_path))
        pdfmetrics.registerFont(TTFont("AppSans-Bold", bold_path or regular_path))
        return {
            "regular": "AppSans",
            "bold": "AppSans-Bold",
        }

    return {
        "regular": "Helvetica",
        "bold": "Helvetica-Bold",
    }


FONTS = _register_fonts()

STYLES = {
    "hero_label": ParagraphStyle(
        "HeroLabel",
        fontName=FONTS["bold"],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#b8cbe8"),
        uppercase=True,
    ),
    "hero": ParagraphStyle(
        "Hero",
        fontName=FONTS["bold"],
        fontSize=27,
        leading=32,
        textColor=PALETTE["white"],
    ),
    "hero_meta": ParagraphStyle(
        "HeroMeta",
        fontName=FONTS["regular"],
        fontSize=11,
        leading=15,
        textColor=colors.HexColor("#d9e5f7"),
    ),
    "title": ParagraphStyle(
        "Title",
        fontName=FONTS["bold"],
        fontSize=18,
        leading=23,
        textColor=PALETTE["ink"],
    ),
    "subtitle": ParagraphStyle(
        "Subtitle",
        fontName=FONTS["regular"],
        fontSize=9.5,
        leading=13,
        textColor=PALETTE["muted"],
    ),
    "label": ParagraphStyle(
        "Label",
        fontName=FONTS["bold"],
        fontSize=7.5,
        leading=10,
        textColor=PALETTE["muted"],
    ),
    "metric": ParagraphStyle(
        "Metric",
        fontName=FONTS["bold"],
        fontSize=13,
        leading=17,
        textColor=PALETTE["ink"],
    ),
    "body": ParagraphStyle(
        "Body",
        fontName=FONTS["regular"],
        fontSize=9.5,
        leading=14,
        textColor=PALETTE["ink"],
    ),
    "body_muted": ParagraphStyle(
        "BodyMuted",
        fontName=FONTS["regular"],
        fontSize=9,
        leading=13,
        textColor=PALETTE["muted"],
    ),
    "body_bold": ParagraphStyle(
        "BodyBold",
        fontName=FONTS["bold"],
        fontSize=9.5,
        leading=13,
        textColor=PALETTE["ink"],
    ),
    "small": ParagraphStyle(
        "Small",
        fontName=FONTS["regular"],
        fontSize=8,
        leading=11,
        textColor=PALETTE["muted"],
    ),
    "small_bold": ParagraphStyle(
        "SmallBold",
        fontName=FONTS["bold"],
        fontSize=8,
        leading=11,
        textColor=PALETTE["ink"],
    ),
    "right": ParagraphStyle(
        "Right",
        fontName=FONTS["bold"],
        fontSize=9,
        leading=12,
        alignment=TA_RIGHT,
        textColor=PALETTE["ink"],
    ),
    "center_white": ParagraphStyle(
        "CenterWhite",
        fontName=FONTS["bold"],
        fontSize=8,
        leading=10,
        alignment=TA_CENTER,
        textColor=PALETTE["white"],
    ),
}


def _p(text, style="body"):
    safe_text = escape(str(text or "")).replace("\n", "<br/>")
    return Paragraph(safe_text, STYLES[style])


def _money(value):
    amount = value or Decimal("0.00")
    return f"{amount:,.2f}".replace(",", " ") + " zł"


def _number(value):
    return f"{int(value or 0):,}".replace(",", " ")


def _date(value):
    if not value:
        return "-"
    return value.strftime("%d.%m.%Y")


def _parts_total(service):
    return service.parts_total or Decimal("0.00")


def _on_page(canvas, doc, car):
    canvas.saveState()
    canvas.setFillColor(PALETTE["paper"])
    canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, stroke=0, fill=1)

    header_x = MARGIN_X
    header_y = PAGE_HEIGHT - 16 * mm
    header_w = PAGE_WIDTH - (2 * MARGIN_X)
    header_h = 9 * mm
    canvas.setFillColor(PALETTE["white"])
    canvas.setStrokeColor(PALETTE["soft_line"])
    canvas.roundRect(header_x, header_y, header_w, header_h, 4 * mm, stroke=1, fill=1)

    canvas.setFont(FONTS["bold"], 8)
    canvas.setFillColor(PALETTE["primary"])
    canvas.drawString(header_x + 6 * mm, header_y + 3 * mm, "Historia serwisowa")
    canvas.setFillColor(PALETTE["ink"])
    canvas.drawRightString(header_x + header_w - 6 * mm, header_y + 3 * mm, f"{car.brand} {car.model}")

    footer_y = 10 * mm
    canvas.setStrokeColor(PALETTE["line"])
    canvas.line(MARGIN_X, footer_y + 7 * mm, PAGE_WIDTH - MARGIN_X, footer_y + 7 * mm)
    canvas.setFont(FONTS["regular"], 7)
    canvas.setFillColor(PALETTE["light_muted"])
    generated = timezone.localtime(timezone.now()).strftime("%d.%m.%Y %H:%M")
    canvas.drawString(MARGIN_X, footer_y, f"Wygenerowano: {generated}")
    canvas.drawRightString(PAGE_WIDTH - MARGIN_X, footer_y, f"Strona {doc.page}")
    canvas.restoreState()


def _section(title, subtitle=None):
    content = [_p(title, "title")]
    if subtitle:
        content.append(Spacer(1, 1.5 * mm))
        content.append(_p(subtitle, "subtitle"))
    return KeepTogether(content + [Spacer(1, 5 * mm)])


def _hero(car):
    data = [
        [_p("KSIĄŻKA SERWISOWA", "hero_label")],
        [_p(f"{car.brand} {car.model}", "hero")],
        [_p(f"{car.year} | {car.fuel_type} | przebieg {_number(car.odometer)} km", "hero_meta")],
    ]
    table = Table(data, colWidths=[CONTENT_WIDTH], rowHeights=[11 * mm, 18 * mm, 10 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALETTE["primary_dark"]),
                ("BOX", (0, 0), (-1, -1), 0, PALETTE["primary_dark"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 12 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12 * mm),
                ("TOPPADDING", (0, 0), (-1, 0), 9 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    return table


def _metric_row(services):
    total_service = sum((service.cost for service in services), Decimal("0.00"))
    total_parts = sum((_parts_total(service) for service in services), Decimal("0.00"))
    last_service = services[0] if services else None
    metrics = [
        ("WPISY", str(len(services)), PALETTE["primary"]),
        ("KOSZT USŁUG", _money(total_service), PALETTE["danger"]),
        ("CZĘŚCI", _money(total_parts), PALETTE["warning"]),
        ("OSTATNI SERWIS", _date(last_service.date) if last_service else "-", PALETTE["success"]),
    ]
    col_width = (CONTENT_WIDTH - (3 * 4 * mm)) / 4
    cards = []
    for label, value, accent in metrics:
        card = Table(
            [[_p(label, "label")], [_p(value, "metric")]],
            colWidths=[col_width],
            rowHeights=[9 * mm, 13 * mm],
        )
        card.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), PALETTE["white"]),
                    ("BOX", (0, 0), (-1, -1), 0.7, PALETTE["soft_line"]),
                    ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
                    ("TOPPADDING", (0, 0), (-1, 0), 5 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
                    ("TOPPADDING", (0, 1), (-1, 1), 0),
                    ("BOTTOMPADDING", (0, 1), (-1, 1), 4 * mm),
                ]
            )
        )
        cards.append(card)

    row = Table([cards], colWidths=[col_width] * 4)
    row.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return row


def _vehicle_details(car):
    rows = [
        ("MARKA I MODEL", f"{car.brand} {car.model}"),
        ("ROK PRODUKCJI", car.year),
        ("RODZAJ PALIWA", car.fuel_type),
        ("AKTUALNY PRZEBIEG", f"{_number(car.odometer)} km"),
        ("CENA ZAKUPU", _money(car.price)),
    ]
    data = [[_p(label, "label") for label, _ in rows], [_p(value, "body_bold") for _, value in rows]]
    table = Table(data, colWidths=[CONTENT_WIDTH / 5] * 5, rowHeights=[9 * mm, 13 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALETTE["white"]),
                ("BOX", (0, 0), (-1, -1), 0.7, PALETTE["soft_line"]),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, PALETTE["soft_line"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 5 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 4 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    return table


def _service_card(index, service):
    parts = list(service.parts.all())
    part_rows = [[_p("Część / materiał", "label"), _p("Kwota", "label")]]
    if parts:
        part_rows.extend([[_p(part.name, "small_bold"), _p(_money(part.price), "right")] for part in parts])
    else:
        part_rows.append([_p("Brak wyszczególnionych części.", "small"), _p("-", "right")])

    part_rows.append([_p("Suma części", "small_bold"), _p(_money(_parts_total(service)), "right")])
    parts_name_width = SERVICE_INNER_WIDTH - SERVICE_PRICE_WIDTH
    parts_table = Table(part_rows, colWidths=[parts_name_width, SERVICE_PRICE_WIDTH])
    parts_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), PALETTE["paper"]),
                ("BACKGROUND", (0, -1), (-1, -1), PALETTE["primary_soft"]),
                ("BOX", (0, 0), (-1, -1), 0.5, PALETTE["soft_line"]),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, PALETTE["soft_line"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    header = Table(
        [
            [
                _p(f"{index:02d}", "center_white"),
                [_p(service.service_type, "body_bold"), _p(f"{_date(service.date)} | {service.workshop_name or 'Nie podano warsztatu'}", "small")],
                _p(_money(service.cost), "right"),
            ]
        ],
        colWidths=[
            SERVICE_NUMBER_WIDTH,
            SERVICE_CARD_WIDTH - SERVICE_NUMBER_WIDTH - SERVICE_PRICE_WIDTH,
            SERVICE_PRICE_WIDTH,
        ],
        rowHeights=[17 * mm],
    )
    header.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALETTE["primary_soft"]),
                ("BACKGROUND", (0, 0), (0, 0), PALETTE["primary"]),
                ("BOX", (0, 0), (-1, -1), 0, PALETTE["primary_soft"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TEXTCOLOR", (2, 0), (2, 0), PALETTE["danger"]),
            ]
        )
    )

    body = Table(
        [
            [_p("Zakres prac", "label")],
            [_p(service.description or "Brak opisu.", "body_muted")],
            [_p("Części i materiały", "label")],
            [parts_table],
        ],
        colWidths=[SERVICE_CARD_WIDTH],
    )
    body.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALETTE["white"]),
                ("LEFTPADDING", (0, 0), (-1, -1), SERVICE_CARD_PADDING),
                ("RIGHTPADDING", (0, 0), (-1, -1), SERVICE_CARD_PADDING),
                ("TOPPADDING", (0, 0), (-1, 0), 5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 1 * mm),
                ("TOPPADDING", (0, 1), (-1, 1), 0),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 5 * mm),
                ("TOPPADDING", (0, 2), (-1, 2), 0),
                ("BOTTOMPADDING", (0, 2), (-1, 2), 2 * mm),
                ("TOPPADDING", (0, 3), (-1, 3), 0),
                ("BOTTOMPADDING", (0, 3), (-1, 3), 5 * mm),
            ]
        )
    )

    card = Table([[header], [body]], colWidths=[SERVICE_CARD_WIDTH])
    card.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.7, PALETTE["soft_line"]),
                ("BACKGROUND", (0, 0), (-1, -1), PALETTE["white"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return KeepTogether([card, Spacer(1, 5 * mm)])


def _empty_state():
    table = Table(
        [
            [_p("Brak wpisów serwisowych", "title")],
            [_p("Dodaj pierwszy serwis w panelu samochodu, aby raport zawierał pełną historię napraw.", "subtitle")],
        ],
        colWidths=[CONTENT_WIDTH],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALETTE["white"]),
                ("BOX", (0, 0), (-1, -1), 0.7, PALETTE["soft_line"]),
                ("LEFTPADDING", (0, 0), (-1, -1), 8 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8 * mm),
                ("TOPPADDING", (0, 0), (-1, 0), 8 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 2 * mm),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 8 * mm),
            ]
        )
    )
    return table


def build_service_history_pdf(car, services):
    services = list(services)
    output = BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=MARGIN_X,
        rightMargin=MARGIN_X,
        topMargin=MARGIN_TOP + 9 * mm,
        bottomMargin=MARGIN_BOTTOM + 8 * mm,
        title=f"Książka serwisowa - {car.brand} {car.model}",
        author="Website-Finance",
    )

    story = [
        _hero(car),
        Spacer(1, 7 * mm),
        _metric_row(services),
        Spacer(1, 8 * mm),
        _section("Dane pojazdu"),
        _vehicle_details(car),
        Spacer(1, 9 * mm),
        _section("Historia napraw", "Wpisy są ułożone od najnowszego do najstarszego."),
    ]

    if services:
        for index, service in enumerate(services, start=1):
            story.append(_service_card(index, service))
    else:
        story.append(_empty_state())

    doc.build(
        story,
        onFirstPage=lambda canvas, doc: _on_page(canvas, doc, car),
        onLaterPages=lambda canvas, doc: _on_page(canvas, doc, car),
    )
    return output.getvalue()
