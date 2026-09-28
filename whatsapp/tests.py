import hashlib
import hmac
import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from booking.models import Appointment, Client, OpeningHours, Service, Stylist

from . import client as wa
from .models import Conversation, Message

PHONE = "919812345678"


def payload(text=None, reply_id=None, title="", msg_id=None, name="Asha"):
    if reply_id:
        msg = {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": reply_id, "title": title}}}
    else:
        msg = {"type": "text", "text": {"body": text}}
    msg.update({"from": PHONE, "id": msg_id or f"wamid.{timezone.now().timestamp()}{text}{reply_id}"})
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"contacts": [{"wa_id": PHONE, "profile": {"name": name}}], "messages": [msg]}}]}],
    }


class WebhookTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        for weekday in range(7):
            OpeningHours.objects.create(weekday=weekday, opens=time(10), closes=time(12))
        cls.cut = Service.objects.create(name="Haircut", duration_minutes=30, price=Decimal("300"))
        cls.priya = Stylist.objects.create(name="Priya")
        cls.rahul = Stylist.objects.create(name="Rahul")
        cls.priya.services.set([cls.cut])
        cls.rahul.services.set([cls.cut])

    def post(self, body, **headers):
        return self.client.post(
            reverse("whatsapp_webhook"), data=json.dumps(body), content_type="application/json", **headers
        )

    def last_reply(self):
        return Message.objects.filter(direction=Message.OUTBOUND).last().body

    def say(self, text=None, reply_id=None, **kw):
        response = self.post(payload(text=text, reply_id=reply_id, **kw))
        self.assertEqual(response.status_code, 200)
        return self.last_reply()


class VerificationTests(WebhookTestCase):
    def test_handshake(self):
        url = reverse("whatsapp_webhook")
        ok = self.client.get(url, {"hub.mode": "subscribe", "hub.verify_token": "change-me", "hub.challenge": "42"})
        self.assertEqual(ok.content, b"42")
        bad = self.client.get(url, {"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
        self.assertEqual(bad.status_code, 403)

    @override_settings(WHATSAPP_APP_SECRET="s3cret")
    def test_signature_is_checked(self):
        body = json.dumps(payload("hi")).encode()
        self.assertEqual(self.post(payload("hi")).status_code, 403)
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        response = self.client.post(
            reverse("whatsapp_webhook"), data=body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sig
        )
        self.assertEqual(response.status_code, 200)

    def test_status_updates_are_recorded(self):
        client = Client.objects.create(phone=PHONE)
        Message.objects.create(client=client, direction=Message.OUTBOUND, body="x", wa_message_id="wamid.1")
        self.post({"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.1", "status": "read"}]}}]}]})
        self.assertEqual(Message.objects.get(wa_message_id="wamid.1").status, "read")


