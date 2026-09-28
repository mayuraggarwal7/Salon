from django.contrib import admin

from .models import Conversation, Message


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "client", "direction", "status", "body")
    list_filter = ("direction", "status")
    search_fields = ("client__name", "client__phone", "body")


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("client", "step", "needs_staff", "last_inbound_at")
    list_filter = ("needs_staff", "step")
