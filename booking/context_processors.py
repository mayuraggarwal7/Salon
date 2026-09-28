from django.conf import settings


def salon(request):
    number = settings.WHATSAPP_PUBLIC_NUMBER
    return {
        "SALON_NAME": settings.SALON_NAME,
        "SALON_CURRENCY": settings.SALON_CURRENCY,
        "WHATSAPP_LINK": f"https://wa.me/{number}?text=Hi" if number else "",
        "WHATSAPP_LIVE": bool(settings.WHATSAPP_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID),
    }


def inbox(request):
    if not request.user.is_authenticated:
        return {}
    from django.db.models import Q

    from whatsapp.models import Conversation, Message
    from whatsapp.routing import is_manager

    manager = is_manager(request.user)
    unread = Message.objects.filter(direction=Message.INBOUND, read_by_staff=False)
    waiting = Conversation.objects.filter(needs_staff=True)
    if not manager:
        me = request.user.stylist
        unread = unread.filter(
            Q(client__conversation__assigned_to=me) | Q(client__anchor_stylist=me)
        )
        waiting = waiting.filter(Q(assigned_to=me) | Q(assigned_to__isnull=True))
    # Number of chats that need a look from staff.
    attention = set(unread.values_list("client", flat=True)) | set(waiting.values_list("client", flat=True))
    return {"INBOX_ATTENTION": len(attention), "IS_MANAGER": manager}
