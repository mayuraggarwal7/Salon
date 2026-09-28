from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from whatsapp import client as wa
from whatsapp import notifications, routing
from whatsapp.models import Conversation, Message

from . import insights
from .availability import available_slots
from .forms import (
    AppointmentForm,
    CheckoutForm,
    ClientForm,
    InvoiceForm,
    InvoiceImportForm,
    InvoiceLineFormSet,
    OpeningHoursFormSet,
    ReplyForm,
    ServiceForm,
    StylistForm,
)
from .invoice_import import SAMPLE_CSV, import_invoices, link_unmatched_lines
from .models import Appointment, Client, Invoice, InvoiceLine, OpeningHours, Service, Stylist
from .utils import money


def _day_bounds(day):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, time.min), tz)
    return start, start + timedelta(days=1)


def _my_stylist(request):
    """The stylist behind a stylist login; None for managers and front desk."""
    if routing.is_manager(request.user):
        return None
    return request.user.stylist


def _visible_client(request, pk):
    client = get_object_or_404(Client, pk=pk)
    if not routing.clients_visible_to(request.user).filter(pk=client.pk).exists():
        raise PermissionDenied
    return client


def manager_required(view):
    def wrapped(request, *args, **kwargs):
        if not routing.is_manager(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return login_required(wrapped)


@login_required
def dashboard(request):
    today = timezone.localdate()
    start, end = _day_bounds(today)
    me = _my_stylist(request)
    todays = Appointment.objects.filter(start__gte=start, start__lt=end).select_related(
        "client", "service", "stylist"
    )
    waiting = Conversation.objects.filter(needs_staff=True).select_related("client", "assigned_to")
    if me:
        todays = todays.filter(stylist=me)
        waiting = waiting.filter(Q(assigned_to=me) | Q(assigned_to__isnull=True))
    todays = insights.appointment_prompts(list(todays))
    active_today = [a for a in todays if a.status != Appointment.CANCELLED]
    week_end = start + timedelta(days=7)
    week = Appointment.objects.filter(start__gte=start, start__lt=week_end, status__in=Appointment.ACTIVE_STATUSES)
    context = {
        "today": today,
        "me": me,
        "appointments": todays,
        "stats": {
            "today": len(active_today),
            "revenue": sum(a.service.price for a in active_today),
            "week": (week.filter(stylist=me) if me else week).count(),
            "whatsapp_week": Appointment.objects.filter(
                created_at__gte=timezone.now() - timedelta(days=7), source=Appointment.SOURCE_WHATSAPP
            ).count(),
        },
        "waiting": waiting,
        "my_clients": me.anchored_clients.count() if me else None,
    }
    return render(request, "booking/dashboard.html", context)


# --- Appointments ----------------------------------------------------------------

@login_required
def appointment_list(request):
    try:
        day = date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        day = timezone.localdate()
    start, end = _day_bounds(day)
    appointments = Appointment.objects.filter(start__gte=start, start__lt=end).select_related(
        "client", "service", "stylist"
    )
    stylist_id = request.GET.get("stylist")
    if stylist_id is None and _my_stylist(request):
        stylist_id = str(_my_stylist(request).pk)
    if stylist_id:
        appointments = appointments.filter(stylist_id=stylist_id)
    return render(
        request,
        "booking/appointment_list.html",
        {
            "day": day,
            "prev_day": day - timedelta(days=1),
            "next_day": day + timedelta(days=1),
            "appointments": insights.appointment_prompts(list(appointments)),
            "stylists": Stylist.objects.filter(active=True),
            "stylist_id": stylist_id,
        },
    )


@login_required
def appointment_form(request, pk=None):
    appt = get_object_or_404(Appointment, pk=pk) if pk else None
    initial = {}
    if appt is None:
        initial = {k: request.GET[k] for k in ("client_phone", "client_name", "date") if k in request.GET}
        if "service" in request.GET:
            initial["service"] = request.GET["service"]
    form = AppointmentForm(request.POST or None, instance=appt, initial=initial)
    if request.method == "POST" and form.is_valid():
        appt = form.save()
        if form.cleaned_data.get("notify"):
            if appt.status == Appointment.CANCELLED:
                notifications.send_cancellation(appt)
            else:
                notifications.send_confirmation(appt)
        messages.success(request, f"Saved appointment for {appt.client}.")
        return redirect(f"{reverse('appointment_list')}?date={timezone.localtime(appt.start).date().isoformat()}")
    return render(request, "booking/appointment_form.html", {"form": form, "appointment": appt})


@login_required
@require_POST
def appointment_status(request, pk):
    appt = get_object_or_404(Appointment, pk=pk)
    status = request.POST.get("status")
    if status in dict(Appointment.STATUSES):
        appt.status = status
        appt.save(update_fields=["status"])
        if status == Appointment.CANCELLED and request.POST.get("notify"):
            notifications.send_cancellation(appt)
        messages.success(request, f"Marked {appt.client} as {appt.get_status_display().lower()}.")
    next_url = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect("appointment_list")


@login_required
def checkout(request, pk):
    """Charge for a visit, prompted by the client's price history and suggested add-ons."""
    appt = get_object_or_404(Appointment.objects.select_related("client", "service", "stylist"), pk=pk)
    if hasattr(appt, "invoice"):
        messages.info(request, "This visit has already been checked out.")
        return redirect("client_detail", pk=appt.client_id)
    history = insights.service_history(appt.client)
    suggestions = insights.suggestions(appt.client, [appt.service], history=history)
    form = CheckoutForm(request.POST or None, appointment=appt, suggestions=suggestions)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            invoice = Invoice.objects.create(
                client=appt.client,
                stylist=appt.stylist,
                appointment=appt,
                date=timezone.localtime(appt.start).date(),
                source=Invoice.SOURCE_CHECKOUT,
                notes=form.cleaned_data["notes"],
            )
            for service, price in form.lines():
                InvoiceLine.objects.create(invoice=invoice, service=service, description=service.name, unit_price=price)
            invoice.recalculate_total()
            appt.status = Appointment.COMPLETED
            appt.save(update_fields=["status"])
        messages.success(request, f"Charged {appt.client} {money(invoice.total)}.")
        return redirect(f"{reverse('appointment_list')}?date={invoice.date.isoformat()}")
    return render(
        request,
        "booking/checkout.html",
        {
            "appointment": appt,
            "form": form,
            "charge_hint": insights.charge_prompt(appt.client, appt.service, history),
            "history": history,
            "summary": insights.client_summary(appt.client),
        },
    )


@login_required
def slots_api(request):
    service = get_object_or_404(Service, pk=request.GET.get("service"))
    stylist = Stylist.objects.filter(pk=request.GET.get("stylist") or None).first()
    try:
        day = date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        return JsonResponse({"slots": []})
    slots = [
        {"time": timezone.localtime(start).strftime("%H:%M"), "stylist": s.name, "stylist_id": s.pk}
        for start, s in available_slots(service, day, stylist)
    ]
    return JsonResponse({"slots": slots})


# --- Clients -----------------------------------------------------------------------

@login_required
def client_list(request):
    q = request.GET.get("q", "").strip()
    clients = routing.clients_visible_to(request.user).select_related("anchor_stylist")
    anchor = request.GET.get("anchor")
    if anchor is None and _my_stylist(request):
        anchor = str(_my_stylist(request).pk)
    if anchor:
        clients = clients.filter(anchor_stylist_id=anchor)
    if q:
        clients = clients.filter(Q(name__icontains=q) | Q(phone__icontains=q))
    clients = clients.annotate(
        visits=Count("invoices", distinct=True), last_visit=Max("appointments__start")
    )
    return render(
        request,
        "booking/client_list.html",
        {"clients": clients, "q": q, "anchor": anchor, "stylists": Stylist.objects.filter(active=True)},
    )


@login_required
def client_detail(request, pk):
    client = _visible_client(request, pk)
    history = insights.service_history(client)
    return render(
        request,
        "booking/client_detail.html",
        {
            "client": client,
            "appointments": client.appointments.select_related("service", "stylist").order_by("-start")[:50],
            "invoices": client.invoices.select_related("stylist").prefetch_related("lines__service")[:50],
            "history": history,
            "summary": insights.client_summary(client),
            "suggestions": insights.suggestions(client, history=history),
        },
    )


@login_required
def client_form(request, pk=None):
    client = _visible_client(request, pk) if pk else None
    form = ClientForm(request.POST or None, instance=client)
    if request.method == "POST" and form.is_valid():
        client = form.save()
        messages.success(request, f"Saved {client}.")
        return redirect("client_detail", pk=client.pk)
    return render(request, "booking/simple_form.html", {"form": form, "title": "Client"})


@login_required
def invoice_new(request, client_pk):
    client = _visible_client(request, client_pk)
    invoice = Invoice(client=client, source=Invoice.SOURCE_MANUAL, stylist=client.anchor_stylist)
    form = InvoiceForm(request.POST or None, request.FILES or None, instance=invoice)
    lines = InvoiceLineFormSet(request.POST or None, instance=invoice)
    if request.method == "POST" and form.is_valid() and lines.is_valid():
        with transaction.atomic():
            invoice = form.save()
            lines.instance = invoice
            for line in lines.save(commit=False):
                if not line.description and line.service:
                    line.description = line.service.name
                line.save()
            invoice.recalculate_total()
        messages.success(request, f"Added past invoice ({money(invoice.total)}) for {client}.")
        return redirect("client_detail", pk=client.pk)
    return render(request, "booking/invoice_form.html", {"client": client, "form": form, "lines": lines})


@login_required
def invoice_file(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    _visible_client(request, invoice.client_id)
    if not invoice.file:
        raise Http404
    return FileResponse(invoice.file.open("rb"), filename=invoice.file.name.rsplit("/", 1)[-1])


@manager_required
def invoice_import(request):
    form = InvoiceImportForm(request.POST or None, request.FILES or None)
    result = None
    if request.method == "POST" and form.is_valid():
        result = import_invoices(form.cleaned_data["file"])
        if result.invoices:
            messages.success(request, f"Imported {result.invoices} invoice(s) with {result.lines} line(s).")
    return render(request, "booking/invoice_import.html", {"form": form, "result": result, "sample": SAMPLE_CSV})


# --- Catalogue ------------------------------------------------------------------------

@login_required
def service_list(request):
    return render(
        request,
        "booking/service_list.html",
        {"services": Service.objects.select_related("upgrade").prefetch_related("stylists", "related_services")},
    )


@manager_required
def service_form(request, pk=None):
    service = get_object_or_404(Service, pk=pk) if pk else None
    form = ServiceForm(request.POST or None, instance=service)
    if request.method == "POST" and form.is_valid():
        service = form.save()
        linked = link_unmatched_lines(service)
        messages.success(request, "Service saved." + (f" Linked {linked} past invoice line(s)." if linked else ""))
        return redirect("service_list")
    return render(request, "booking/simple_form.html", {"form": form, "title": "Service"})


@login_required
def stylist_list(request):
    stylists = Stylist.objects.prefetch_related("services").annotate(
        clients=Count("anchored_clients", distinct=True)
    )
    return render(request, "booking/stylist_list.html", {"stylists": stylists})


@manager_required
def stylist_form(request, pk=None):
    stylist = get_object_or_404(Stylist, pk=pk) if pk else None
    form = StylistForm(request.POST or None, instance=stylist)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Stylist saved.")
        return redirect("stylist_list")
    return render(request, "booking/simple_form.html", {"form": form, "title": "Stylist"})


@manager_required
def opening_hours(request):
    for weekday, _ in OpeningHours.WEEKDAYS:
        OpeningHours.objects.get_or_create(weekday=weekday, defaults={"closed": True})
    formset = OpeningHoursFormSet(request.POST or None, queryset=OpeningHours.objects.all())
    if request.method == "POST" and formset.is_valid():
        formset.save()
        messages.success(request, "Opening hours saved.")
        return redirect("opening_hours")
    return render(request, "booking/opening_hours.html", {"formset": formset})


# --- WhatsApp inbox ---------------------------------------------------------------

@login_required
def inbox(request):
    me = _my_stylist(request)
    view = request.GET.get("view") or ("mine" if me else "all")
    clients = routing.clients_visible_to(request.user).filter(messages__isnull=False)
    if view == "mine" and me:
        clients = clients.filter(Q(conversation__assigned_to=me) | Q(anchor_stylist=me))
    elif view == "unassigned":
        clients = clients.filter(Q(conversation__assigned_to__isnull=True) | Q(conversation__isnull=True))
    elif view == "waiting":
        clients = clients.filter(conversation__needs_staff=True)
    clients = (
        clients.annotate(
            last_message_at=Max("messages__created_at"),
            unread=Count(
                "messages",
                filter=Q(messages__direction=Message.INBOUND, messages__read_by_staff=False),
                distinct=True,
            ),
        )
        .select_related("conversation__assigned_to", "anchor_stylist")
        .order_by("-last_message_at")
    )
    return render(request, "booking/inbox.html", {"clients": clients, "view": view, "me": me})


@login_required
def chat(request, pk):
    client = _visible_client(request, pk)
    me = _my_stylist(request)
    form = ReplyForm(request.POST or None)
    conversation, _ = Conversation.objects.get_or_create(client=client)
    if request.method == "POST" and form.is_valid():
        msg = wa.send_text(client, form.cleaned_data["body"], sent_by_staff=True, staff=me)
        if not conversation.needs_staff or (me and conversation.assigned_to_id is None):
            routing.assign(conversation, me or conversation.assigned_to)
        if msg.status == "failed":
            messages.error(request, "WhatsApp rejected the message. Check the API settings or the 24h window.")
        return redirect("chat", pk=client.pk)
    client.messages.filter(direction=Message.INBOUND, read_by_staff=False).update(read_by_staff=True)
    history = insights.service_history(client)
    return render(
        request,
        "booking/chat.html",
        {
            "client": client,
            "chat_messages": client.messages.select_related("staff"),
            "conversation": conversation,
            "form": form,
            "stylists": Stylist.objects.filter(active=True),
            "summary": insights.client_summary(client),
            "history": history[:5],
            "suggestions": insights.suggestions(client, history=history),
            "window_open": conversation.last_inbound_at
            and timezone.now() - conversation.last_inbound_at < timedelta(hours=24),
        },
    )


@login_required
def chat_messages(request, pk):
    """Message list fragment, polled by the chat page for new messages."""
    client = _visible_client(request, pk)
    client.messages.filter(direction=Message.INBOUND, read_by_staff=False).update(read_by_staff=True)
    return render(request, "booking/_messages.html", {"chat_messages": client.messages.select_related("staff")})


@login_required
@require_POST
def chat_handover(request, pk):
    client = _visible_client(request, pk)
    conversation, _ = Conversation.objects.get_or_create(client=client)
    action = request.POST.get("action")
    if action == "bot":
        conversation.needs_staff = False
        conversation.reset()
        conversation.save()
    elif action == "assign":
        stylist = Stylist.objects.filter(pk=request.POST.get("stylist") or None, active=True).first()
        conversation.assigned_to = stylist
        conversation.needs_staff = True
        conversation.save()
        if stylist:
            routing.relay_to_staff(conversation, f"{client} has been passed to you. Swipe-reply here to chat.")
        messages.success(request, f"Chat assigned to {stylist or 'the front desk'}.")
    else:  # take over
        routing.assign(conversation, _my_stylist(request) or conversation.assigned_to)
    return redirect("chat", pk=client.pk)


@login_required
@require_POST
def send_offer(request, pk):
    """Cross-sell / upsell: send the client a one-tap offer to book a suggested service."""
    client = _visible_client(request, pk)
    service = get_object_or_404(Service, pk=request.POST.get("service"), active=True)
    me = _my_stylist(request)
    who = me or client.anchor_stylist
    body = request.POST.get("message") or (
        f"Hi {client.name or 'there'}! {who.name + ' here from ' if who else ''}{settings.SALON_NAME}. "
        f"I think you'd love our *{service.name}* ({money(service.price)}, {service.duration_minutes} min)."
        + (f"\n{service.description}" if service.description else "")
    )
    msg = wa.send_buttons(client, body, [(f"offer:{service.pk}", "Book it"), ("addon_no:", "Not now")], staff=me)
    if msg.status == "failed":
        messages.error(request, "WhatsApp rejected the offer. The client may be outside the 24h window.")
    else:
        messages.success(request, f"Sent {service.name} offer to {client}.")
    next_url = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect("client_detail", pk=client.pk)
