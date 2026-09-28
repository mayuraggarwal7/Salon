import re
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def normalize_phone(value):
    """Store phones as digits with country code, the format WhatsApp uses (e.g. 919812345678)."""
    return re.sub(r"\D", "", value or "")


class Service(models.Model):
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True)
    duration_minutes = models.PositiveIntegerField(default=30)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    active = models.BooleanField(default=True)
    related_services = models.ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        help_text="Add-ons to suggest alongside this service (cross-sell).",
    )
    upgrade = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="upgrade_from",
        verbose_name="premium upgrade",
        help_text="A premium version to upsell to, e.g. Haircut → Signature cut & style.",
    )
    rebook_after_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Typical days between visits for this service, e.g. 42 for colour. Used to flag clients who are due.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def duration(self):
        return timedelta(minutes=self.duration_minutes)


class Stylist(models.Model):
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True, help_text="WhatsApp number with country code")
    services = models.ManyToManyField(Service, related_name="stylists", blank=True)
    active = models.BooleanField(default=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="stylist"
    )
    relay_whatsapp = models.BooleanField(
        "Forward chats to my WhatsApp",
        default=False,
        help_text="Forward client chats to this stylist's own WhatsApp; their replies go back through the salon number.",
    )
    # Last time this stylist messaged the salon number (opens WhatsApp's 24h window for relays).
    last_inbound_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.phone = normalize_phone(self.phone)
        super().save(*args, **kwargs)


class OpeningHours(models.Model):
    WEEKDAYS = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS, unique=True)
    opens = models.TimeField(null=True, blank=True)
    closes = models.TimeField(null=True, blank=True)
    closed = models.BooleanField(default=False)

    class Meta:
        ordering = ["weekday"]
        verbose_name_plural = "opening hours"

    def __str__(self):
        if self.closed or not (self.opens and self.closes):
            return f"{self.get_weekday_display()}: closed"
        return f"{self.get_weekday_display()}: {self.opens:%H:%M}–{self.closes:%H:%M}"

    def clean(self):
        if not self.closed and (not self.opens or not self.closes or self.opens >= self.closes):
            raise ValidationError("Set an opening time before the closing time, or mark the day closed.")


class Client(models.Model):
    name = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, unique=True, help_text="With country code, e.g. 919812345678")
    notes = models.TextField(blank=True)
    anchor_stylist = models.ForeignKey(
        Stylist,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="anchored_clients",
        help_text="The stylist who looks after this client. Chats and bookings go to them first.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "phone"]

    def __str__(self):
        return self.name or f"+{self.phone}"

    def save(self, *args, **kwargs):
        self.phone = normalize_phone(self.phone)
        super().save(*args, **kwargs)

    def upcoming_appointments(self):
        return self.appointments.filter(
            start__gte=timezone.now(), status__in=Appointment.ACTIVE_STATUSES
        ).select_related("service", "stylist")


class Appointment(models.Model):
    BOOKED = "booked"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"
    STATUSES = [
        (BOOKED, "Booked"),
        (CONFIRMED, "Confirmed"),
        (COMPLETED, "Completed"),
        (CANCELLED, "Cancelled"),
        (NO_SHOW, "No-show"),
    ]
    ACTIVE_STATUSES = [BOOKED, CONFIRMED]

    SOURCE_WEB = "web"
    SOURCE_WHATSAPP = "whatsapp"
    SOURCES = [(SOURCE_WEB, "Front desk"), (SOURCE_WHATSAPP, "WhatsApp")]

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="appointments")
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="appointments")
    stylist = models.ForeignKey(
        Stylist, on_delete=models.SET_NULL, null=True, blank=True, related_name="appointments"
    )
    start = models.DateTimeField()
    end = models.DateTimeField(editable=False)
    status = models.CharField(max_length=20, choices=STATUSES, default=BOOKED)
    source = models.CharField(max_length=20, choices=SOURCES, default=SOURCE_WEB)
    notes = models.CharField(max_length=255, blank=True)
    reminder_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start"]

    def __str__(self):
        return f"{self.client} – {self.service} @ {timezone.localtime(self.start):%d %b %H:%M}"

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    def save(self, *args, **kwargs):
        self.end = self.start + self.service.duration
        super().save(*args, **kwargs)

    def clean(self):
        if not (self.start and self.service_id):
            return
        end = self.start + self.service.duration
        if self.stylist_id and self.is_active:
            clash = Appointment.objects.filter(
                stylist_id=self.stylist_id,
                status__in=self.ACTIVE_STATUSES,
                start__lt=end,
                end__gt=self.start,
            ).exclude(pk=self.pk)
            if clash.exists():
                raise ValidationError(f"{self.stylist} already has an appointment at that time.")


def invoice_upload_path(instance, filename):
    return f"invoices/{instance.client_id}/{filename}"


class Invoice(models.Model):
    SOURCE_IMPORT = "import"
    SOURCE_MANUAL = "manual"
    SOURCE_CHECKOUT = "checkout"
    SOURCES = [(SOURCE_IMPORT, "Imported"), (SOURCE_MANUAL, "Added by hand"), (SOURCE_CHECKOUT, "Checkout")]

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="invoices")
    stylist = models.ForeignKey(Stylist, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices")
    appointment = models.OneToOneField(
        Appointment, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoice"
    )
    number = models.CharField(max_length=50, blank=True)
    date = models.DateField()
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    file = models.FileField(upload_to=invoice_upload_path, blank=True, help_text="Scan or PDF of the original invoice")
    source = models.CharField(max_length=20, choices=SOURCES, default=SOURCE_MANUAL)
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-pk"]

    def __str__(self):
        return f"Invoice {self.number or self.pk} – {self.client} ({self.date:%d %b %Y})"

    def recalculate_total(self):
        self.total = sum(line.amount for line in self.lines.all())
        self.save(update_fields=["total"])


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    service = models.ForeignKey(Service, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoice_lines")
    description = models.CharField(max_length=150, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return self.label

    @property
    def label(self):
        return self.service.name if self.service else self.description

    @property
    def amount(self):
        return self.unit_price * self.quantity
