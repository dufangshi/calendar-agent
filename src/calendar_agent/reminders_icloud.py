"""Apple Reminders through iCloud's private CloudKit web API (via pyicloud).

Auth needs the Apple ID password + 2FA once; pyicloud then keeps a trusted
session in SESSION_DIR. App-specific passwords are rejected by CloudKit.
"""

from __future__ import annotations

import getpass
import logging
from dataclasses import dataclass
from datetime import datetime

from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloudAPIResponseException,
    PyiCloudConnectionException,
    PyiCloudException,
    PyiCloudFailedLoginException,
    PyiCloudServiceUnavailable,
)

from . import reminders_alarms
from .config import SESSION_DIR, Config, Secrets, ensure_data_dirs

LOGGER = logging.getLogger(__name__)


@dataclass
class Probe:
    # ok | needs_reauth | transient
    status: str
    reason: str = ""


def _service(cfg: Config, password: str | None) -> PyiCloudService:
    ensure_data_dirs()
    return PyiCloudService(
        cfg.apple_id,
        password=password,
        cookie_directory=str(SESSION_DIR),
        with_family=False,
        authenticate=False,
    )


def login_interactive(cfg: Config, password: str) -> PyiCloudService:
    """Password + 2FA in the terminal, then trust the session."""
    api = _service(cfg, password)
    api.authenticate(force_refresh=True)
    if api.requires_2fa:
        if api.security_key_names:
            raise SystemExit(
                "This account uses a hardware security key; not supported yet."
            )
        if not api.request_2fa_code():
            raise SystemExit("Apple did not offer a code-based 2FA challenge.")
        if api.two_factor_delivery_method == "sms":
            print("已通过短信发送验证码。")
        else:
            print("请在你的苹果设备上点「允许」，然后输入显示的 6 位验证码。")
        for _ in range(3):
            code = input("2FA code: ").strip()
            if api.validate_2fa_code(code):
                break
            print("验证码不对，再试一次。")
        else:
            raise SystemExit("2FA failed.")
    elif api.requires_2sa:
        raise SystemExit("Legacy two-step verification is not supported.")
    if not api.is_trusted_session:
        api.trust_session()
    return api


def prompt_password() -> str:
    return getpass.getpass("Apple ID 密码（仅用于本次登录）: ")


def open_service(cfg: Config, secrets: Secrets) -> tuple[PyiCloudService | None, Probe]:
    """Reuse the cached session; renew silently with a stored password if Apple allows."""
    try:
        api = _service(cfg, secrets.apple_password or None)
        status = api.get_auth_status()
        if not (status["authenticated"] and status["trusted_session"]):
            if not secrets.apple_password:
                return None, Probe("needs_reauth", "iCloud web session expired")
            api.authenticate(force_refresh=True)
            if api.requires_2fa or api.requires_2sa or not api.is_trusted_session:
                return None, Probe("needs_reauth", "Apple asked for 2FA again")
        return api, Probe("ok")
    except PyiCloudConnectionException as exc:
        return None, Probe("transient", f"network: {exc}")
    except PyiCloudFailedLoginException as exc:
        return None, Probe("needs_reauth", f"login rejected: {exc}")
    except PyiCloudException as exc:
        return None, Probe("transient", f"{type(exc).__name__}: {exc}")


def probe(cfg: Config, secrets: Secrets) -> Probe:
    """Session check plus one real Reminders read, so ADP/PCS problems surface too."""
    api, result = open_service(cfg, secrets)
    if api is None:
        return result
    try:
        next(iter(api.reminders.lists()), None)
    except PyiCloudConnectionException as exc:
        return Probe("transient", f"network: {exc}")
    except (PyiCloudServiceUnavailable, PyiCloudAPIResponseException) as exc:
        # Most often: Advanced Data Protection without "Access iCloud Data on the Web".
        return Probe("needs_reauth", f"Reminders unavailable: {exc}")
    return Probe("ok")


def pick_list(api: PyiCloudService, name: str):
    """The named list, else the first one (when the name is empty or gone)."""
    lists = [
        lst for lst in api.reminders.lists() if not lst.deleted and not lst.is_group
    ]
    if not lists:
        raise RuntimeError("这个账户没有提醒事项清单")
    for lst in lists:
        if name and lst.title == name:
            return lst
    return lists[0]


def list_titles(cfg: Config, secrets: Secrets, max_age: float = 3600) -> list[str]:
    """Reminders list names for the model to choose from, cached for an hour."""
    import time

    from .config import load_state, save_state

    state = load_state()
    cached = state.get("reminderLists") or {}
    if cached.get("titles") and time.time() - cached.get("at", 0) < max_age:
        return cached["titles"]
    api, _ = open_service(cfg, secrets)
    if api is None:
        return cached.get("titles") or []
    titles = [
        lst.title
        for lst in api.reminders.lists()
        if not lst.deleted and not lst.is_group
    ]
    state = load_state()
    state["reminderLists"] = {"at": time.time(), "titles": titles}
    save_state(state)
    return titles


def create_reminder(
    api: PyiCloudService,
    list_id: str,
    title: str,
    due: datetime | None,
    tz_name: str,
    notes: str = "",
    early_hours: tuple[int, ...] = (),
    all_day: bool = False,
    priority: int = 0,
):
    reminder = api.reminders.create(
        list_id=list_id,
        title=title,
        desc=notes,
        due_date=due,
        time_zone=None if all_day or due is None else tz_name,
        all_day=all_day,
        priority=priority,
    )
    # A timed due date rings like the app's own reminders; dateless and
    # all-day ones carry no alarm, as when created in the app.
    if due is not None and not all_day:
        reminders_alarms.set_alerts(api, reminder, due, tz_name, early_hours)
    return reminder
