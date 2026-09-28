from django.db import models

from booking.models import Client, Stylist


class Message(models.Model):
    INBOUND = "in"
    OUTBOUND = "out"
    DIRECTIONS = [(INBOUND, "From client"), (OUTBOUND, "To client")]

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="messages")
    direction = models.CharField(max_length=3, choices=DIRECTIONS)
    body = models.TextField()
    wa_message_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    # Delivery status reported by WhatsApp (sent/delivered/read/failed), or "dry_run".
    status = models.CharField(max_length=20, blank=True)
    sent_by_staff = models.BooleanField(default=False)
    # Which stylist wrote it (staff replies) or had it forwarded to their WhatsApp (client messages).
    staff = models.ForeignKey(Stylist, on_delete=models.SET_NULL, null=True, blank=True, related_name="messages")
    via_relay = models.BooleanField(default=False, help_text="Sent or received through the stylist's own WhatsApp")
    read_by_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.get_direction_display()} {self.client}: {self.body[:40]}"


class Conversation(models.Model):
    """Where each client is in the WhatsApp booking flow."""

    client = models.OneToOneField(Client, on_delete=models.CASCADE, related_name="conversation")
    step = models.CharField(max_length=30, default="idle")
    data = models.JSONField(default=dict, blank=True)
    # Client asked for a person; the bot stays quiet until staff hand back or client types "menu".
    needs_staff = models.BooleanField(default=False)
    # The stylist this chat is routed to; unassigned chats go to the front desk.
    assigned_to = models.ForeignKey(
        Stylist, on_delete=models.SET_NULL, null=True, blank=True, related_name="conversations"
    )
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.client} ({self.step})"

    def reset(self):
        self.step = "idle"
        self.data = {}


class StaffRelay(models.Model):
    """A client message forwarded to a stylist's WhatsApp, so a swipe-reply can be routed back."""

    stylist = models.ForeignKey(Stylist, on_delete=models.CASCADE, related_name="relays")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="relays")
    wa_message_id = models.CharField(max_length=128, blank=True, db_index=True)
    # The stylist's own message this relay was created for, so webhook retries aren't sent twice.
    inbound_wa_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
