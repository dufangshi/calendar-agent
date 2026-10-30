"""Credential health check and re-auth notifications.

Two independent credentials:
  - CalDAV app-specific password: long-lived, only dies if revoked.
  - CloudKit web session (Reminders): expires periodically and needs 2FA.

When the CloudKit session dies, the agent puts an event with an alert into the
Agent calendar over CalDAV, so every Apple device rings. If CalDAV itself is
broken, it falls back to `notify_command` (if configured) and the exit code.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from caldav.lib import error as caldav_error

from . import calendar_caldav, reminders_icloud
from .config import Config, Secrets, load_state, save_state

LOGGER = logging.getLogger(__name__)


@dataclass
class Report:
    caldav: str
    caldav_reason: str
    reminders: str
    reminders_reason: str

    @property
    def needs_reauth(self) -> bool:
        return "needs_reauth" in (self.caldav, self.reminders)

    @property
    def exit_code(self) -> int:
        if self.needs_reauth:
            return 2
        if "transient" in (self.caldav, self.reminders):
            return 1
        return 0


def check_caldav(cfg: Config, secrets: Secrets) -> tuple[str, str, object | None]:
    if not secrets.caldav_app_password:
        return "needs_reauth", "no app-specific password configured", None
    try:
        principal = calendar_caldav.connect(cfg.apple_id, secrets.caldav_app_password)
        principal.calendars()
        return "ok", "", principal
    except calendar_caldav.CalDAVAuthError as exc:
        return "needs_reauth", f"app-specific password rejected: {exc}", None
    except (caldav_error.DAVError, OSError) as exc:  # network, DNS, 5xx
        return "transient", f"{type(exc).__name__}: {exc}", None


def run_check(cfg: Config, secrets: Secrets) -> tuple[Report, object | None]:
    c_status, c_reason, principal = check_caldav(cfg, secrets)
    r = reminders_icloud.probe(cfg, secrets)
    return Report(c_status, c_reason, r.status, r.reason), principal


def _message(report: Report) -> str:
    parts = []
    if report.reminders == "needs_reauth":
        parts.append(
            f"提醒事项授权失效（{report.reminders_reason}）。运行: calendar-agent auth --icloud"
        )
    if report.caldav == "needs_reauth":
        parts.append(
            f"日历 App 专用密码失效（{report.caldav_reason}）。运行: calendar-agent auth --caldav"
        )
    return "\n".join(parts)


def notify(cfg: Config, report: Report, principal) -> list[str]:
    """Send a re-auth notification at most once per cooldown. Returns channels used."""
    state = load_state()
    now = datetime.now(ZoneInfo(cfg.timezone))
    key = f"{report.caldav}/{report.reminders}"
    last = state.get("reauth_notice", {})
    cooldown = timedelta(hours=cfg.notify_cooldown_hours)
    if (
        last.get("key") == key
        and last.get("at")
        and now - datetime.fromisoformat(last["at"]) < cooldown
    ):
        return []

    text = _message(report)
    used: list[str] = []
    href = None
    if principal is not None:
        calendar = calendar_caldav.get_or_create_calendar(principal, cfg.calendar_name)
        start = now + timedelta(minutes=2)
        _, href = calendar_caldav.add_event(
            calendar,
            calendar_caldav.EventSpec(
                summary="⚠️ calendar-agent 需要重新授权",
                start=start,
                end=start + timedelta(minutes=15),
                description=text,
                alerts_minutes=(0,),
                source="reauth-notice",
            ),
            ZoneInfo(cfg.timezone),
        )
        used.append("icloud-calendar")
    command = os.environ.get("CALENDAR_AGENT_NOTIFY_COMMAND")
    if command:
        subprocess.run(
            command,
            shell=True,
            env={**os.environ, "CALENDAR_AGENT_MESSAGE": text},
            check=False,
        )
        used.append("notify-command")

    if used:
        state["reauth_notice"] = {"key": key, "at": now.isoformat(), "eventHref": href}
        save_state(state)
    return used


def clear_notice(principal, cfg: Config) -> None:
    """After a successful check, drop the stale re-auth event from the calendar."""
    state = load_state()
    notice = state.pop("reauth_notice", None)
    if not notice:
        return
    if notice.get("eventHref") and principal is not None:
        try:
            calendar = calendar_caldav.get_or_create_calendar(
                principal, cfg.calendar_name
            )
            calendar_caldav.delete_event(calendar, notice["eventHref"])
        except (caldav_error.DAVError, OSError):
            LOGGER.warning("could not delete re-auth notice event", exc_info=True)
    save_state(state)


def report_dict(report: Report) -> dict:
    return {
        "caldav": report.caldav,
        "caldavReason": report.caldav_reason,
        "reminders": report.reminders,
        "remindersReason": report.reminders_reason,
        "needsReauth": report.needs_reauth,
    }
