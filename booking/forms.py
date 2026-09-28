from datetime import datetime

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.forms import inlineformset_factory, modelformset_factory
from django.utils import timezone

from .availability import is_slot_free
from .models import (
    Appointment,
    Client,
    Invoice,
    InvoiceLine,
    OpeningHours,
    Service,
    Stylist,
    normalize_phone,
)


class DateInput(forms.DateInput):
    input_type = "date"


class TimeInput(forms.TimeInput):
    input_type = "time"


class AppointmentForm(forms.ModelForm):
    client_phone = forms.CharField(label="Client WhatsApp number", help_text="With country code, e.g. 919812345678")
    client_name = forms.CharField(label="Client name", required=False)
    date = forms.DateField(widget=DateInput)
    time = forms.TimeField(widget=TimeInput)
    notify = forms.BooleanField(
        label="Send WhatsApp confirmation", required=False, initial=True
    )

    class Meta:
        model = Appointment
        fields = ["service", "stylist", "notes", "status"]
        help_texts = {"stylist": "Leave as “any” to use the client's anchor stylist when free."}

    field_order = ["client_phone", "client_name", "service", "stylist", "date", "time", "notes", "status", "notify"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = Service.objects.filter(active=True)
        self.fields["stylist"].queryset = Stylist.objects.filter(active=True)
        self.fields["stylist"].empty_label = "Any available stylist"
        if self.instance.pk:
            local = timezone.localtime(self.instance.start)
            self.initial.update(
                client_phone=self.instance.client.phone,
                client_name=self.instance.client.name,
                date=local.date(),
                time=local.time(),
            )
            self.fields["notify"].initial = False
            self.fields["notify"].label = "Send WhatsApp confirmation of the changes"
        else:
            del self.fields["status"]

    def clean_client_phone(self):
        phone = normalize_phone(self.cleaned_data["client_phone"])
        if len(phone) < 8:
            raise forms.ValidationError("Enter the full number including country code.")
        return phone

    def clean(self):
        data = super().clean()
        service, day, time = data.get("service"), data.get("date"), data.get("time")
        if not (service and day and time):
            return data
        start = timezone.make_aware(datetime.combine(day, time))
        self.instance.start = start
        self.instance.service = service
        if not data.get("stylist"):
            existing = Client.objects.filter(phone=data.get("client_phone")).first()
            anchor = existing.anchor_stylist if existing else None
            candidates = list(Stylist.objects.filter(active=True, services=service))
            if anchor in candidates:
                candidates.remove(anchor)
                candidates.insert(0, anchor)
            free = [s for s in candidates if is_slot_free(service, start, s, exclude_pk=self.instance.pk)]
            if not free:
                raise forms.ValidationError("No stylist offering this service is free at that time.")
            data["stylist"] = free[0]
        return data

    def save(self, commit=True):
        client, _ = Client.objects.get_or_create(phone=self.cleaned_data["client_phone"])
        name = self.cleaned_data.get("client_name")
        if name and name != client.name:
            client.name = name
            client.save(update_fields=["name"])
        self.instance.client = client
        return super().save(commit)


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ["name", "phone", "anchor_stylist", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["anchor_stylist"].queryset = Stylist.objects.filter(active=True)

    def clean_phone(self):
        phone = normalize_phone(self.cleaned_data["phone"])
        if Client.objects.filter(phone=phone).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("A client with this number already exists.")
        return phone


class ServiceForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = [
            "name", "description", "duration_minutes", "price",
            "rebook_after_days", "related_services", "upgrade", "active",
        ]
        widgets = {"related_services": forms.CheckboxSelectMultiple}
        labels = {"related_services": "Add-ons to suggest alongside (cross-sell)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        others = Service.objects.filter(active=True).exclude(pk=self.instance.pk)
        self.fields["related_services"].queryset = others
        self.fields["upgrade"].queryset = others


class StylistForm(forms.ModelForm):
    username = forms.CharField(
        label="Login username", required=False, help_text="Give this stylist their own login to see their clients and chats."
    )
    password = forms.CharField(
        label="Set password", required=False, widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank to keep the current password.",
    )

    class Meta:
        model = Stylist
        fields = ["name", "phone", "services", "relay_whatsapp", "active"]
        widgets = {"services": forms.CheckboxSelectMultiple}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.user_id:
            self.fields["username"].initial = self.instance.user.username

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        User = get_user_model()
        if username and User.objects.filter(username=username).exclude(pk=self.instance.user_id).exists():
            raise forms.ValidationError("That username is taken.")
        return username

    def clean(self):
        data = super().clean()
        if data.get("relay_whatsapp") and not data.get("phone"):
            self.add_error("phone", "Add the stylist's WhatsApp number to forward chats to it.")
        if data.get("username") and not self.instance.user_id and not data.get("password"):
            self.add_error("password", "Set a password for the new login.")
        if data.get("password"):
            try:
                validate_password(data["password"])
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return data

    def save(self, commit=True):
        stylist = super().save(commit)
        username, password = self.cleaned_data.get("username"), self.cleaned_data.get("password")
        if username:
            User = get_user_model()
            user = stylist.user or User(is_staff=False)
            user.username = username
            user.first_name = stylist.name
            user.is_active = stylist.active
            if password:
                user.set_password(password)
            user.save()
            if stylist.user_id != user.pk:
                stylist.user = user
                stylist.save(update_fields=["user"])
        return stylist


class OpeningHoursForm(forms.ModelForm):
    class Meta:
        model = OpeningHours
        fields = ["weekday", "opens", "closes", "closed"]
        widgets = {"weekday": forms.HiddenInput, "opens": TimeInput, "closes": TimeInput}


OpeningHoursFormSet = modelformset_factory(OpeningHours, form=OpeningHoursForm, extra=0)


class InvoiceForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ["date", "number", "stylist", "file", "notes"]
        widgets = {"date": DateInput}
        labels = {"number": "Invoice number"}


class InvoiceLineForm(forms.ModelForm):
    class Meta:
        model = InvoiceLine
        fields = ["service", "description", "quantity", "unit_price"]
        labels = {"description": "Or other item"}

    def clean(self):
        data = super().clean()
        if data.get("unit_price") is not None and not (data.get("service") or data.get("description")):
            raise forms.ValidationError("Choose a service or describe the item.")
        return data


InvoiceLineFormSet = inlineformset_factory(Invoice, InvoiceLine, form=InvoiceLineForm, extra=4, can_delete=False)


class InvoiceImportForm(forms.Form):
    file = forms.FileField(label="CSV file", help_text="One row per service on each invoice.")

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith(".csv"):
            raise forms.ValidationError("Upload a .csv file (export from Excel with “Save as CSV”).")
        return f


class CheckoutForm(forms.Form):
    """Prices for the booked service and any add-ons the client agreed to."""

    def __init__(self, *args, appointment, suggestions, **kwargs):
        super().__init__(*args, **kwargs)
        self.appointment = appointment
        self.suggestions = suggestions
        self.fields["main_price"] = forms.DecimalField(
            label=appointment.service.name, min_value=0, decimal_places=2, initial=appointment.service.price
        )
        for s in suggestions:
            self.fields[f"add_{s.service.pk}"] = forms.BooleanField(label=f"Add {s.service.name}", required=False)
            self.fields[f"price_{s.service.pk}"] = forms.DecimalField(
                min_value=0, decimal_places=2, initial=s.service.price, required=False
            )
        self.fields["extra_service"] = forms.ModelChoiceField(
            Service.objects.filter(active=True), required=False, label="Anything else?", empty_label="—"
        )
        self.fields["extra_price"] = forms.DecimalField(min_value=0, decimal_places=2, required=False)
        self.fields["notes"] = forms.CharField(required=False, max_length=255)

    def suggestion_rows(self):
        for s in self.suggestions:
            yield s, self[f"add_{s.service.pk}"], self[f"price_{s.service.pk}"]

    def lines(self):
        data = self.cleaned_data
        lines = [(self.appointment.service, data["main_price"])]
        for s in self.suggestions:
            if data.get(f"add_{s.service.pk}"):
                price = data.get(f"price_{s.service.pk}")
                lines.append((s.service, s.service.price if price is None else price))
        if data.get("extra_service"):
            price = data.get("extra_price")
            lines.append((data["extra_service"], data["extra_service"].price if price is None else price))
        return lines


class ReplyForm(forms.Form):
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Type a WhatsApp reply…"}))
