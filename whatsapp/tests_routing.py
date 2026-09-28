"""Anchor stylists on WhatsApp: routing chats, relaying to staff phones, add-ons and offers."""
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from booking.models import Appointment, Client, Service

from .models import Conversation, Message, StaffRelay
from .tests import PHONE, WebhookTestCase

PRIYA_PHONE = "919700000001"


def staff_payload(text, context_id=None, msg_id=None):
    msg = {"from": PRIYA_PHONE, "id": msg_id or f"wamid.staff.{timezone.now().timestamp()}", "type": "text",
           "text": {"body": text}}
    if context_id:
        msg["context"] = {"id": context_id}
    return {"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}


class AnchorRoutingTests(WebhookTestCase):
    def setUp(self):
        self.priya.phone = PRIYA_PHONE
        self.priya.relay_whatsapp = True
        self.priya.save()
        self.asha = Client.objects.create(phone=PHONE, name="Asha", anchor_stylist=self.priya)
        # Every call to the stylist's phone gets a fresh WhatsApp id.
        self.ids = (f"wamid.fwd{i}" for i in range(100))
        patcher = mock.patch("whatsapp.client.send_to_number", side_effect=lambda phone, payload: next(self.ids))
        self.to_staff = patcher.start()
        self.addCleanup(patcher.stop)

    def staff_texts(self):
        return [c.args[1].get("text", {}).get("body", "") for c in self.to_staff.call_args_list]

    def test_menu_offers_their_stylist(self):
        self.assertIn("Message Priya", self.say("hi"))

    def test_chat_is_routed_to_anchor_and_forwarded(self):
        self.say("hi")
        self.assertIn("Priya will reply", self.say(reply_id="staff:"))
        self.say("Can I add a head massage?")
        conv = Conversation.objects.get()
        self.assertEqual((conv.assigned_to, conv.needs_staff), (self.priya, True))
        self.assertIn("Can I add a head massage?", self.staff_texts()[-1])
        inbound = Message.objects.filter(direction="in").last()
        self.assertEqual((inbound.staff, inbound.via_relay), (self.priya, True))

    def test_stylist_replies_from_their_phone_into_the_thread(self):
        self.say("hi")
        self.say(reply_id="staff:")
        self.say("Is 5pm ok?")
        self.post(staff_payload("Yes, see you at 5!", context_id="wamid.fwd1"))
        reply = Message.objects.filter(direction="out").last()
        self.assertEqual(reply.client, self.asha)
        self.assertEqual((reply.body, reply.staff, reply.via_relay), ("Yes, see you at 5!", self.priya, True))

    def test_swipe_reply_picks_the_right_client(self):
        neha = Client.objects.create(phone="919900011122", name="Neha", anchor_stylist=self.priya)
        conv = Conversation.objects.create(client=self.asha, assigned_to=self.priya, needs_staff=True)
        other = Conversation.objects.create(client=neha, assigned_to=self.priya, needs_staff=True)
        from . import routing
        asha_fwd = routing.relay_to_staff(conv, "question from Asha")
        routing.relay_to_staff(other, "question from Neha")
        self.post(staff_payload("Answer for Asha", context_id=asha_fwd.wa_message_id))
        self.assertEqual(Message.objects.get(body="Answer for Asha").client, self.asha)
        self.post(staff_payload("Unquoted goes to latest"))
        self.assertEqual(Message.objects.get(body="Unquoted goes to latest").client, self.asha)

    def test_staff_commands_and_retries(self):
        Conversation.objects.create(client=self.asha, assigned_to=self.priya, needs_staff=True)
        from . import routing
        routing.relay_to_staff(self.asha.conversation, "hello")
        self.post(staff_payload("#who"))
        self.assertIn("Asha", self.staff_texts()[-1])
        self.post(staff_payload("Hi!", msg_id="wamid.dup"))
        self.post(staff_payload("Hi!", msg_id="wamid.dup"))
        self.assertEqual(Message.objects.filter(body="Hi!").count(), 1)
        self.post(staff_payload("#done"))
        self.assertFalse(Conversation.objects.get().needs_staff)
        self.assertIn("back with the booking bot", self.staff_texts()[-1])

    @override_settings(WHATSAPP_STAFF_ALERT_TEMPLATE="client_waiting")
    def test_template_used_when_stylist_window_closed(self):
        self.priya.last_inbound_at = timezone.now() - timedelta(days=2)
        self.priya.save()
        self.say("hi")
        self.say(reply_id="staff:")
        payload = self.to_staff.call_args.args[1]
        self.assertEqual(payload["template"]["name"], "client_waiting")

    def test_button_taps_still_reach_bot_during_staff_chat(self):
        Conversation.objects.create(client=self.asha, assigned_to=self.priya, needs_staff=True)
        self.assertIn("Haircut", self.say(reply_id="book:"))


class AnchorBookingTests(WebhookTestCase):
    def setUp(self):
        self.asha = Client.objects.create(phone=PHONE, name="Asha", anchor_stylist=self.rahul)
        self.spa = Service.objects.create(name="Hair spa", duration_minutes=30, price=Decimal("600"))
        self.cut.related_services.add(self.spa)
        self.day = timezone.localdate() + timedelta(days=1)

    def book_cut(self, start):
        self.say(reply_id=f"svc:{self.cut.pk}")
        self.say(reply_id=f"sty:{self.priya.pk}")
        self.say(reply_id=f"day:{self.day.isoformat()}")
        self.say(reply_id=f"slot:{self.day.isoformat()}T{start}|{self.priya.pk}")
        return self.say(reply_id="confirm:yes")

    def test_anchor_listed_first(self):
        reply = self.say(reply_id=f"svc:{self.cut.pk}")
        self.assertTrue(reply.split("\n")[1].startswith("• Rahul ⭐"))

    def test_addon_offered_and_booked_back_to_back(self):
        self.priya.services.add(self.spa)
        self.assertIn("Add Hair spa", self.book_cut("10:00"))
        main = Appointment.objects.get()
        self.assertIn("You're booked", self.say(reply_id=f"addon:{main.pk}.{self.spa.pk}"))
        addon = Appointment.objects.get(service=self.spa)
        self.assertEqual((addon.start, addon.stylist), (main.end, self.priya))

    def test_addon_that_cannot_be_fitted_goes_to_stylist(self):
        self.priya.services.add(self.spa)
        self.book_cut("11:30")  # ends at 12:00 closing, so the spa can't follow
        main = Appointment.objects.get()
        self.assertIn("Priya will confirm", self.say(reply_id=f"addon:{main.pk}.{self.spa.pk}"))
        main.refresh_from_db()
        self.assertIn("Add-on requested: Hair spa", main.notes)
        conv = Conversation.objects.get()
        self.assertEqual((conv.assigned_to, conv.needs_staff), (self.priya, True))

    def test_addon_not_booked_over_another_client(self):
        self.priya.services.add(self.spa)
        self.book_cut("10:00")
        main = Appointment.objects.get()
        other = Client.objects.create(phone="911111111111")
        Appointment.objects.create(client=other, service=self.cut, stylist=self.priya, start=main.end)
        self.assertIn("will confirm", self.say(reply_id=f"addon:{main.pk}.{self.spa.pk}"))
        self.assertFalse(Appointment.objects.filter(service=self.spa).exists())

    def test_no_addon_offer_when_stylist_cannot_do_it(self):
        self.assertNotIn("recommends", self.book_cut("10:00"))

    def test_accepting_an_offer_jumps_to_days_with_anchor(self):
        self.rahul.services.add(self.spa)
        reply = self.say(reply_id=f"offer:{self.spa.pk}")
        self.assertIn("Which day", reply)
        conv = Conversation.objects.get()
        self.assertEqual((conv.data["service"], conv.data["stylist"]), (self.spa.pk, self.rahul.pk))
