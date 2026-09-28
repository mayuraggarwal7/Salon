"""Thin wrapper around the WhatsApp Cloud API that also logs every message we send."""
import logging

import requests
from django.conf import settings

from .models import Message

logger = logging.getLogger("whatsapp")

# Limits imposed by WhatsApp on interactive messages.
MAX_BUTTONS = 3
MAX_LIST_ROWS = 10
BUTTON_TITLE_LEN = 20
ROW_TITLE_LEN = 24
ROW_DESCRIPTION_LEN = 72


def is_configured():
    return bool(settings.WHATSAPP_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID)


def _post(payload):
    url = (
        f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/"
        f"{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    )
    response = requests.post(
        url,
        json={"messaging_product": "whatsapp", "recipient_type": "individual", **payload},
        headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["messages"][0]["id"]


def send_to_number(phone, payload):
    """Send to any number (e.g. a stylist's phone) without logging it in a client thread.

    Returns the WhatsApp message id, "" in dry run, or None if sending failed.
    """
    if not is_configured():
        logger.info("[dry run] to +%s: %s", phone, payload)
        return ""
    try:
        return _post({"to": phone, **payload})
    except (requests.RequestException, KeyError, IndexError, ValueError):
        logger.exception("WhatsApp send to +%s failed", phone)
        return None


def _send(client, payload, log_body, sent_by_staff=False, staff=None, via_relay=False):
    message = Message.objects.create(
        client=client,
        direction=Message.OUTBOUND,
        body=log_body,
        sent_by_staff=sent_by_staff,
        staff=staff,
        via_relay=via_relay,
    )
    if not is_configured():
        logger.info("[dry run] to +%s: %s", client.phone, log_body)
        message.status = "dry_run"
    else:
        try:
            message.wa_message_id = _post({"to": client.phone, **payload})
            message.status = "sent"
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            logger.exception("WhatsApp send to +%s failed", client.phone)
            message.status = "failed"
            message.body += f"\n[send failed: {exc}]"
    message.save()
    return message


def _clip(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def send_text(client, body, sent_by_staff=False, staff=None, via_relay=False):
    return _send(client, {"type": "text", "text": {"body": body}}, body, sent_by_staff, staff, via_relay)


def send_buttons(client, body, buttons, staff=None):
    """buttons: list of (id, title), at most 3."""
    buttons = buttons[:MAX_BUTTONS]
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": bid, "title": _clip(title, BUTTON_TITLE_LEN)}}
                    for bid, title in buttons
                ]
            },
        },
    }
    log = body + "\n" + "  ".join(f"[{title}]" for _, title in buttons)
    return _send(client, payload, log, sent_by_staff=staff is not None, staff=staff)


def send_list(client, body, button_label, rows, section_title="Options"):
    """rows: list of (id, title, description), at most 10."""
    rows = rows[:MAX_LIST_ROWS]
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": _clip(button_label, BUTTON_TITLE_LEN),
                "sections": [
                    {
                        "title": _clip(section_title, ROW_TITLE_LEN),
                        "rows": [
                            {
                                "id": rid,
                                "title": _clip(title, ROW_TITLE_LEN),
                                **({"description": _clip(desc, ROW_DESCRIPTION_LEN)} if desc else {}),
                            }
                            for rid, title, desc in rows
                        ],
                    }
                ],
            },
        },
    }
    log = body + "\n" + "\n".join(f"• {title}" + (f" – {desc}" if desc else "") for _, title, desc in rows)
    return _send(client, payload, log)


def template_payload(name, params):
    return {
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": settings.WHATSAPP_TEMPLATE_LANGUAGE},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]}
            ],
        },
    }


def send_template(client, name, params, fallback_text):
    """Send an approved template (needed outside the 24h customer-service window)."""
    return _send(client, template_payload(name, params), fallback_text)
