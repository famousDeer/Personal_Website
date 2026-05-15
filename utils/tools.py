from decimal import Decimal, InvalidOperation
from datetime import datetime
import re 

def month_start(date):
    """Returns the first day of
    the month for a given date."""
    return date.replace(day=1)

def parse_decimal(value: str) -> Decimal:
    """Parses a string to a Decimal
    and handles invalid inputs."""
    if value is None:
        raise ValueError("Missing decimal value")

    normalized = str(value).strip().replace(',', '.').replace(' ', '').replace('\xa0', '')
    is_negative = normalized.startswith('-')
    cleaned = re.sub(r'[^0-9.]', '', normalized)
    if cleaned.count('.') > 1:
        dot_idx = cleaned.find('.')
        cleaned = cleaned[:dot_idx + 1] + cleaned[dot_idx + 1:].replace('.', '')
    if not cleaned:
        raise ValueError(f"Invalid decimal values: {value}")

    clear_value = f"-{cleaned}" if is_negative else cleaned
    try:
        return Decimal(clear_value)
    except (ValueError, InvalidOperation):
        raise ValueError(f"Invalid decimal values: {value}")


def parse_date_input(value: str):
    """Parses supported UI date formats to a date object."""
    if value is None:
        raise ValueError("Missing date value")

    normalized = value.strip()
    for date_format in ('%Y-%m-%d', '%d.%m.%Y'):
        try:
            return datetime.strptime(normalized, date_format).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date value: {value}")
