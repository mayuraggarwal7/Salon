"""
Turns a client's past invoices into prompts for staff: what they usually pay,
which services they are due for, and what to cross-sell or upsell.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from .models import Appointment, InvoiceLine, Service
from .utils import money

# Needs this many past purchases before we trust an inferred rebooking interval.
MIN_VISITS_FOR_INTERVAL = 2
# Flag services that fall due within this many days.
DUE_SOON_DAYS = 7
# Services must appear together on at least this many invoices to count as a pair.
MIN_PAIR_COUNT = 2


@dataclass
class ServiceHistory:
    service: Service
    times: int
    last_date: object
    last_price: Decimal
    average_price: Decimal
    interval_days: int | None

    @property
    def price_differs(self):
        return self.last_price != self.service.price

    @property
    def due_date(self):
        return self.last_date + timedelta(days=self.interval_days) if self.interval_days else None


@dataclass
class Suggestion:
    service: Service
    kind: str  # "due", "cross_sell" or "upsell"
    reason: str

    @property
    def label(self):
        return {"due": "Due", "cross_sell": "Cross-sell", "upsell": "Upsell"}[self.kind]


def service_history(client):
    """Per service the client has paid for: how often, when last, and at what price."""
    lines = (
        InvoiceLine.objects.filter(invoice__client=client, service__isnull=False)
        .select_related("service__upgrade", "invoice")
        .order_by("invoice__date", "pk")
    )
    by_service = defaultdict(list)
    for line in lines:
        by_service[line.service].append(line)

    history = []
    for service, service_lines in by_service.items():
        dates = sorted({line.invoice.date for line in service_lines})
        interval = service.rebook_after_days
        if len(dates) >= MIN_VISITS_FOR_INTERVAL:
            gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
            interval = round(sum(gaps) / len(gaps)) or interval
        last = service_lines[-1]
        prices = [line.unit_price for line in service_lines]
        history.append(
            ServiceHistory(
                service=service,
                times=len(dates),
                last_date=last.invoice.date,
                last_price=last.unit_price,
                average_price=(sum(prices) / len(prices)).quantize(Decimal("0.01")),
                interval_days=interval,
            )
        )
    return sorted(history, key=lambda h: h.last_date, reverse=True)


def client_summary(client):
    totals = client.invoices.aggregate(spend=Sum("total"), visits=Count("pk"))
    spend = totals["spend"] or Decimal(0)
    visits = totals["visits"] or 0
    last = client.invoices.order_by("-date").values_list("date", flat=True).first()
    return {
        "spend": spend,
        "visits": visits,
        "average_ticket": (spend / visits).quantize(Decimal("0.01")) if visits else Decimal(0),
        "last_visit": last,
    }


def frequently_paired(service, limit=3):
    """Other services that most often appear on the same invoice as `service`, salon-wide."""
    invoice_ids = InvoiceLine.objects.filter(service=service).values("invoice_id")
    pairs = (
        InvoiceLine.objects.filter(invoice_id__in=invoice_ids, service__isnull=False, service__active=True)
        .exclude(service=service)
        .values("service")
        .annotate(n=Count("invoice", distinct=True))
        .filter(n__gte=MIN_PAIR_COUNT)
        .order_by("-n")[:limit]
    )
    counts = {row["service"]: row["n"] for row in pairs}
    services = Service.objects.in_bulk(list(counts))
    return [(services[pk], n) for pk, n in counts.items()]


def charge_prompt(client, service, history=None):
    """What the staff should know before charging this client for `service`."""
    history = history if history is not None else service_history(client)
    for h in history:
        if h.service == service:
            if h.last_price < service.price:
                return (
                    f"Last charged {money(h.last_price)} on {h.last_date:%d %b %Y}; list price is now "
                    f"{money(service.price)}. Mention the new price."
                )
            if h.last_price > service.price:
                return f"Last charged {money(h.last_price)} on {h.last_date:%d %b %Y}, above today's list price {money(service.price)}."
            return f"Charged {money(h.last_price)} last time ({h.times} visit{'s' if h.times != 1 else ''})."
    return "First time for this service."


def suggestions(client, current_services=(), limit=5, history=None, today=None):
    """Services to offer this client, most relevant first."""
    today = today or timezone.localdate()
    history = history if history is not None else service_history(client)
    current = set(current_services)
    upcoming = set(
        client.appointments.filter(
            start__gte=timezone.now(), status__in=Appointment.ACTIVE_STATUSES
        ).values_list("service_id", flat=True)
    )
    skip = {s.pk for s in current} | upcoming
    recent = {h.service.pk for h in history if (today - h.last_date).days < 30}
    out, seen = [], set()

    def add(service, kind, reason):
        if service.pk in skip or service.pk in seen or not service.active:
            return
        seen.add(service.pk)
        out.append(Suggestion(service, kind, reason))

    # 1. Things they buy regularly and are due for.
    for h in sorted(history, key=lambda h: h.due_date or today + timedelta(days=9999)):
        if h.due_date and h.due_date <= today + timedelta(days=DUE_SOON_DAYS):
            days = (today - h.last_date).days
            add(h.service, "due", f"Last done {days} days ago; usually every {h.interval_days} days.")

    # 2. Add-ons that go with what they're having today.
    for service in current:
        for related in service.related_services.filter(active=True):
            if related.pk not in recent:
                add(related, "cross_sell", f"Goes well with {service.name}.")
        for paired, n in frequently_paired(service):
            if paired.pk not in recent:
                add(paired, "cross_sell", f"Booked together with {service.name} on {n} past invoices.")

    # 3. Premium upgrades of what they're having today or usually have.
    for service in list(current) + [h.service for h in history]:
        upgrade = service.upgrade
        if upgrade and upgrade.active and upgrade.pk != service.pk:
            add(upgrade, "upsell", f"Upgrade from {service.name} (+{money(upgrade.price - service.price)}).")

    return out[:limit]


def appointment_prompts(appointments):
    """Attach `charge_hint` and `suggestions` to each appointment, for schedule views."""
    cache = {}
    for appt in appointments:
        if appt.client_id not in cache:
            cache[appt.client_id] = service_history(appt.client)
        history = cache[appt.client_id]
        appt.charge_hint = charge_prompt(appt.client, appt.service, history)
        appt.suggestions = suggestions(appt.client, [appt.service], limit=2, history=history)
    return appointments
