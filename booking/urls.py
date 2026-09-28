from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("appointments/", views.appointment_list, name="appointment_list"),
    path("appointments/new/", views.appointment_form, name="appointment_new"),
    path("appointments/<int:pk>/", views.appointment_form, name="appointment_edit"),
    path("appointments/<int:pk>/status/", views.appointment_status, name="appointment_status"),
    path("appointments/<int:pk>/checkout/", views.checkout, name="checkout"),
    path("api/slots/", views.slots_api, name="slots_api"),
    path("clients/", views.client_list, name="client_list"),
    path("clients/new/", views.client_form, name="client_new"),
    path("clients/<int:pk>/", views.client_detail, name="client_detail"),
    path("clients/<int:pk>/edit/", views.client_form, name="client_edit"),
    path("clients/<int:client_pk>/invoices/new/", views.invoice_new, name="invoice_new"),
    path("clients/<int:pk>/offer/", views.send_offer, name="send_offer"),
    path("invoices/import/", views.invoice_import, name="invoice_import"),
    path("invoices/<int:pk>/file/", views.invoice_file, name="invoice_file"),
    path("services/", views.service_list, name="service_list"),
    path("services/new/", views.service_form, name="service_new"),
    path("services/<int:pk>/", views.service_form, name="service_edit"),
    path("stylists/", views.stylist_list, name="stylist_list"),
    path("stylists/new/", views.stylist_form, name="stylist_new"),
    path("stylists/<int:pk>/", views.stylist_form, name="stylist_edit"),
    path("hours/", views.opening_hours, name="opening_hours"),
    path("inbox/", views.inbox, name="inbox"),
    path("inbox/<int:pk>/", views.chat, name="chat"),
    path("inbox/<int:pk>/messages/", views.chat_messages, name="chat_messages"),
    path("inbox/<int:pk>/handover/", views.chat_handover, name="chat_handover"),
]
