"""
The WhatsApp booking assistant.

Each incoming message is routed by the id of the button/list row the client tapped
(`reply_id`) or, for typed text, by the step stored on their Conversation.
"""
from datetime import date, datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from booking.availability import available_slots, is_slot_free, open_days, opening_window
from booking.models import Appointment, OpeningHours, Service, Stylist

from . import client as wa
from . import notifications, routing
from .models import Conversation

MENU_WORDS = {"hi", "hello", "hey", "menu", "start", "help", "0", "namaste"}
PAGE_SIZE = 9  # leaves one list row for "More…"


def handle_message(client, text="", reply_id="", message=None):
    conv, _ = Conversation.objects.get_or_create(client=client)
    conv.last_inbound_at = timezone.now()
    text = (text or "").strip()
    word = text.lower()

    if word in MENU_WORDS or reply_id == "menu:main":
        conv.needs_staff = False
        conv.reset()
        conv.save()
        return main_menu(client, greeting=True)

    if conv.needs_staff and not reply_id:
        # A person is handling this chat; stay out of the way and pass typed messages on.
        conv.save()
        routing.relay_to_staff(conv, text or (message.body if message else ""), message)
        return None

    handler = ROUTES.get(reply_id.split(":", 1)[0]) if reply_id else None
    if handler:
        arg = reply_id.split(":", 1)[1] if ":" in reply_id else ""
        return handler(conv, arg)

    if conv.step == "ask_name" and text:
        client.name = text[:100]
        client.save(update_fields=["name"])
        conv.reset()
        conv.save()
        wa.send_text(client, f"Nice to meet you, {client.name}! 😊")
        return choose_service(conv)

    conv.save()
    return main_menu(client, greeting=False)


# --- Menus ---------------------------------------------------------------------

def main_menu(client, greeting=True):
    hello = f"Hi {client.name}! " if client.name else "Hi! "
    body = (
        f"{hello}Welcome to *{settings.SALON_NAME}* 💈\nWhat would you like to do?"
        if greeting
        else "Sorry, I didn't catch that. Please pick an option below 👇"
    )
    return wa.send_list(
        client,
        body,
        "Menu",
        [
            ("book:", "Book appointment", "Pick a service, day and time"),
            ("mine:", "My appointments", "View or cancel bookings"),
            ("info:", "Services & prices", "Price list, hours and address"),
            staff_row(client),
        ],
        section_title="Main menu",
    )


def staff_row(client):
    anchor = routing.anchor_for(client)
    if anchor:
        return ("staff:", f"Message {anchor.name}", "Your stylist – ask about services & add-ons")
    return ("staff:", "Talk to the salon", "Chat with a team member")


def start_booking(conv, _arg):
    if not conv.client.name:
        conv.step = "ask_name"
        conv.data = {}
        conv.save()
        return wa.send_text(conv.client, "Great! First, what's your name?")
    return choose_service(conv)


def paged_rows(rows, offset, more_prefix):
    page = rows[offset : offset + PAGE_SIZE]
    if len(rows) > offset + PAGE_SIZE:
        page.append((f"{more_prefix}:{offset + PAGE_SIZE}", "More…", "See more options"))
    return page


def choose_service(conv, offset="0"):
    conv.step = "service"
    conv.data = {}
    conv.save()
    rows = [
        (f"svc:{s.pk}", s.name, f"{notifications.money(s.price)} · {s.duration_minutes} min")
        for s in Service.objects.filter(active=True, stylists__active=True).distinct()
    ]
    if not rows:
        return wa.send_text(conv.client, "Online booking isn't available right now. Please call us!")
    return wa.send_list(
        conv.client, "Which service would you like? ✂️", "Services", paged_rows(rows, int(offset or 0), "svcmore")
    )


def pick_service(conv, service_id):
    service = Service.objects.filter(pk=service_id, active=True).first()
    if service is None:
        return choose_service(conv)
    conv.step = "stylist"
    conv.data = {"service": service.pk}
    conv.save()
    stylists = list(Stylist.objects.filter(active=True, services=service))
    if len(stylists) == 1:
        return pick_stylist(conv, str(stylists[0].pk))
    anchor = routing.anchor_for(conv.client)
    rows = []
    if anchor in stylists:
        stylists.remove(anchor)
        rows.append((f"sty:{anchor.pk}", f"{anchor.name} ⭐", "Your stylist"))
    rows.append(("sty:any", "No preference", "First available stylist"))
    rows += [(f"sty:{s.pk}", s.name, "") for s in stylists[: wa.MAX_LIST_ROWS - len(rows)]]
    return wa.send_list(conv.client, f"*{service.name}* — who would you like? 💇", "Stylists", rows)


