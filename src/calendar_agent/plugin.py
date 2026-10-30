"""Pockymoe plugin entry: one JSON request on stdin, one JSON value on stdout.

See Pockymoe docs/external-plugins.md. Actions:
  status  connection summary for the plugin panel (display only)
  tick    scheduled every minute; runs the credential check at most every
          `check_interval_minutes` and sends a re-auth notice when needed
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta

from . import bot, health
from .config import Config, Secrets, load_state, save_state

CHECK_INTERVAL = timedelta(minutes=30)

LABELS = {
    "ok": "正常",
    "needs_reauth": "需要重新授权",
    "transient": "暂时连不上",
}


def _item(label: str, status: str, detail: str = "") -> dict:
    return {
        "label": label,
        "status": status,
        "statusText": LABELS.get(status, status),
        "detail": detail,
    }


def _not_configured(label: str) -> dict:
    return {
        "label": label,
        "status": "notConfigured",
        "statusText": "未配置",
        "detail": "让日程管家线程帮你配置",
    }


def _telegram(cfg: Config) -> dict:
    if not bot.configured(cfg, Secrets.load()):
        return _not_configured("Telegram")
    return _item("Telegram 机器人", "ok", f"只接受 user id {cfg.telegram_owner_id}")


def _slack() -> dict:
    from . import slack

    state = slack.status()
    if state["state"] == "notConfigured":
        return _not_configured("Slack")
    if state["state"] != "ok":
        return _item("Slack", "needs_reauth", "授权已过期")
    return _item("Slack", "ok", f"{state.get('team') or ''} · 只读")


def status(cfg: Config) -> dict:
    if not cfg.apple_id:
        return {
            "summary": "还没有授权 iCloud",
            "sections": [
                {
                    "title": "Apple",
                    "items": [
                        _not_configured("iCloud 日历"),
                        _not_configured("提醒事项"),
                    ],
                }
            ],
        }
    report, _ = health.run_check(cfg, Secrets.load())
    return {
        "summary": "需要重新授权" if report.needs_reauth else "运行正常",
        "sections": [
            {
                "title": "Apple（写入目标）",
                "items": [
                    _item(
                        f"iCloud 日历「{cfg.calendar_name}」",
                        report.caldav,
                        report.caldav_reason,
                    ),
                    _item(
                        f"提醒事项「{cfg.reminders_list or '默认清单'}」",
                        report.reminders,
                        report.reminders_reason,
                    ),
                ],
            },
            {
                "title": "信息来源",
                "items": [
                    _telegram(cfg),
                    _not_configured("Google 日历"),
                    _not_configured("Outlook 日历"),
                    _slack(),
                ],
            },
        ],
    }


def tick(cfg: Config) -> dict:
    out = {"telegram": bot.tick(cfg)}
    out["health"] = check(cfg)
    return out


def check(cfg: Config) -> dict:
    if not cfg.apple_id:
        return {"skipped": "not configured"}
    state = load_state()
    now = datetime.now(UTC)
    last = state.get("lastHealthCheckAt")
    if last and now - datetime.fromisoformat(last) < CHECK_INTERVAL:
        return {"skipped": "checked recently"}
    report, principal = health.run_check(cfg, Secrets.load())
    out = health.report_dict(report)
    if report.needs_reauth:
        out["notified"] = health.notify(cfg, report, principal)
    elif report.exit_code == 0:
        health.clear_notice(principal, cfg)
    state = load_state()
    state["lastHealthCheckAt"] = now.isoformat()
    save_state(state)
    return out


ACTIONS = {"status": status, "tick": tick}


def main() -> int:
    request = json.loads(sys.stdin.read() or "{}")
    action = request.get("action", "")
    handler = ACTIONS.get(action)
    if handler is None:
        print(f"unknown action {action!r}", file=sys.stderr)
        return 2
    json.dump(handler(Config.load()), sys.stdout, ensure_ascii=False)
    return 0