class BotFlowTests(WebhookTestCase):
    def test_greeting_creates_client_from_profile(self):
        reply = self.say("Hi")
        client = Client.objects.get(phone=PHONE)
        self.assertEqual(client.name, "Asha")
        self.assertIn("Welcome", reply)
        self.assertIn("Book appointment", reply)

    def test_full_booking(self):
        self.say("hi")
        self.assertIn("Haircut", self.say(reply_id="book:"))
        self.assertIn("No preference", self.say(reply_id=f"svc:{self.cut.pk}"))
        day = timezone.localdate() + timedelta(days=1)
        self.assertIn(f"{day:%a %d %b}", self.say(reply_id="sty:any"))
        reply = self.say(reply_id=f"day:{day.isoformat()}")
        self.assertIn("10:00 AM", reply)
        self.say(reply_id=f"slot:{day.isoformat()}T10:00|{self.priya.pk}")
        self.assertIn("Please confirm", Message.objects.filter(direction="out").last().body)
        self.assertIn("You're booked", self.say(reply_id="confirm:yes"))

        appt = Appointment.objects.get()
        self.assertEqual(appt.source, Appointment.SOURCE_WHATSAPP)
        self.assertEqual(appt.stylist, self.priya)
        self.assertEqual(timezone.localtime(appt.start).hour, 10)
        self.assertEqual(Conversation.objects.get().step, "idle")

    def test_slot_taken_meanwhile(self):
        day = timezone.localdate() + timedelta(days=1)
        self.say("hi")
        self.say(reply_id=f"svc:{self.cut.pk}")
        self.say(reply_id=f"sty:{self.priya.pk}")
        self.say(reply_id=f"day:{day.isoformat()}")
        self.say(reply_id=f"slot:{day.isoformat()}T10:00|{self.priya.pk}")
        other = Client.objects.create(phone="911111111111")
        Appointment.objects.create(
            client=other, service=self.cut, stylist=self.priya, start=timezone.make_aware(datetime.combine(day, time(10)))
        )
        reply = self.say(reply_id="confirm:yes")
        self.assertNotIn("10:00 AM", reply)
        self.assertEqual(Appointment.objects.count(), 1)

    def test_asks_for_name_when_unknown(self):
        self.say("hi", name="")
        self.assertIn("your name", self.say(reply_id="book:", name=""))
        self.assertIn("Haircut", self.say("Kiran", name=""))
        self.assertEqual(Client.objects.get().name, "Kiran")

    def test_view_and_cancel(self):
        client = Client.objects.create(phone=PHONE, name="Asha")
        appt = Appointment.objects.create(
            client=client, service=self.cut, stylist=self.priya, start=timezone.now() + timedelta(days=1)
        )
        self.assertIn("Haircut", self.say(reply_id="mine:"))
        self.assertIn("Cancel it", self.say(reply_id=f"appt:{appt.pk}"))
        self.assertIn("cancelled", self.say(reply_id=f"cancel:{appt.pk}"))
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.CANCELLED)

    def test_cannot_cancel_someone_elses_appointment(self):
        other = Client.objects.create(phone="911111111111")
        appt = Appointment.objects.create(
            client=other, service=self.cut, stylist=self.priya, start=timezone.now() + timedelta(days=1)
        )
        self.say("hi")
        self.say(reply_id=f"cancel:{appt.pk}")
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.BOOKED)

    def test_talk_to_staff_silences_bot_until_menu(self):
        self.say("hi")
        self.assertIn("team member", self.say(reply_id="staff:"))
        before = Message.objects.filter(direction="out").count()
        self.say("Do you do bridal makeup?")
        self.assertEqual(Message.objects.filter(direction="out").count(), before)
        self.assertTrue(Conversation.objects.get().needs_staff)
        self.assertIn("Welcome", self.say("menu"))
        self.assertFalse(Conversation.objects.get().needs_staff)

    def test_unknown_text_shows_menu(self):
        self.assertIn("didn't catch that", self.say("what?"))

    def test_duplicate_delivery_is_ignored(self):
        self.say("hi", msg_id="wamid.same")
        count = Message.objects.count()
        self.say("hi", msg_id="wamid.same")
        self.assertEqual(Message.objects.count(), count)

    def test_info(self):
        self.say("hi")
        self.say(reply_id="info:")
        info = Message.objects.filter(direction="out").order_by("-pk")[1].body
        self.assertIn("Haircut", info)
        self.assertIn("Monday", info)


@override_settings(WHATSAPP_TOKEN="tok", WHATSAPP_PHONE_NUMBER_ID="123")
class ApiClientTests(TestCase):
    def test_list_message_payload(self):
        client = Client.objects.create(phone=PHONE)
        response = mock.Mock()
        response.json.return_value = {"messages": [{"id": "wamid.abc"}]}
        with mock.patch("whatsapp.client.requests.post", return_value=response) as post:
            msg = wa.send_list(client, "Pick", "Menu", [("a", "A very long row title that is clipped", "desc")])
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["to"], PHONE)
        row = sent["interactive"]["action"]["sections"][0]["rows"][0]
        self.assertLessEqual(len(row["title"]), wa.ROW_TITLE_LEN)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertEqual((msg.status, msg.wa_message_id), ("sent", "wamid.abc"))

    def test_failure_is_recorded(self):
        import requests

        client = Client.objects.create(phone=PHONE)
        with mock.patch("whatsapp.client.requests.post", side_effect=requests.ConnectionError("down")), \
                self.assertLogs("whatsapp", "ERROR"):
            msg = wa.send_text(client, "hello")
        self.assertEqual(msg.status, "failed")
