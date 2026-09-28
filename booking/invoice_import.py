"""Import historic invoices from a CSV export of the old billing system (one row per invoice line)."""
import csv
import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count

from .models import Client, Invoice, InvoiceLine, Service, Stylist, normalize_phone

# Accepted header names for each column (compared lower-case, ignoring spaces/underscores).
COLUMNS = {
    "date": ["date", "invoicedate", "billdate", "visitdate"],
    "number": ["invoice", "invoiceno", "invoicenumber", "billno", "billnumber", "number"],
    "phone": ["phone", "mobile", "whatsapp", "clientphone", "customerphone", "contact"],
    "name": ["name", "client", "clientname", "customer", "customername"],
    "service": ["service", "item", "description", "servicename"],
    "price": ["price", "amount", "rate", "unitprice", "total"],
    "quantity": ["quantity", "qty"],
    "stylist": ["stylist", "staff", "employee", "therapist", "servedby"],
}
REQUIRED = ["date", "phone", "service", "price"]
DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d-%b-%Y", "%d/%m/%y", "%m/%d/%Y"]
SAMPLE_CSV = (
    "date,invoice,phone,name,service,price,quantity,stylist\n"
    "2026-03-02,INV-101,919812345678,Asha,Haircut,250,1,Priya\n"
    "2026-03-02,INV-101,919812345678,Asha,Hair spa,600,1,Priya\n"
)


@dataclass
class ImportResult:
    invoices: int = 0
    lines: int = 0
    clients_created: int = 0
    duplicates: int = 0
    anchors_set: int = 0
    errors: list = field(default_factory=list)
    unmatched_services: Counter = field(default_factory=Counter)
    unmatched_stylists: Counter = field(default_factory=Counter)


def _key(header):
    return re.sub(r"[\s_\-.#]", "", (header or "").lower())


def _map_headers(headers):
    lookup = {_key(h): h for h in headers}
    mapping = {}
    for column, aliases in COLUMNS.items():
        for alias in aliases:
            if alias in lookup:
                mapping[column] = lookup[alias]
                break
    return mapping


def parse_date(value):
    value = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date '{value}'")


def parse_amount(value):
    cleaned = re.sub(r"[^\d.\-]", "", value or "")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"unrecognised amount '{value}'") from None


def import_invoices(uploaded, result=None):
    result = result or ImportResult()
    text = uploaded.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    mapping = _map_headers(reader.fieldnames or [])
    missing = [c for c in REQUIRED if c not in mapping]
    if missing:
        result.errors.append((0, f"Missing column(s): {', '.join(missing)}"))
        return result

    services = {s.name.lower(): s for s in Service.objects.all()}
    stylists = {s.name.lower(): s for s in Stylist.objects.all()}
    groups = defaultdict(list)
    for row_no, row in enumerate(reader, start=2):
        get = lambda col: (row.get(mapping[col]) or "").strip() if col in mapping else ""
        try:
            phone = normalize_phone(get("phone"))
            if len(phone) < 8:
                raise ValueError(f"invalid phone '{get('phone')}'")
            day = parse_date(get("date"))
            price = parse_amount(get("price"))
            quantity = int(get("quantity") or 1)
        except ValueError as exc:
            result.errors.append((row_no, str(exc)))
            continue
        number = get("number")
        groups[(phone, number or day.isoformat())].append(
            {"date": day, "number": number, "name": get("name"), "service": get("service"),
             "price": price, "quantity": quantity, "stylist": get("stylist")}
        )

    touched = set()
    with transaction.atomic():
        for (phone, _), rows in groups.items():
            first = rows[0]
            client, created = Client.objects.get_or_create(phone=phone, defaults={"name": first["name"][:100]})
            result.clients_created += created
            if not client.name and first["name"]:
                client.name = first["name"][:100]
                client.save(update_fields=["name"])
            if first["number"] and Invoice.objects.filter(client=client, number=first["number"]).exists():
                result.duplicates += 1
                continue
            stylist = stylists.get(first["stylist"].lower())
            if first["stylist"] and stylist is None:
                result.unmatched_stylists[first["stylist"]] += 1
            invoice = Invoice.objects.create(
                client=client, stylist=stylist, number=first["number"], date=first["date"], source=Invoice.SOURCE_IMPORT
            )
            for row in rows:
                service = services.get(row["service"].lower())
                if service is None:
                    result.unmatched_services[row["service"]] += 1
                InvoiceLine.objects.create(
                    invoice=invoice,
                    service=service,
                    description=row["service"][:150],
                    quantity=row["quantity"],
                    unit_price=row["price"],
                )
                result.lines += 1
            invoice.recalculate_total()
            result.invoices += 1
            touched.add(client.pk)
        result.anchors_set = assign_missing_anchors(Client.objects.filter(pk__in=touched))
    return result


def assign_missing_anchors(clients):
    """Give clients without an anchor stylist the one who served them most often."""
    count = 0
    for client in clients.filter(anchor_stylist__isnull=True):
        top = (
            client.invoices.filter(stylist__active=True)
            .values("stylist")
            .annotate(n=Count("pk"))
            .order_by("-n")
            .first()
        )
        if top:
            client.anchor_stylist_id = top["stylist"]
            client.save(update_fields=["anchor_stylist"])
            count += 1
    return count


def link_unmatched_lines(service):
    """After a service is created or renamed, attach imported lines that used its name."""
    return InvoiceLine.objects.filter(service__isnull=True, description__iexact=service.name).update(service=service)
