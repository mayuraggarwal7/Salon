import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from booking.models import Client, Stylist, normalize_phone

from . import bot, routing
from .models import Message

logger = logging.getLogger("whatsapp")


def _signature_ok(request):
    if not settings.WHATSAPP_APP_SECRET:
        return True
    expected = "sha256=" + hmac.new(
        settings.WHATSAPP_APP_SECRET.encode(), request.body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, request.headers.get("X-Hub-Signature-256", ""))


def _extract(msg):
    """Return (text shown in the inbox, typed text, tapped reply id) for an inbound message."""
    kind = msg.get("type")
    if kind == "text":
        text = msg["text"].get("body", "")
        return text, text, ""
    if kind == "interactive":
        interactive = msg["interactive"]
        reply = interactive.get(interactive.get("type"), {})
        return reply.get("title", ""), "", reply.get("id", "")
    if kind == "button":  # quick-reply button on a template message
        text = msg["button"].get("text", "")
        return text, text, msg["button"].get("payload", "")
    return f"[{kind} message]", "", ""


def _handle_inbound(msg, profile_names):
    phone = normalize_phone(msg.get("from"))
    if not phone:
        return
    stylist = Stylist.objects.filter(phone=phone, active=True, relay_whatsapp=True).first()
    if stylist is not None:
        _, text, _ = _extract(msg)
        routing.handle_staff_message(stylist, text, msg.get("context", {}).get("id", ""), msg.get("id"))
        return

    client, created = Client.objects.get_or_create(phone=phone)
    if created and profile_names.get(phone):
        client.name = profile_names[phone][:100]
        client.save(update_fields=["name"])

    shown, text, reply_id = _extract(msg)
    try:
        with transaction.atomic():
            message = Message.objects.create(
                client=client,
                direction=Message.INBOUND,
                body=shown,
                wa_message_id=msg.get("id"),
                status="received",
            )
    except IntegrityError:
        return  # WhatsApp retried a message we've already handled.

    bot.handle_message(client, text=text, reply_id=reply_id, message=message)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def webhook(request):
    if request.method == "GET":
        # Meta's one-off subscription handshake.
        if (
            request.GET.get("hub.mode") == "subscribe"
            and request.GET.get("hub.verify_token") == settings.WHATSAPP_VERIFY_TOKEN
        ):
            return HttpResponse(request.GET.get("hub.challenge", ""))
        return HttpResponseForbidden("Verification failed")

    if not _signature_ok(request):
        return HttpResponseForbidden("Bad signature")
    try:
        payload = json.loads(request.body)
    except ValueError:
        return HttpResponseBadRequest("Invalid JSON")

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {
                normalize_phone(c.get("wa_id")): c.get("profile", {}).get("name", "")
                for c in value.get("contacts", [])
            }
            for msg in value.get("messages", []):
                try:
                    _handle_inbound(msg, names)
                except Exception:
                    # Always 200 so WhatsApp doesn't keep retrying a message that crashes us.
                    logger.exception("Failed to handle WhatsApp message %s", msg.get("id"))
            for status in value.get("statuses", []):
                Message.objects.filter(wa_message_id=status.get("id")).update(status=status.get("status", ""))
    return HttpResponse("OK")
