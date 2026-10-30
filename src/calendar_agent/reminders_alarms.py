"""Due-time alarm + early reminders written the way the Reminders app writes them.

pyicloud's create() stores only DueDate, and its set_early_reminders() uses
"Reminder/<uuid>" and the account dsid in the payload. iPhone ignores that:
the app shows no early reminder and never rings. Records the app itself
wrote (dumped from this account, 2026-10-28) look like this instead:

  Reminder.AlarmIDs = [<alarm uuid>]
  Alarm        {AlarmUID, TriggerID, Reminder ref, DueDateResolutionTokenAsNonce}
  AlarmTrigger {Type: "Date", Alarm ref,
                DateComponentsData: BYTES base64(JSON NSDateComponents)}
  DueDateDeltaAlertsData: reminderIdentifier = bare uuid,
                          accountIdentifier  = Reminders account UUID (not dsid)

This uses pyicloud's private write helpers, so pin pyicloud and re-check after
upgrades.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from pyicloud import PyiCloudService
from pyicloud.common.cloudkit import CKModifyOperation
from pyicloud.services.reminders._constants import _REMINDERS_ZONE_REQ
from pyicloud.services.reminders._protocol import (
    _APPLE_EPOCH_OFFSET,
    _as_raw_id,
    _encode_due_date_delta_alerts,
    _merge_resolution_token_map,
)
from pyicloud.services.reminders._support import (
    _assert_modify_success,
    _refresh_record_change_tag,
)
from pyicloud.services.reminders.models import (
    EarlyReminder,
    EarlyReminderUnit,
    Reminder,
)

from .config import Config, load_state, save_state

_STATE_KEY = "remindersAccountIdentifier"
# Reminders this agent wrote alerts for; excluded from account discovery so
# our own copies of the value never outvote the app.
_OWN_KEY = "agentReminderIds"


def _ref(record_name: str) -> dict:
    return {
        "type": "REFERENCE",
        "value": {"recordName": record_name, "action": "VALIDATE"},
    }


def _date_components(due: datetime, tz_name: str) -> str:
    local = due.astimezone(ZoneInfo(tz_name))
    doc = {
        "era": 1,
        "year": local.year,
        "month": local.month,
        "day": local.day,
        "hour": local.hour,
        "minute": local.minute,
        "second": local.second,
        "timeZone": {"identifier": tz_name},
    }
    return base64.b64encode(json.dumps(doc, separators=(",", ":")).encode()).decode()


def account_identifier(api: PyiCloudService, refresh: bool = False) -> str | None:
    """The Reminders account UUID the app stamps on early reminders.

    Taken from the newest early reminder the app wrote (it differs per device
    generation, so an account can hold several),
    then cached in state. Pass refresh=True after switching iPhones.
    None if the app never set an early reminder on this account.
    """
    state = load_state()
    if state.get(_STATE_KEY) and not refresh:
        return state[_STATE_KEY]
    own = set(state.get(_OWN_KEY, []))
    newest: tuple[float, str] | None = None
    for event in api.reminders.iter_changes():
        r = event.reminder
        if r is None or r.id in own:
            continue
        account = r.early_reminder_account_id
        # App-written values are UUIDs; pyicloud's own fallback is the numeric dsid.
        if not account or "-" not in account:
            continue
        for alert in r.early_reminders:
            created = alert.creation_date or 0.0
            if newest is None or created > newest[0]:
                newest = (created, account)
    if newest is None:
        return None
    state[_STATE_KEY] = newest[1]
    save_state(state)
    return newest[1]


def set_alerts(
    api: PyiCloudService,
    reminder: Reminder,
    due: datetime,
    tz_name: str,
    early_hours: tuple[int, ...] = (),
) -> None:
    """Attach a due-time alarm and early reminders in one atomic modify."""
    writes = api.reminders._writes
    record_name = writes._reminder_record_name(reminder.id)
    bare_id = _as_raw_id(record_name, "Reminder")
    alarm_uuid = str(uuid.uuid4()).upper()
    trigger_uuid = str(uuid.uuid4()).upper()
    alarm_name = f"Alarm/{alarm_uuid}"
    trigger_name = f"AlarmTrigger/{trigger_uuid}"
    now_ms = int(time.time() * 1000)
    apple_now = time.time() - _APPLE_EPOCH_OFFSET

    current = writes._lookup_current_fields(record_name)
    alarm_ids = [_as_raw_id(x, "Alarm") for x in (reminder.alarm_ids or [])] + [
        alarm_uuid
    ]
    changed = ["alarmIDs", "lastModifiedDate"]
    fields: dict = {
        "AlarmIDs": {"type": "STRING_LIST", "value": alarm_ids},
        "LastModifiedDate": {"type": "TIMESTAMP", "value": now_ms},
    }
    if early_hours:
        account = Config.load().reminders_account_id or account_identifier(api) or ""
        alerts = [
            EarlyReminder(
                count=-h,
                unit=EarlyReminderUnit.HOURS,
                identifier=str(uuid.uuid4()).upper(),
                creation_date=apple_now,
            )
            for h in early_hours
        ]
        fields["DueDateDeltaAlertsData"] = _encode_due_date_delta_alerts(
            reminder_identifier=bare_id, account_identifier=account, alerts=alerts
        )
        changed.append("dueDateDeltaAlertsData")
    fields["ResolutionTokenMap"] = {
        "type": "STRING",
        "value": _merge_resolution_token_map(
            current.get_value("ResolutionTokenMap") if current is not None else None,
            changed,
        ),
    }

    reminder_op = CKModifyOperation(
        operationType="update",
        record=writes._write_record(
            record_name=record_name,
            record_type="Reminder",
            record_change_tag=reminder.record_change_tag,
            fields=fields,
        ),
    )
    alarm_op = CKModifyOperation(
        operationType="create",
        record=writes._write_record(
            record_name=alarm_name,
            record_type="Alarm",
            fields={
                "AlarmUID": {"type": "STRING", "value": alarm_uuid},
                "Deleted": {"type": "INT64", "value": 0},
                "Imported": {"type": "INT64", "value": 0},
                "Reminder": _ref(record_name),
                "TriggerID": {"type": "STRING", "value": trigger_uuid},
                # The app uses 1e11 + the dueDate resolution-token time.
                "DueDateResolutionTokenAsNonce": {
                    "type": "DOUBLE",
                    "value": 100_000_000_000 + apple_now,
                },
            },
            parent_record_name=record_name,
        ),
    )
    trigger_op = CKModifyOperation(
        operationType="create",
        record=writes._write_record(
            record_name=trigger_name,
            record_type="AlarmTrigger",
            fields={
                "Alarm": _ref(alarm_name),
                "DateComponentsData": {
                    "type": "BYTES",
                    "value": _date_components(due, tz_name),
                },
                "Deleted": {"type": "INT64", "value": 0},
                "Imported": {"type": "INT64", "value": 0},
                "Type": {"type": "STRING", "value": "Date"},
            },
            parent_record_name=alarm_name,
        ),
    )
    response = writes._get_raw().modify(
        operations=[reminder_op, alarm_op, trigger_op],
        zone_id=_REMINDERS_ZONE_REQ,
        atomic=True,
    )
    _assert_modify_success(response, "Set reminder alerts")
    reminder.alarm_ids = alarm_ids
    _refresh_record_change_tag(response, reminder, record_name)
    state = load_state()
    state[_OWN_KEY] = sorted(set(state.get(_OWN_KEY, [])) | {record_name})
    save_state(state)
