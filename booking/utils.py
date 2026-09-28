from django.conf import settings


def money(amount):
    text = f"{amount:,.2f}"
    return f"{settings.SALON_CURRENCY}{text[:-3] if text.endswith('.00') else text}"
