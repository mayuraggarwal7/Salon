"""
Routes client chats to their anchor stylist and relays them through the stylist's own WhatsApp.

Everything goes through the salon number, so the whole conversation stays in one
thread per client, whether staff reply from the web inbox or from their phone:

    client ──▶ salon number ──▶ (relay) stylist's WhatsApp
    client ◀── salon number ◀── stylist swipe-replies to the forwarded message
"""
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from booking.models import Client

from . import client as wa
from .models import Conversation, Message, StaffRelay

STAFF_HELP = (
    "Reply to a forwarded message (swipe right on it) to answer that client.\n"
    "Messages you don't reply to go to the client you were last sent.\n"
    "#done – hand the client back to the booking bot\n"
    "#who – show which client you're talking to"
)
PREVIEW_LEN = 200


def anchor_for(client):
    stylist = client.anchor_stylist
    return stylist if stylist and stylist.active else None


def assign(conv, stylist=None):
    """Route the chat to `stylist`, or the client's anchor stylist, or leave it for the front desk."""
    conv.assigned_to = stylist or anchor_for(conv.client)
    conv.needs_staff = True
    conv.save()
    return conv.assigned_to


def _window_open(stylist):
    return stylist.last_inbound_at and timezone.now() - stylist.last_inbound_at < timedelta(hours=24)


def relay_to_staff(conv, text, message=None):
    """Forward a client's message to the assigned stylist's own WhatsApp, if they've opted in."""
    stylist = conv.assigned_to
    if not (stylist and stylist.relay_whatsapp and stylist.phone):
        return None
    name = conv.client.name or f"+{conv.client.phone}"
    preview = text if len(text) <= PREVIEW_LEN else text[: PREVIEW_LEN - 1] + "…"
    if _window_open(stylist) or not settings.WHATSAPP_STAFF_ALERT_TEMPLATE:
        payload = {
            "type": "text",
            "text": {"body": f"💬 *{name}* (+{conv.client.phone}):\n{text}\n\n↩️ Swipe-reply to answer · #help"},
        }
    else:
        # Outside the 24h window only an approved template gets through.
        payload = wa.template_payload(settings.WHATSAPP_STAFF_ALERT_TEMPLATE, [name, preview])
    wa_id = wa.send_to_number(stylist.phone, payload)
    if wa_id is None:
        return None
    if message is not None:
        message.staff = stylist
        message.via_relay = True
        message.save(update_fields=["staff", "via_relay"])
    return StaffRelay.objects.create(stylist=stylist, client=conv.client, wa_message_id=wa_id)


def _staff_reply(stylist, body):
    wa.send_to_number(stylist.phone, {"type": "text", "text": {"body": body}})


def handle_staff_message(stylist, text, context_id="", inbound_id=None):
    """A stylist wrote to the salon number from their own WhatsApp."""
    if inbound_id and StaffRelay.objects.filter(inbound_wa_id=inbound_id).exists():
        return None  # WhatsApp retried a message we've already passed on.
    stylist.last_inbound_at = timezone.now()
    stylist.save(update_fields=["last_inbound_at"])
    relays = StaffRelay.objects.filter(stylist=stylist).select_related("client")
    relay = (relays.filter(wa_message_id=context_id).first() if context_id else None) or relays.first()
    command = text.strip().lower()

    if command in {"#help", "help"}:
        return _staff_reply(stylist, STAFF_HELP)
    if relay is None:
        return _staff_reply(stylist, "No client chats have been forwarded to you yet.\n\n" + STAFF_HELP)

    client = relay.client
    if command == "#who":
        return _staff_reply(stylist, f"You're talking to {client} (+{client.phone}).")
    conv, _ = Conversation.objects.get_or_create(client=client)
    if command == "#done":
        conv.needs_staff = False
        conv.reset()
        conv.save()
        return _staff_reply(stylist, f"✅ {client} is back with the booking bot.")
    if not text.strip():
        return None

    if conv.assigned_to_id != stylist.pk or not conv.needs_staff:
        conv.assigned_to = stylist
        conv.needs_staff = True
        conv.save()
    message = wa.send_text(client, text, sent_by_staff=True, staff=stylist, via_relay=True)
    if message.status == "failed":
        _staff_reply(stylist, f"⚠️ Couldn't deliver that to {client}. It has been more than 24h since they messaged.")
    # Point future un-quoted replies at this client.
    StaffRelay.objects.create(
        stylist=stylist, client=client, wa_message_id=message.wa_message_id or "", inbound_wa_id=inbound_id or None
    )
    return message


def clients_visible_to(user):
    """Managers see every client; a stylist sees their own clients plus unassigned chats."""
    stylist = getattr(user, "stylist", None)
    if is_manager(user) or stylist is None:
        return Client.objects.all()
    return Client.objects.filter(
        Q(anchor_stylist=stylist)
        | Q(conversation__assigned_to=stylist)
        | Q(conversation__assigned_to__isnull=True, anchor_stylist__isnull=True)
        | Q(appointments__stylist=stylist)
    ).distinct()


def is_manager(user):
    return user.is_superuser or user.is_staff or not hasattr(user, "stylist")
