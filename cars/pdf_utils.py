from io import BytesIO

from PIL import Image, ImageDraw, ImageFont


PAGE_WIDTH = 1240
PAGE_HEIGHT = 1754
MARGIN_X = 90
MARGIN_TOP = 100
MARGIN_BOTTOM = 110
CONTENT_WIDTH = PAGE_WIDTH - (MARGIN_X * 2)

FONT_PATHS = {
    False: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
    True: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ],
}


def _load_font(size, bold=False):
    for path in FONT_PATHS[bold]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _line_height(draw, font):
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    return (bbox[3] - bbox[1]) + 8


def _wrap_text(draw, text, font):
    wrapped_lines = []
    for paragraph in str(text).splitlines() or [""]:
        words = paragraph.split()
        if not words:
            wrapped_lines.append("")
            continue

        current_line = words[0]
        for word in words[1:]:
            trial_line = f"{current_line} {word}"
            trial_width = draw.textbbox((0, 0), trial_line, font=font)[2]
            if trial_width <= CONTENT_WIDTH:
                current_line = trial_line
            else:
                wrapped_lines.append(current_line)
                current_line = word
        wrapped_lines.append(current_line)
    return wrapped_lines


def build_service_history_pdf(car, services):
    fonts = {
        "title": _load_font(48, bold=True),
        "subtitle": _load_font(28),
        "section": _load_font(34, bold=True),
        "body": _load_font(26),
        "muted": _load_font(22),
    }

    pages = []

    def new_page():
        image = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), "white")
        draw = ImageDraw.Draw(image)
        pages.append(image)
        return image, draw, MARGIN_TOP

    _, draw, current_y = new_page()

    def ensure_space(height):
        nonlocal draw, current_y
        if current_y + height <= PAGE_HEIGHT - MARGIN_BOTTOM:
            return
        _, draw, current_y = new_page()
        header = f"Ksiazka serwisowa: {car.brand} {car.model}"
        draw.text((MARGIN_X, current_y), header, font=fonts["subtitle"], fill="#495057")
        current_y += _line_height(draw, fonts["subtitle"]) + 20

    def draw_block(text, style, fill="#212529", spacing_after=10):
        nonlocal current_y
        font = fonts[style]
        lines = _wrap_text(draw, text, font)
        line_height = _line_height(draw, font)
        ensure_space(max(line_height * max(len(lines), 1), line_height) + spacing_after)
        for line in lines:
            draw.text((MARGIN_X, current_y), line, font=font, fill=fill)
            current_y += line_height
        current_y += spacing_after

    draw_block(f"Ksiazka serwisowa - {car.brand} {car.model}", "title", spacing_after=18)
    draw_block(
        f"Rok: {car.year}   |   Paliwo: {car.fuel_type}   |   Przebieg: {car.odometer} km",
        "subtitle",
        fill="#495057",
        spacing_after=28,
    )

    if not services:
        draw_block("Brak wpisow serwisowych dla tego samochodu.", "body")
    else:
        for index, service in enumerate(services, start=1):
            draw_block(f"{index}. {service.service_type}", "section", spacing_after=12)
            draw_block(f"Data: {service.date:%d.%m.%Y}", "body", spacing_after=4)
            draw_block(
                f"Warsztat: {service.workshop_name or 'Nie podano'}",
                "body",
                spacing_after=4,
            )
            draw_block("Opis naprawy:", "body", spacing_after=2)
            draw_block(service.description, "muted", fill="#495057", spacing_after=10)

            parts = list(service.parts.all())
            if parts:
                draw_block("Wymienione czesci:", "body", spacing_after=4)
                for part in parts:
                    draw_block(
                        f"- {part.name}: {part.price:.2f} zl",
                        "muted",
                        fill="#495057",
                        spacing_after=2,
                    )
                draw_block(
                    f"Suma czesci: {service.parts_total:.2f} zl",
                    "muted",
                    fill="#6c757d",
                    spacing_after=8,
                )
            else:
                draw_block(
                    "Wymienione czesci: brak wyszczegolnionych pozycji.",
                    "muted",
                    fill="#6c757d",
                    spacing_after=8,
                )

            draw_block(
                f"Cena calkowita uslugi: {service.cost:.2f} zl",
                "body",
                spacing_after=20,
            )

    output = BytesIO()
    pages[0].save(output, format="PDF", save_all=True, append_images=pages[1:])
    return output.getvalue()
