"""iCloud Calendar over CalDAV, authenticated with an app-specific password.

This is Apple's public, stable path. It is also the notification channel when
the Reminders (CloudKit) session needs re-auth, because it uses a separate
credential that does not expire with the web session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import caldav
from caldav.lib import error as caldav_error
from icalendar import Alarm, Calendar, Event

from .config import CALDAV_URL

# Marks events written by this agent so later sync rules can find/own them.
AGENT_PROP = "X-CALENDAR-AGENT"


class CalDAVAuthError(Exception):
    """The app-specific password was rejected (revoked, wrong, or Apple ID changed)."""


@dataclass
class EventSpec:
    summary: str
    start: datetime
    end: datetime
    description: str = ""
    location: str = ""
    all_day: bool = False
    # Minutes before start; each becomes one VALARM.
    alerts_minutes: tuple[int, ...] = ()
    uid: str | None = None
    source: str = "agent"


def connect(apple_id: str, app_password: str) -> caldav.Principal:
    client = caldav.DAVClient(url=CALDAV_URL, username=apple_id, password=app_password)
    try:
        return client.principal()
    except caldav_error.AuthorizationError as exc:
        raise CalDAVAuthError(str(exc)) from exc


def get_or_create_calendar(principal: caldav.Principal, name: str) -> caldav.Calendar:
    for cal in principal.calendars():
        if cal.get_display_name() == name:
            return cal
    return principal.make_calendar(
        name=name, supported_calendar_component_set=["VEVENT"]
    )


def build_ics(spec: EventSpec, tz: ZoneInfo) -> tuple[str, str]:
    uid = spec.uid or f"{uuid.uuid4()}@calendar-agent"
    event = Event()
    event.add("uid", uid)
    event.add("summary", spec.summary)
    event.add("dtstamp", datetime.now(tz))
    if spec.all_day:
        event.add("dtstart", spec.start.date())
        event.add("dtend", max(spec.end.date(), spec.start.date() + timedelta(days=1)))
    else:
        event.add("dtstart", spec.start.astimezone(tz))
        event.add("dtend", spec.end.astimezone(tz))
    if spec.description:
        event.add("description", spec.description)
    if spec.location:
        event.add("location", spec.location)
    event.add(AGENT_PROP, spec.source)
    for minutes in spec.alerts_minutes:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", spec.summary)
        alarm.add("trigger", timedelta(minutes=-minutes))
        event.add_component(alarm)
    cal = Calendar()
    cal.add("prodid", "-//calendar-agent//EN")
    cal.add("version", "2.0")
    cal.add_component(event)
    cal.add_missing_timezones()
    return uid, cal.to_ical().decode()


def add_event(
    calendar: caldav.Calendar, spec: EventSpec, tz: ZoneInfo
) -> tuple[str, str]:
    """Returns (uid, href). Keep the href: iCloud answers 412 to UID-filtered
    REPORT searches, so later updates/deletes must address the object by URL."""
    uid, ics = build_ics(spec, tz)
    event = calendar.add_event(ics)
    return uid, str(event.url)


def delete_event(calendar: caldav.Calendar, href: str) -> None:
    calendar.event_by_url(href).delete()


def upcoming(principal: caldav.Principal, start: datetime, end: datetime) -> list[dict]:
    """Events on every calendar between start and end, recurrences expanded."""
    out = []
    for cal in principal.calendars():
        name = cal.get_display_name()
        try:
            found = cal.search(start=start, end=end, event=True, expand=True)
        except caldav_error.DAVError:
            continue
        for obj in found:
            for comp in obj.icalendar_instance.walk("VEVENT"):
                begin = comp.decoded("dtstart")
                finish = comp.decoded("dtend") if comp.get("dtend") else begin
                out.append(
                    {
                        "calendar": name,
                        "title": str(comp.get("summary", "")),
                        "start": begin.isoformat(),
                        "end": finish.isoformat(),
                        "allDay": not isinstance(begin, datetime),
                    }
                )
    return sorted(out, key=lambda e: e["start"])
