from django import template

register = template.Library()

@register.filter
def days_between(start_date, end_date):
    if not start_date or not end_date:
        return ""
    delta = (end_date - start_date).days + 1
    return f"{delta} dni"
