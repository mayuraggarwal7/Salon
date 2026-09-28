from django.contrib import admin

from .models import Appointment, Client, OpeningHours, Service, Stylist


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "duration_minutes", "price", "active")
    list_filter = ("active",)


@admin.register(Stylist)
class StylistAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "active")
    filter_horizontal = ("services",)


@admin.register(OpeningHours)
class OpeningHoursAdmin(admin.ModelAdmin):
    list_display = ("weekday", "opens", "closes", "closed")


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "created_at")
    search_fields = ("name", "phone")


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("start", "client", "service", "stylist", "status", "source")
    list_filter = ("status", "source", "stylist")
    date_hierarchy = "start"
