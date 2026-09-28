from datetime import time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from booking.models import OpeningHours, Service, Stylist


class Command(BaseCommand):
    help = "Create sample services, stylists and opening hours (and an admin/admin login, locally) for trying the app."

    def add_arguments(self, parser):
        parser.add_argument("--no-admin", action="store_true", help="Don't create the admin/admin login.")

    def handle(self, *args, no_admin=False, **options):
        for weekday in range(7):
            OpeningHours.objects.update_or_create(
                weekday=weekday,
                defaults={"opens": time(10), "closes": time(20), "closed": weekday == 0},
            )
        services = {}
        for name, minutes, price, rebook in [
            ("Haircut", 30, "300", 35),
            ("Hair colour", 90, "1500", 45),
            ("Hair spa", 30, "600", None),
            ("Beard trim", 15, "150", 14),
            ("Facial", 60, "900", 30),
            ("Manicure", 45, "500", 21),
            ("Signature cut & style", 60, "800", 35),
        ]:
            services[name], _ = Service.objects.get_or_create(
                name=name,
                defaults={"duration_minutes": minutes, "price": Decimal(price), "rebook_after_days": rebook},
            )
        # What to suggest alongside each service (cross-sell / upsell).
        for name, extras in [
            ("Haircut", ["Hair spa", "Beard trim"]),
            ("Hair colour", ["Hair spa"]),
            ("Facial", ["Manicure"]),
        ]:
            services[name].related_services.add(*[services[e] for e in extras])
        # Premium versions to upsell to.
        services["Haircut"].upgrade = services["Signature cut & style"]
        services["Haircut"].save(update_fields=["upgrade"])
        for name, offers in [
            ("Priya", ["Haircut", "Signature cut & style", "Hair colour", "Hair spa", "Facial"]),
            ("Rahul", ["Haircut", "Beard trim"]),
            ("Meera", ["Facial", "Manicure", "Hair colour"]),
        ]:
            stylist, _ = Stylist.objects.get_or_create(name=name)
            stylist.services.set([services[s] for s in offers])

        User = get_user_model()
        if not no_admin and not User.objects.filter(username="admin").exists():
            User.objects.create_superuser("admin", "", "admin")
            self.stdout.write("Created login admin / admin — change this password before going live.")
        self.stdout.write(self.style.SUCCESS("Demo data ready."))
