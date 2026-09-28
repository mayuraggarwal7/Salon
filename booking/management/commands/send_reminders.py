from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from booking.models import Appointment
from whatsapp import notifications


class Command(BaseCommand):
    help = "Send WhatsApp reminders for upcoming appointments. Run it from cron, e.g. every 15 minutes."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=settings.REMINDER_LEAD_HOURS)

    def handle(self, *args, hours, **options):
        now = timezone.now()
        due = Appointment.objects.filter(
            status__in=Appointment.ACTIVE_STATUSES,
            reminder_sent=False,
            start__gt=now,
            start__lte=now + timedelta(hours=hours),
        ).select_related("client", "service", "stylist")
        sent = 0
        for appt in due:
            message = notifications.send_reminder(appt)
            if message.status != "failed":
                appt.reminder_sent = True
                appt.save(update_fields=["reminder_sent"])
                sent += 1
        self.stdout.write(f"Sent {sent} reminder(s).")