def pick_stylist(conv, stylist_id):
    if "service" not in conv.data:
        return choose_service(conv)
    conv.data["stylist"] = None if stylist_id == "any" else int(stylist_id)
    conv.step = "day"
    conv.save()
    days = open_days(count=7)
    if not days:
        return wa.send_text(conv.client, "We have no open days coming up. Please check back soon!")
    today = timezone.localdate()
    rows = []
    for d in days:
        label = {0: "Today", 1: "Tomorrow"}.get((d - today).days, "")
        rows.append((f"day:{d.isoformat()}", f"{d:%a %d %b}", label))
    return wa.send_list(conv.client, "Which day suits you? 🗓", "Days", rows)


def _selection(conv):
    service = Service.objects.filter(pk=conv.data.get("service")).first()
    stylist_id = conv.data.get("stylist")
    stylist = Stylist.objects.filter(pk=stylist_id).first() if stylist_id else None
    return service, stylist


def pick_day(conv, day_iso, offset=0):
    service, stylist = _selection(conv)
    if service is None:
        return choose_service(conv)
    try:
        day = date.fromisoformat(day_iso)
    except ValueError:
        return pick_stylist(conv, str(conv.data.get("stylist") or "any"))
    conv.data["day"] = day.isoformat()
    conv.step = "time"
    conv.save()
    slots = available_slots(service, day, stylist)
    if not slots:
        wa.send_text(conv.client, f"Sorry, nothing free on {day:%a %d %b}. Please try another day.")
        return pick_stylist(conv, str(conv.data.get("stylist") or "any"))
    rows = [
        (
            f"slot:{timezone.localtime(start):%Y-%m-%dT%H:%M}|{s.pk}",
            f"{timezone.localtime(start):%I:%M %p}",
            f"with {s.name}",
        )
        for start, s in slots
    ]
    return wa.send_list(
        conv.client, f"Available times on {day:%a %d %b} ⏰", "Times", paged_rows(rows, offset, "slotmore")
    )


def more_slots(conv, offset):
    return pick_day(conv, conv.data.get("day", ""), int(offset or 0))


def pick_slot(conv, value):
    service, _ = _selection(conv)
    try:
        start_str, stylist_id = value.split("|")
        start = timezone.make_aware(datetime.fromisoformat(start_str))
    except ValueError:
        return choose_service(conv)
    stylist = Stylist.objects.filter(pk=stylist_id, active=True).first()
    if service is None or stylist is None:
        return choose_service(conv)
    conv.data.update({"start": start_str, "slot_stylist": stylist.pk})
    conv.step = "confirm"
    conv.save()
    preview = Appointment(client=conv.client, service=service, stylist=stylist, start=start)
    return wa.send_buttons(
        conv.client,
        f"Please confirm your booking:\n\n{notifications.appointment_summary(preview)}",
        [("confirm:yes", "Confirm ✅"), ("confirm:no", "Start over")],
    )


def confirm(conv, answer):
    if answer != "yes":
        return choose_service(conv)
    service, _ = _selection(conv)
    stylist = Stylist.objects.filter(pk=conv.data.get("slot_stylist")).first()
    if service is None or stylist is None or "start" not in conv.data:
        return choose_service(conv)
    start = timezone.make_aware(datetime.fromisoformat(conv.data["start"]))
    with transaction.atomic():
        if start <= timezone.now() or not is_slot_free(service, start, stylist):
            wa.send_text(conv.client, "Sorry, that time was just taken 😕 Here are the latest free times:")
            return pick_day(conv, conv.data.get("day", ""))
        appt = Appointment.objects.create(
            client=conv.client,
            service=service,
            stylist=stylist,
            start=start,
            source=Appointment.SOURCE_WHATSAPP,
        )
    conv.reset()
    conv.save()
    notifications.send_confirmation(appt)
    return offer_addons(conv, appt)


def addon_candidates(appt):
    return list(
        appt.service.related_services.filter(active=True, stylists=appt.stylist).exclude(pk=appt.service_id)[:2]
    )


def offer_addons(conv, appt):
    """Cross-sell: suggest services that go with what they just booked."""
    addons = addon_candidates(appt)
    if not addons:
        return None
    lines = "\n".join(
        f"• {s.name} – {notifications.money(s.price)} (+{s.duration_minutes} min)" for s in addons
    )
    who = appt.stylist.name if appt.stylist else "We"
    return wa.send_buttons(
        conv.client,
        f"✨ {who} recommends adding:\n{lines}\n\nWant to add one to your visit?",
        [(f"addon:{appt.pk}.{s.pk}", f"Add {s.name}") for s in addons] + [("addon_no:", "No thanks")],
    )


