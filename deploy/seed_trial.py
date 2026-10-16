"""Fill a fresh install with trial data so the app looks used.

Adds services, stylists and hours (seed_demo), past invoices and clients from
sample_invoices.csv, a few days of appointments, and some WhatsApp chats.
Safe to run twice: it stops if the trial data is already there.

    docker exec -i salon-web-1 python manage.py shell < deploy/seed_trial.py
"""
import hashlib
import hmac
import io
import json
from datetime import datetime, time, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import Client as HttpClient
from django.utils import timezone

from booking.invoice_import import import_invoices
from booking.models import Appointment, Client, Service, Stylist
from whatsapp.models import Conversation, Message

MARKER_PHONE = "919811100004"


def main():
    if Client.objects.filter(phone=MARKER_PHONE).exists():
        print("Trial data is already loaded; nothing to do.")
        return

    call_command("seed_demo", no_admin=True)

    csv_path = Path(settings.BASE_DIR) / "sample_invoices.csv"
    if csv_path.exists():
        r = import_invoices(io.BytesIO(csv_path.read_bytes()))
        print(f"Imported {r.invoices} invoices and {r.clients_created} clients.")

    services = {s.name: s for s in Service.objects.all()}
    stylists = {s.name: s for s in Stylist.objects.all()}

    for phone, name, notes in [
        ("919811100001", "Neha Kapoor", "Prefers morning slots"),
        ("919811100002", "Vikram Singh", ""),
        ("919811100003", "Ananya Rao", "Sensitive scalp; use mild products"),
        (MARKER_PHONE, "Karan Mehta", ""),
    ]:
        Client.objects.get_or_create(phone=phone, defaults={"name": name, "notes": notes})
    clients = list(Client.objects.all())

    tz = timezone.get_current_timezone()
    today = timezone.localdate()

    def at(day, hour, minute=0):
        return timezone.make_aware(datetime.combine(day, time(hour, minute)), tz)

    plan = [
        (-2, 11, 0, "Haircut", "Rahul", Appointment.COMPLETED, "web"),
        (-1, 15, 0, "Facial", "Meera", Appointment.COMPLETED, "whatsapp"),
        (0, 10, 0, "Haircut", "Priya", Appointment.COMPLETED, "web"),
        (0, 11, 0, "Facial", "Meera", Appointment.CONFIRMED, "whatsapp"),
        (0, 12, 0, "Beard trim", "Rahul", Appointment.BOOKED, "web"),
        (0, 13, 30, "Hair colour", "Priya", Appointment.BOOKED, "whatsapp"),
        (0, 15, 0, "Manicure", "Meera", Appointment.BOOKED, "web"),
        (0, 16, 0, "Haircut", "Rahul", Appointment.NO_SHOW, "web"),
        (0, 17, 30, "Signature cut & style", "Priya", Appointment.CANCELLED, "whatsapp"),
        (1, 11, 0, "Haircut", "Priya", Appointment.BOOKED, "whatsapp"),
        (1, 14, 0, "Hair spa", "Meera", Appointment.BOOKED, "web"),
        (2, 12, 0, "Facial", "Meera", Appointment.BOOKED, "whatsapp"),
        (3, 17, 0, "Hair colour", "Priya", Appointment.BOOKED, "web"),
    ]
    for i, (days, hour, minute, service, stylist, status, source) in enumerate(plan):
        Appointment.objects.create(
            client=clients[i % len(clients)],
            service=services[service],
            stylist=stylists[stylist],
            start=at(today + timedelta(days=days), hour, minute),
            status=status,
            source=source,
        )

    if settings.WHATSAPP_TOKEN:
        print("WhatsApp is live, so no sample chats were added (they would message real numbers).")
    else:
        _sample_chats(services, stylists, today)

    print(
        f"Done: {Client.objects.count()} clients, {Appointment.objects.count()} appointments, "
        f"{Conversation.objects.count()} WhatsApp chats."
    )


def _sample_chats(services, stylists, today):
    """Play a few conversations through the real webhook. Dry-run mode stores the replies."""
    http = HttpClient()
    host = next((h for h in settings.ALLOWED_HOSTS if h and h != "*"), "localhost")

    def say(phone, name, text=None, reply_id=None, title=""):
        if reply_id:
            msg = {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": reply_id, "title": title}}}
        else:
            msg = {"type": "text", "text": {"body": text}}
        msg.update({"from": phone, "id": f"wamid.trial.{phone}.{Message.objects.count()}"})
        body = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": phone, "profile": {"name": name}}], "messages": [msg]}}]}]})
        headers = {}
        if settings.WHATSAPP_APP_SECRET:
            digest = hmac.new(settings.WHATSAPP_APP_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
            headers["HTTP_X_HUB_SIGNATURE_256"] = "sha256=" + digest
        resp = http.post("/whatsapp/webhook/", data=body, content_type="application/json",
                         HTTP_HOST=host, secure=True, **headers)
        if resp.status_code != 200:
            raise SystemExit(f"Webhook returned {resp.status_code}")

    karan = MARKER_PHONE
    tomorrow = today + timedelta(days=1)
    rahul = stylists["Rahul"].pk
    say(karan, "Karan", "hi")
    say(karan, "Karan", reply_id="book:", title="Book appointment")
    say(karan, "Karan", reply_id=f"svc:{services['Haircut'].pk}", title="Haircut")
    say(karan, "Karan", reply_id=f"sty:{rahul}", title="Rahul")
    say(karan, "Karan", reply_id=f"day:{tomorrow.isoformat()}", title=f"{tomorrow:%a %d %b}")
    say(karan, "Karan", reply_id=f"slot:{tomorrow:%Y-%m-%d}T16:00|{rahul}", title="04:00 PM")
    say(karan, "Karan", reply_id="confirm:yes", title="Confirm")

    asha = "919812345678"
    say(asha, "Asha", "hi")
    say(asha, "Asha", reply_id="staff:", title="Talk to the salon")
    say(asha, "Asha", "Hi, can I add a hair spa after my colour today?")

    neha = "919811100001"
    say(neha, "Neha", "hello")
    say(neha, "Neha", reply_id="info:", title="Services & prices")
    say(neha, "Neha", reply_id="staff:", title="Talk to the salon")
    say(neha, "Neha", "Do you do keratin treatments?")


main()
