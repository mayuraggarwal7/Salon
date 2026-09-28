"""Anchor stylists, invoice history, charge prompts, cross/upsell and staff permissions."""
import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from whatsapp.models import Message

from . import insights
from .invoice_import import import_invoices
from .models import Appointment, Client, Invoice, InvoiceLine, Service, Stylist
from .tests import SalonTestCase, at


def csv_file(text, name="invoices.csv"):
    return SimpleUploadedFile(name, text.encode(), content_type="text/csv")


def invoice(client, day, *lines, stylist=None, number=""):
    inv = Invoice.objects.create(client=client, date=day, stylist=stylist, number=number)
    for service, price in lines:
        InvoiceLine.objects.create(invoice=inv, service=service, description=service.name, unit_price=Decimal(price))
    inv.recalculate_total()
    return inv


class ImportTests(SalonTestCase):
    def test_import_groups_lines_and_matches_everything(self):
        result = import_invoices(csv_file(
            "Bill No,Date,Mobile,Customer Name,Item,Amount,Qty,Staff\n"
            "A1,02/03/2026,+91 98123 45678,Asha,Haircut,250,1,Priya\n"
            "A1,02/03/2026,+91 98123 45678,Asha,colour,₹1400,1,Priya\n"
            "A2,15-04-2026,919900011122,Neha,Keratin,3000,1,Zoya\n"
            "A3,not a date,919900011122,Neha,Haircut,300,1,Rahul\n"
        ))
        self.assertEqual((result.invoices, result.lines, result.clients_created), (2, 3, 1))
        self.assertEqual(result.unmatched_services, {"Keratin": 1})
        self.assertEqual(result.unmatched_stylists, {"Zoya": 1})
        self.assertEqual(result.errors[0][0], 5)
        a1 = Invoice.objects.get(number="A1")
        self.assertEqual((a1.client, a1.total, a1.date), (self.client_obj, Decimal("1650"), date(2026, 3, 2)))
        self.assertEqual({l.service for l in a1.lines.all()}, {self.cut, self.colour})
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.anchor_stylist, self.priya)
        self.assertEqual(result.anchors_set, 1)

    def test_reimport_is_skipped(self):
        text = "invoice,date,phone,service,price\nX9,2026-01-05,919812345678,Haircut,250\n"
        import_invoices(csv_file(text))
        again = import_invoices(csv_file(text))
        self.assertEqual((again.invoices, again.duplicates), (0, 1))
        self.assertEqual(Invoice.objects.count(), 1)

    def test_missing_columns_reported(self):
        result = import_invoices(csv_file("name,amount\nAsha,100\n"))
        self.assertIn("Missing column", result.errors[0][1])

    def test_creating_service_links_imported_lines(self):
        import_invoices(csv_file("date,phone,service,price\n2026-01-05,919812345678,Keratin,3000\n"))
        staff = get_user_model().objects.create_user("boss", password="pw", is_staff=True)
        self.client.force_login(staff)
        self.client.post(reverse("service_new"), {"name": "Keratin", "duration_minutes": 90, "price": "3500", "active": "on"})
        self.assertEqual(InvoiceLine.objects.get().service.name, "Keratin")


class InsightTests(SalonTestCase):
    def setUp(self):
        self.today = timezone.localdate()

    def test_charge_prompt_flags_old_lower_price(self):
        invoice(self.client_obj, self.today - timedelta(days=40), (self.cut, "250"))
        self.assertIn("Last charged ₹250", insights.charge_prompt(self.client_obj, self.cut))
        self.assertIn("new price", insights.charge_prompt(self.client_obj, self.cut))
        self.assertEqual(insights.charge_prompt(self.client_obj, self.colour), "First time for this service.")

    def test_due_service_uses_their_own_rhythm(self):
        for days_ago in (100, 70, 40):
            invoice(self.client_obj, self.today - timedelta(days=days_ago), (self.colour, "1500"))
        [s] = insights.suggestions(self.client_obj)
        self.assertEqual((s.service, s.kind), (self.colour, "due"))
        self.assertIn("usually every 30 days", s.reason)

    def test_not_due_if_already_booked(self):
        for days_ago in (100, 70, 40):
            invoice(self.client_obj, self.today - timedelta(days=days_ago), (self.colour, "1500"))
        Appointment.objects.create(client=self.client_obj, service=self.colour, stylist=self.priya, start=at(self.day, 10))
        self.assertEqual(insights.suggestions(self.client_obj), [])

    def test_cross_sell_related_and_frequently_paired(self):
        spa = Service.objects.create(name="Hair spa", duration_minutes=30, price=Decimal("600"))
        beard = Service.objects.create(name="Beard trim", duration_minutes=15, price=Decimal("150"))
        self.cut.related_services.add(spa)
        other = Client.objects.create(phone="911111111111")
        for i in range(2):
            invoice(other, self.today - timedelta(days=i + 1), (self.cut, "300"), (beard, "150"))
        kinds = {s.service: s.kind for s in insights.suggestions(self.client_obj, [self.cut])}
        self.assertEqual(kinds, {spa: "cross_sell", beard: "cross_sell"})

    def test_upsell_to_premium_upgrade(self):
        premium = Service.objects.create(name="Signature cut", duration_minutes=45, price=Decimal("800"))
        self.cut.upgrade = premium
        self.cut.save()
        invoice(self.client_obj, self.today - timedelta(days=5), (self.cut, "300"))
        [s] = insights.suggestions(self.client_obj)
        self.assertEqual((s.service, s.kind), (premium, "upsell"))
        self.assertIn("Upgrade from Haircut (+₹500)", s.reason)
        # Pricier add-ons are cross-sells, not upsells.
        spa = Service.objects.create(name="Hair spa", duration_minutes=30, price=Decimal("600"))
        self.cut.related_services.add(spa)
        kinds = {s.service: s.kind for s in insights.suggestions(self.client_obj, [self.cut])}
        self.assertEqual(kinds, {premium: "upsell", spa: "cross_sell"})


