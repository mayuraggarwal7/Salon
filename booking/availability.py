"""Works out which start times are free for a service on a given day."""
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from .models import Appointment, OpeningHours, Stylist


def opening_window(day):
    """Return (open_dt, close_dt) as aware datetimes for `day`, or None if closed."""
    hours = OpeningHours.objects.filter(weekday=day.weekday()).first()
    if hours is None or hours.closed or not (hours.opens and hours.closes):
        return None
    tz = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(day, hours.opens), tz),
        timezone.make_aware(datetime.combine(day, hours.closes), tz),
    )


def open_days(start_day=None, count=7, horizon=None):
    """The next `count` days the salon is open, starting from `start_day`."""
    start_day = start_day or timezone.localdate()
    horizon = horizon or settings.BOOKING_WINDOW_DAYS
    days = []
    for offset in range(horizon):
        day = start_day + timedelta(days=offset)
        if opening_window(day):
            days.append(day)
            if len(days) == count:
                break
    return days


def eligible_stylists(service, stylist=None):
    if stylist is not None:
        return [stylist]
    return list(Stylist.objects.filter(active=True, services=service))


def available_slots(service, day, stylist=None, now=None):
    """
    Return a list of (start_datetime, stylist) for every bookable start time.

    When no stylist is requested, each time is offered once, assigned to the first
    stylist who is free, so clients see a simple list of times.
    """
    window = opening_window(day)
    if window is None:
        return []
    open_dt, close_dt = window
    stylists = eligible_stylists(service, stylist)
    if not stylists:
        return []

    busy = {}
    for appt in Appointment.objects.filter(
        stylist__in=stylists,
        status__in=Appointment.ACTIVE_STATUSES,
        start__lt=close_dt,
        end__gt=open_dt,
    ):
        busy.setdefault(appt.stylist_id, []).append((appt.start, appt.end))

    now = now or timezone.now()
    step = timedelta(minutes=settings.SLOT_INTERVAL_MINUTES)
    slots = []
    start = open_dt
    while start + service.duration <= close_dt:
        end = start + service.duration
        if start > now:
            for s in stylists:
                if all(end <= b_start or start >= b_end for b_start, b_end in busy.get(s.pk, [])):
                    slots.append((start, s))
                    break
        start += step
    return slots


def is_slot_free(service, start, stylist, exclude_pk=None):
    end = start + service.duration
    return not Appointment.objects.filter(
        stylist=stylist, status__in=Appointment.ACTIVE_STATUSES, start__lt=end, end__gt=start
    ).exclude(pk=exclude_pk).exists()