def add_addon(conv, value):
    """Book the add-on straight after the main appointment, or ask the stylist to fit it in."""
    try:
        appt_id, service_id = value.split(".")
    except ValueError:
        return main_menu(conv.client)
    appt = _own_appointment(conv, appt_id)
    service = Service.objects.filter(pk=service_id, active=True).first()
    if appt is None or service is None:
        return main_menu(conv.client)
    stylist = appt.stylist
    window = opening_window(timezone.localtime(appt.end).date())
    fits_hours = window is not None and appt.end + service.duration <= window[1]
    with transaction.atomic():
        can_book = (
            fits_hours
            and stylist
            and stylist.services.filter(pk=service.pk).exists()
            and is_slot_free(service, appt.end, stylist)
        )
        if can_book:
            addon = Appointment.objects.create(
                client=conv.client,
                service=service,
                stylist=stylist,
                start=appt.end,
                source=Appointment.SOURCE_WHATSAPP,
                notes=f"Add-on to {appt.service.name}",
            )
    if can_book:
        return notifications.send_confirmation(addon)
    appt.notes = (appt.notes + f" | Add-on requested: {service.name}").strip(" |")[:255]
    appt.save(update_fields=["notes"])
    routing.assign(conv, stylist)
    routing.relay_to_staff(
        conv, f"{conv.client} would like to add {service.name} to their {appt.service.name} on {notifications.when(appt)}."
    )
    name = stylist.name if stylist else "The team"
    return wa.send_text(
        conv.client, f"👍 Noted! {name} will confirm {service.name} with you here shortly."
    )


def decline_addon(conv, _arg):
    return wa.send_text(conv.client, "No problem! See you soon 😊")


def accept_offer(conv, service_id):
    """Client tapped "Book it" on an offer a stylist sent from the dashboard."""
    service = Service.objects.filter(pk=service_id, active=True).first()
    if service is None:
        return main_menu(conv.client)
    if not conv.client.name:
        return start_booking(conv, "")
    conv.needs_staff = False
    anchor = routing.anchor_for(conv.client)
    if anchor and anchor.services.filter(pk=service.pk).exists():
        conv.step = "stylist"
        conv.data = {"service": service.pk}
        conv.save()
        return pick_stylist(conv, str(anchor.pk))
    return pick_service(conv, str(service.pk))


def my_appointments(conv, _arg):
    conv.reset()
    conv.save()
    appts = list(conv.client.upcoming_appointments()[: wa.MAX_LIST_ROWS])
    if not appts:
        return wa.send_buttons(
            conv.client,
            "You have no upcoming appointments.",
            [("book:", "Book now"), ("menu:main", "Main menu")],
        )
    rows = [
        (f"appt:{a.pk}", f"{timezone.localtime(a.start):%a %d %b %I:%M%p}", a.service.name)
        for a in appts
    ]
    return wa.send_list(conv.client, "Your upcoming appointments. Tap one to manage it:", "Appointments", rows)


def _own_appointment(conv, appt_id):
    return conv.client.upcoming_appointments().filter(pk=appt_id).first()


def show_appointment(conv, appt_id):
    appt = _own_appointment(conv, appt_id)
    if appt is None:
        return my_appointments(conv, "")
    return wa.send_buttons(
        conv.client,
        notifications.appointment_summary(appt),
        [(f"cancel:{appt.pk}", "Cancel it"), ("menu:main", "Keep it")],
    )


def cancel_appointment(conv, appt_id):
    appt = _own_appointment(conv, appt_id)
    if appt is None:
        return my_appointments(conv, "")
    appt.status = Appointment.CANCELLED
    appt.save(update_fields=["status"])
    return notifications.send_cancellation(appt)


def salon_info(conv, _arg):
    services = "\n".join(
        f"• {s.name} – {notifications.money(s.price)} ({s.duration_minutes} min)"
        for s in Service.objects.filter(active=True)
    )
    hours = "\n".join(str(h) for h in OpeningHours.objects.all())
    parts = [f"*{settings.SALON_NAME}*"]
    if settings.SALON_ADDRESS:
        parts.append(f"📍 {settings.SALON_ADDRESS}")
    if services:
        parts.append(f"*Services*\n{services}")
    if hours:
        parts.append(f"*Opening hours*\n{hours}")
    wa.send_text(conv.client, "\n\n".join(parts))
    return wa.send_buttons(conv.client, "Anything else?", [("book:", "Book now"), ("menu:main", "Main menu")])


def talk_to_staff(conv, _arg):
    conv.reset()
    stylist = routing.assign(conv)
    who = f"{stylist.name} will" if stylist else "A team member will"
    routing.relay_to_staff(conv, f"{conv.client} would like to chat.")
    return wa.send_text(
        conv.client,
        f"👋 {who} reply here shortly. Type your question below – "
        "you can ask about any service or add-on.\n"
        "(Type *menu* to go back to the booking assistant.)",
    )


ROUTES = {
    "book": start_booking,
    "svcmore": choose_service,
    "svc": pick_service,
    "sty": pick_stylist,
    "day": pick_day,
    "slotmore": more_slots,
    "slot": pick_slot,
    "confirm": confirm,
    "mine": my_appointments,
    "appt": show_appointment,
    "cancel": cancel_appointment,
    "info": salon_info,
    "staff": talk_to_staff,
    "addon": add_addon,
    "addon_no": decline_addon,
    "offer": accept_offer,
}