class CheckoutTests(SalonTestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("desk", password="pw"))
        self.spa = Service.objects.create(name="Hair spa", duration_minutes=30, price=Decimal("600"))
        self.cut.related_services.add(self.spa)
        self.appt = Appointment.objects.create(client=self.client_obj, service=self.cut, stylist=self.priya, start=at(self.day, 10))

    def test_prompts_and_charges_with_addon(self):
        invoice(self.client_obj, timezone.localdate() - timedelta(days=60), (self.cut, "250"))
        page = self.client.get(reverse("checkout", args=[self.appt.pk]))
        self.assertContains(page, "Last charged ₹250")
        self.assertContains(page, "Hair spa")
        self.client.post(
            reverse("checkout", args=[self.appt.pk]),
            {"main_price": "300", f"add_{self.spa.pk}": "on", f"price_{self.spa.pk}": "550"},
        )
        inv = self.appt.invoice
        self.assertEqual(inv.total, Decimal("850"))
        self.assertEqual(inv.source, Invoice.SOURCE_CHECKOUT)
        self.appt.refresh_from_db()
        self.assertEqual(self.appt.status, Appointment.COMPLETED)
        # Second checkout of the same visit is refused.
        self.assertRedirects(self.client.get(reverse("checkout", args=[self.appt.pk])), reverse("client_detail", args=[self.client_obj.pk]))

    def test_schedule_shows_prompts(self):
        page = self.client.get(reverse("appointment_list") + f"?date={self.day.isoformat()}")
        self.assertContains(page, "First time for this service")
        self.assertContains(page, "Hair spa")


class InvoiceUploadTests(SalonTestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media)
        self.client.force_login(get_user_model().objects.create_user("desk", password="pw"))

    def test_upload_past_invoice_with_scan(self):
        with override_settings(MEDIA_ROOT=self.media):
            response = self.client.post(
                reverse("invoice_new", args=[self.client_obj.pk]),
                {
                    "date": "2026-02-01", "number": "OLD-7", "stylist": self.priya.pk,
                    "file": SimpleUploadedFile("bill.pdf", b"%PDF-1.4 test", content_type="application/pdf"),
                    "lines-TOTAL_FORMS": "4", "lines-INITIAL_FORMS": "0",
                    "lines-0-service": self.cut.pk, "lines-0-quantity": "1", "lines-0-unit_price": "280",
                    "lines-1-description": "Hair serum", "lines-1-quantity": "2", "lines-1-unit_price": "150",
                    **{f"lines-{i}-quantity": "1" for i in (2, 3)},
                },
            )
            self.assertEqual(response.status_code, 302)
            inv = Invoice.objects.get(number="OLD-7")
            self.assertEqual(inv.total, Decimal("580"))
            file_response = self.client.get(reverse("invoice_file", args=[inv.pk]))
            self.assertEqual(b"".join(file_response.streaming_content), b"%PDF-1.4 test")


class StaffAccessTests(SalonTestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_user("boss", password="pw", is_staff=True)
        self.client.force_login(self.manager)
        self.client.post(
            reverse("stylist_edit", args=[self.priya.pk]),
            {"name": "Priya", "phone": "919700000001", "services": [self.cut.pk], "active": "on",
             "username": "priya", "password": "Salon-pass-2026"},
        )
        self.priya.refresh_from_db()
        self.client_obj.anchor_stylist = self.priya
        self.client_obj.save()
        self.rahuls_client = Client.objects.create(phone="911111111111", name="Vik", anchor_stylist=self.rahul)

    def test_stylist_login_created(self):
        self.assertEqual(self.priya.user.username, "priya")
        self.assertFalse(self.priya.user.is_staff)
        self.assertTrue(self.client.login(username="priya", password="Salon-pass-2026"))

    def test_stylist_sees_only_their_clients(self):
        self.client.force_login(self.priya.user)
        clients = self.client.get(reverse("client_list"))
        self.assertContains(clients, "Asha")
        self.assertNotContains(clients, "Vik")
        self.assertEqual(self.client.get(reverse("client_detail", args=[self.rahuls_client.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("chat", args=[self.rahuls_client.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("invoice_import")).status_code, 403)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_desk_booking_defaults_to_anchor(self):
        self.client.post(
            reverse("appointment_new"),
            {"client_phone": self.client_obj.phone, "service": self.cut.pk, "stylist": "",
             "date": self.day.isoformat(), "time": "10:00"},
        )
        self.assertEqual(Appointment.objects.get(client=self.client_obj).stylist, self.priya)

    def test_offer_on_whatsapp(self):
        spa = Service.objects.create(name="Hair spa", duration_minutes=30, price=Decimal("600"))
        self.client.force_login(self.priya.user)
        self.client.post(reverse("send_offer", args=[self.client_obj.pk]), {"service": spa.pk})
        msg = Message.objects.get(client=self.client_obj)
        self.assertIn("Hair spa", msg.body)
        self.assertIn("[Book it]", msg.body)
        self.assertEqual(msg.staff, self.priya)

    def test_route_chat_to_stylist(self):
        self.client.post(reverse("chat_handover", args=[self.client_obj.pk]), {"action": "assign", "stylist": self.rahul.pk})
        self.assertEqual(self.client_obj.conversation.assigned_to, self.rahul)
        self.assertTrue(self.client_obj.conversation.needs_staff)
