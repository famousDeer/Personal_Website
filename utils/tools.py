from decimal import Decimal, InvalidOperation
import re 

def month_start(date):
    """Returns the first day of
    the month for a given date."""
    return date.replace(day=1)

def parse_decimal(value: str) -> Decimal:
    """Parses a string to a Decimal
    and handles invalid inputs."""
    normalized = value.replace(',', '.')
    cleaned = re.sub(r'[^0-9.]', '', normalized)
    if cleaned.count('.') > 1:
        dot_idx = cleaned.find('.')
        cleaned = cleaned[:dot_idx + 1] + cleaned[dot_idx + 1:].replace('.', '')
    clear_value = cleaned
    try:
        return Decimal(clear_value)
    except (ValueError, InvalidOperation):
        raise ValueError(f"Invalid decimal values: {value}")