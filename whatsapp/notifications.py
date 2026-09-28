"""Messages the salon sends about appointments, used by both the bot and the dashboard."""
from django.conf import settings
from django.utils import timezone

from booking.utils import money

from . import client as wa


def when(appt):
    return f"{timezone.localtime(appt.start):%a %d %b, %I:%M %p}"


def appointment_summary(appt):
    lines = [f"💇 {appt.service.name}", f"🗓 {when(appt)}"]
    if appt.stylist:
        lines.append(f"👤 with {appt.stylist.name}")
    lines.append(f"💰 {money(appt.service.price)}")
    return "\n".join(lines)


def send_confirmation(appt):
    return wa.send_text(
        appt.client,
        f"✅ You're booked at {settings.SALON_NAME}!\n\n{appointment_summary(appt)}\n\n"
        "Reply *menu* any time to view or cancel your appointments.",
    )


def send_cancellation(appt):
    return wa.send_text(
        appt.client,
        f"❌ Your appointment at {settings.SALON_NAME} has been cancelled:\n\n"
        f"{appointment_summary(appt)}\n\nReply *menu* to book a new time.",
    )


def send_reminder(appt):
    text = (
        f"⏰ Reminder from {settings.SALON_NAME}\n\n{appointment_summary(appt)}\n\n"
        "Reply *menu* if you need to cancel."
    )
    if settings.WHATSAPP_REMINDER_TEMPLATE:
        return wa.send_template(
            appt.client,
            settings.WHATSAPP_REMINDER_TEMPLATE,
            [appt.client.name or "there", appt.service.name, when(appt)],
            fallback_text=text,
        )
    return wa.send_text(appt.client, text)
