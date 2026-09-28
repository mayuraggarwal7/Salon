from datetime import datetime, time, timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from whatsapp.models import Message

from .availability import available_slots, open_days
from .models import Appointment, Client, OpeningHours, Service, Stylist


def at(day, hour, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


class SalonTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        for weekday in range(7):
            OpeningHours.objects.create(weekday=weekday, opens=time(10), closes=time(12))
        cls.cut = Service.objects.create(name="Haircut", duration_minutes=30, price=Decimal("300"))
        cls.colour = Service.objects.create(name="Colour", duration_minutes=60, price=Decimal("1500"))
        cls.priya = Stylist.objects.create(name="Priya")
        cls.rahul = Stylist.objects.create(name="Rahul")
        cls.priya.services.set([cls.cut, cls.colour])
        cls.rahul.services.set([cls.cut])
        cls.client_obj = Client.objects.create(name="Asha", phone="+91 98123 45678")
        cls.day = timezone.localdate() + timedelta(days=2)


class AvailabilityTests(SalonTestCase):
    def test_phone_is_normalised(self):
        self.assertEqual(self.client_obj.phone, "919812345678")

    def test_all_slots_free_on_empty_day(self):
        slots = available_slots(self.colour, self.day, self.priya)
        self.assertEqual([timezone.localtime(s).time() for s, _ in slots], [time(10), time(10, 30), time(11)])

    def test_booked_time_is_excluded_for_that_stylist(self):
        Appointment.objects.create(client=self.client_obj, service=self.colour, stylist=self.priya, start=at(self.day, 10))
        slots = available_slots(self.cut, self.day, self.priya)
        self.assertEqual([timezone.localtime(s).time() for s, _ in slots], [time(11), time(11, 30)])

    def test_any_stylist_falls_back_to_whoever_is_free(self):
        Appointment.objects.create(client=self.client_obj, service=self.colour, stylist=self.priya, start=at(self.day, 10))
        slots = dict(available_slots(self.cut, self.day))
        self.assertEqual(slots[at(self.day, 10)], self.rahul)
        self.assertEqual(len(slots), 4)

    def test_cancelled_appointments_free_the_slot(self):
        Appointment.objects.create(
            client=self.client_obj, service=self.colour, stylist=self.priya, start=at(self.day, 10), status=Appointment.CANCELLED
        )
        self.assertEqual(len(available_slots(self.colour, self.day, self.priya)), 3)

    def test_closed_day_has_no_slots(self):
        OpeningHours.objects.filter(weekday=self.day.weekday()).update(closed=True)
        self.assertEqual(available_slots(self.cut, self.day), [])
        self.assertNotIn(self.day, open_days(count=7))

    def test_past_times_are_not_offered(self):
        now = at(self.day, 10, 45)
        slots = available_slots(self.cut, self.day, self.priya, now=now)
        self.assertEqual([timezone.localtime(s).time() for s, _ in slots], [time(11), time(11, 30)])


class DashboardTests(SalonTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("staff", password="pw")
        self.client.force_login(self.user)

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, f"{reverse('login')}?next=/")

    def test_pages_render(self):
        appt = Appointment.objects.create(client=self.client_obj, service=self.cut, stylist=self.priya, start=at(self.day, 10))
        Message.objects.create(client=self.client_obj, direction=Message.INBOUND, body="hello")
        for url in [
            reverse("dashboard"),
            reverse("appointment_list") + f"?date={self.day.isoformat()}",
            reverse("appointment_new"),
            reverse("appointment_edit", args=[appt.pk]),
            reverse("client_list") + "?q=Asha",
            reverse("client_detail", args=[self.client_obj.pk]),
            reverse("client_new"),
            reverse("service_list"),
            reverse("service_new"),
            reverse("stylist_list"),
            reverse("stylist_new"),
            reverse("opening_hours"),
            reverse("inbox"),
            reverse("chat", args=[self.client_obj.pk]),
            reverse("chat_messages", args=[self.client_obj.pk]),
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_create_appointment_assigns_free_stylist_and_notifies(self):
        Appointment.objects.create(client=self.client_obj, service=self.cut, stylist=self.priya, start=at(self.day, 10))
        response = self.client.post(
            reverse("appointment_new"),
            {
                "client_phone": "919900011122",
                "client_name": "Neha",
                "service": self.cut.pk,
                "stylist": "",
                "date": self.day.isoformat(),
                "time": "10:00",
                "notify": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        appt = Appointment.objects.get(client__phone="919900011122")
        self.assertEqual(appt.stylist, self.rahul)
        self.assertEqual(appt.client.name, "Neha")
        self.assertTrue(Message.objects.filter(client=appt.client, body__contains="You're booked").exists())

    def test_double_booking_is_rejected(self):
        Appointment.objects.create(client=self.client_obj, service=self.colour, stylist=self.priya, start=at(self.day, 10))
        response = self.client.post(
            reverse("appointment_new"),
            {
                "client_phone": "919900011122",
                "service": self.cut.pk,
                "stylist": self.priya.pk,
                "date": self.day.isoformat(),
                "time": "10:30",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already has an appointment")
        self.assertEqual(Appointment.objects.count(), 1)

    def test_cancel_notifies_client(self):
        appt = Appointment.objects.create(client=self.client_obj, service=self.cut, stylist=self.priya, start=at(self.day, 10))
        self.client.post(reverse("appointment_status", args=[appt.pk]), {"status": "cancelled", "notify": "1"})
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.CANCELLED)
        self.assertTrue(Message.objects.filter(client=self.client_obj, body__contains="cancelled").exists())

    def test_status_redirect_ignores_external_next(self):
        appt = Appointment.objects.create(client=self.client_obj, service=self.cut, stylist=self.priya, start=at(self.day, 10))
        response = self.client.post(
            reverse("appointment_status", args=[appt.pk]), {"status": "completed", "next": "https://evil.example"}
        )
        self.assertRedirects(response, reverse("appointment_list"))

    def test_slots_api(self):
        response = self.client.get(reverse("slots_api"), {"service": self.cut.pk, "date": self.day.isoformat()})
        self.assertEqual(len(response.json()["slots"]), 4)

    def test_staff_reply_is_logged(self):
        self.client.post(reverse("chat", args=[self.client_obj.pk]), {"body": "See you soon!"})
        msg = Message.objects.get(client=self.client_obj)
        self.assertTrue(msg.sent_by_staff)
        self.assertEqual(msg.status, "dry_run")


class ReminderTests(SalonTestCase):
    def test_sends_each_reminder_once(self):
        soon = Appointment.objects.create(
            client=self.client_obj, service=self.cut, stylist=self.priya, start=timezone.now() + timedelta(hours=3)
        )
        Appointment.objects.create(
            client=self.client_obj, service=self.cut, stylist=self.rahul, start=timezone.now() + timedelta(days=3)
        )
        out = StringIO()
        call_command("send_reminders", stdout=out)
        call_command("send_reminders", stdout=out)
        soon.refresh_from_db()
        self.assertTrue(soon.reminder_sent)
        self.assertEqual(Message.objects.filter(body__contains="Reminder").count(), 1)
