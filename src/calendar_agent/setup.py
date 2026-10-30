"""Setup the agent thread can drive from a conversation, without a terminal.

`missing()` lists what is not configured and where the owner gets it. Secrets
arrive on stdin (never as arguments) and are verified before they are saved.
The iCloud web login needs a 2FA code typed later, so a detached worker keeps
the login session alive and waits for `auth icloud-code`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from . import calendar_caldav, reminders_icloud
from .config import DATA_DIR, Config, Secrets, ensure_data_dirs
from .telegram import Bot, TelegramError

AUTH_STATUS = DATA_DIR / "icloud-auth.json"
AUTH_CODE = DATA_DIR / "icloud-2fa-code"
CODE_WAIT_SECONDS = 600

GUIDE = {
    "apple_id": "Apple ID 邮箱（iPhone 设置最上面的名字 → 查看）。发给我邮箱即可。",
    "caldav_app_password": "App 专用密码：打开 https://account.apple.com → 登录与安全 → App 专用密码 → 生成，"
    "格式 xxxx-xxxx-xxxx-xxxx，发给我。只用于日历，随时可在同一页面撤销。",
    "icloud_session": "提醒事项需要 Apple ID 主密码 + 双重认证：发给我主密码后，你的苹果设备会弹出验证码，"
    "再把 6 位验证码发给我。",
    "telegram_bot_token": "Telegram 机器人：在 Telegram 找 @BotFather → /newbot → 按提示起名，"
    "把它给的 token 发给我。",
    "telegram_owner_id": "你的 Telegram user id：机器人配好后，给它发任意一句话，它会回复你的 id；"
    "或者找 @userinfobot。把数字发给我。",
    "reminders_account_id": "提醒事项账户标识：在 iPhone「提醒事项」里随便建一条带「提前提醒」的待办，"
    "等它同步后告诉我，我会自动读取。",
}


def missing(cfg: Config | None = None, secrets: Secrets | None = None) -> list[dict]:
    cfg = cfg or Config.load()
    secrets = secrets or Secrets.load()
    gaps = []
    if not cfg.apple_id:
        gaps.append("apple_id")
    if not secrets.caldav_app_password:
        gaps.append("caldav_app_password")
    if cfg.apple_id:
        api, probe = reminders_icloud.open_service(cfg, secrets)
        if api is None and probe.status == "needs_reauth":
            gaps.append("icloud_session")
    if not secrets.telegram_bot_token:
        gaps.append("telegram_bot_token")
    if not cfg.telegram_owner_id:
        gaps.append("telegram_owner_id")
    if not cfg.reminders_account_id:
        gaps.append("reminders_account_id")
    return [{"item": key, "how": GUIDE[key]} for key in gaps]


def set_caldav_password(password: str) -> str:
    cfg = Config.load()
    if not cfg.apple_id:
        raise ValueError("先设置 apple_id")
    principal = calendar_caldav.connect(cfg.apple_id, password.strip())
    calendar = calendar_caldav.get_or_create_calendar(principal, cfg.calendar_name)
    secrets = Secrets.load()
    secrets.caldav_app_password = password.strip()
    secrets.save()
    return f"日历可用，写入「{calendar.get_display_name()}」"


def set_telegram_token(token: str) -> str:
    bot = Bot(token.strip())
    try:
        me = bot._call("getMe")
    except TelegramError as exc:
        raise ValueError(f"token 无效：{exc}") from None
    secrets = Secrets.load()
    secrets.telegram_bot_token = token.strip()
    secrets.save()
    return f"机器人 @{me.get('username')} 可用"


def detect_reminders_account() -> str:
    from . import reminders_alarms

    cfg = Config.load()
    api, probe = reminders_icloud.open_service(cfg, Secrets.load())
    if api is None:
        raise ValueError(f"提醒事项不可用：{probe.reason}")
    found = reminders_alarms.account_identifier(api, refresh=True)
    if not found:
        raise ValueError(
            "还没找到：请在 iPhone 上建一条带「提前提醒」的待办，同步后再试"
        )
    cfg.reminders_account_id = found
    cfg.save()
    return found


# --- iCloud web login with a 2FA code that arrives later -----------------


def _write_status(**fields) -> None:
    ensure_data_dirs()
    AUTH_STATUS.write_text(
        json.dumps({**fields, "at": time.time()}, ensure_ascii=False)
    )


def auth_status() -> dict:
    if not AUTH_STATUS.exists():
        return {"state": "idle"}
    return json.loads(AUTH_STATUS.read_text())


def start_icloud_login(password: str) -> dict:
    """Spawn the worker; the password goes over a pipe, never argv or disk."""
    ensure_data_dirs()
    AUTH_CODE.unlink(missing_ok=True)
    _write_status(state="starting")
    worker = subprocess.Popen(
        [sys.executable, "-m", "calendar_agent.setup", "icloud-worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    worker.stdin.write(password.encode())
    worker.stdin.close()
    for _ in range(60):
        status = auth_status()
        if status["state"] not in ("starting",):
            return status
        time.sleep(1)
    return auth_status()


def submit_icloud_code(code: str) -> dict:
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6:
        raise ValueError("验证码应为 6 位数字")
    if auth_status().get("state") != "waiting_code":
        raise ValueError("当前没有等待验证码的登录，请重新开始")
    fd = os.open(AUTH_CODE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(code)
    for _ in range(60):
        status = auth_status()
        if status["state"] != "waiting_code":
            return status
        time.sleep(1)
    return auth_status()


def _worker() -> None:
    password = sys.stdin.read()
    cfg = Config.load()
    try:
        api = reminders_icloud._service(cfg, password)
        api.authenticate(force_refresh=True)
        if api.requires_2fa:
            if api.security_key_names:
                _write_status(state="failed", error="此账户用硬件安全密钥，暂不支持")
                return
            api.request_2fa_code()
            delivery = (
                "短信" if api.two_factor_delivery_method == "sms" else "苹果设备弹窗"
            )
            _write_status(state="waiting_code", delivery=delivery)
            deadline = time.time() + CODE_WAIT_SECONDS
            while time.time() < deadline:
                if AUTH_CODE.exists():
                    code = AUTH_CODE.read_text().strip()
                    AUTH_CODE.unlink(missing_ok=True)
                    if api.validate_2fa_code(code):
                        break
                    _write_status(
                        state="waiting_code",
                        delivery=delivery,
                        error="验证码不对，再发一次",
                    )
                time.sleep(1)
            else:
                _write_status(state="failed", error="10 分钟内没有收到验证码")
                return
        if not api.is_trusted_session:
            api.trust_session()
        lst = reminders_icloud.pick_list(api, cfg.reminders_list)
        _write_status(state="ok", list=lst.title)
    except Exception as exc:  # noqa: BLE001 - reported to the conversation, never the password
        _write_status(state="failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")


if __name__ == "__main__" and sys.argv[1:] == ["icloud-worker"]:
    _worker()
