"""Local config, secrets and state paths.

Nothing here is inside the repository: config/secrets live under
~/.config/calendar-agent, the pyicloud session and health state under
~/.local/share/calendar-agent. Secrets and session files are 0600.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("CALENDAR_AGENT_CONFIG_DIR", "~/.config/calendar-agent")
).expanduser()
DATA_DIR = Path(
    os.environ.get("CALENDAR_AGENT_DATA_DIR", "~/.local/share/calendar-agent")
).expanduser()

CONFIG_FILE = CONFIG_DIR / "config.toml"
SECRETS_FILE = CONFIG_DIR / "secrets.json"
SESSION_DIR = DATA_DIR / "icloud-session"
STATE_FILE = DATA_DIR / "state.json"

CALDAV_URL = "https://caldav.icloud.com"


def local_timezone() -> str:
    """This machine's IANA zone, from TZ, /etc/timezone or /etc/localtime; else UTC."""
    if os.environ.get("TZ", "").count("/"):
        return os.environ["TZ"].lstrip(":")
    try:
        name = Path("/etc/timezone").read_text().strip()
        if name:
            return name
    except OSError:
        pass
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


@dataclass
class Config:
    apple_id: str = ""
    # IANA name; empty means this machine's local zone.
    timezone: str = ""
    # Calendar the agent writes to; created on first auth if missing.
    calendar_name: str = "Agent"
    # Existing Reminders list (Reminders lists can't be created via CloudKit yet).
    # Empty means the account's first list.
    reminders_list: str = ""
    # Reminders account UUID stamped on early reminders. It changes when the
    # iPhone is replaced. Empty: discovered from reminders the app wrote.
    reminders_account_id: str = ""
    # Don't repeat a re-auth notification more often than this.
    notify_cooldown_hours: float = 12.0
    # Only this Telegram user (private chat) may talk to the bot.
    telegram_owner_id: int = 0
    # Pockymoe saved connection for the light extraction call; empty sends
    # every message to the agent thread instead.
    model_connection_id: str = ""
    model: str = ""
    # Harness of the conversation thread the plugin keeps (Pockymoe provider/agent).
    agent_provider: str = "acp"
    agent_id: str = "deepseek"
    agent_model: str = "default"

    def __post_init__(self) -> None:
        if not self.timezone:
            self.timezone = local_timezone()

    @classmethod
    def load(cls) -> Config:
        if not CONFIG_FILE.exists():
            return cls()
        data = tomllib.loads(CONFIG_FILE.read_text())
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        lines = []
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            lines.append(f"{name} = {json.dumps(value)}")
        CONFIG_FILE.write_text("\n".join(lines) + "\n")


@dataclass
class Secrets:
    # App-specific password, used only for CalDAV (calendar + notifications).
    caldav_app_password: str = ""
    # Optional Apple ID password. When present, an expired web session can be
    # renewed without the user as long as Apple still trusts this session (no 2FA).
    apple_password: str = ""
    telegram_bot_token: str = ""

    @classmethod
    def load(cls) -> Secrets:
        if not SECRETS_FILE.exists():
            return cls()
        data = json.loads(SECRETS_FILE.read_text())
        return cls(
            caldav_app_password=data.get("caldav_app_password", ""),
            apple_password=data.get("apple_password", ""),
            telegram_bot_token=data.get("telegram_bot_token", ""),
        )

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(CONFIG_DIR, 0o700)
        payload = {"caldav_app_password": self.caldav_app_password}
        if self.apple_password:
            payload["apple_password"] = self.apple_password
        if self.telegram_bot_token:
            payload["telegram_bot_token"] = self.telegram_bot_token
        tmp = SECRETS_FILE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, SECRETS_FILE)


def ensure_data_dirs() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    os.chmod(SESSION_DIR, 0o700)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict) -> None:
    ensure_data_dirs()
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
