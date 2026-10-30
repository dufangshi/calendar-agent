"""Proposed calendar events and reminders: validation, Telegram cards, iCloud writes.

Item JSON (times are local wall-clock in the configured timezone):
  event:    {"kind":"event","title","start":"2026-10-31T14:00","end"?,"allDay"?,
             "location"?,"notes"?,"alertsMinutes"?:[15]}
  reminder: {"kind":"reminder","title","due"?:"2026-10-31T17:00","allDay"?,
             "notes"?,"earlyHours"?:[1],"priority"?:"high"|"medium"|"low"}
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import calendar_caldav, reminders_icloud
from .config import Config, Secrets

WEEKDAYS = "一二三四五六日"
PRIORITY = {"high": 1, "medium": 5, "low": 9}
PRIORITY_TEXT = {"high": "高", "medium": "中", "low": "低"}


class InvalidItem(ValueError):
    pass


def _time(value, tz: ZoneInfo, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise InvalidItem(f"{field} 不是有效时间: {value}") from None
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)


def _ints(value, low: int, high: int) -> list[int]:
    out = []
    for v in value or []:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if low <= n <= high and n not in out:
            out.append(n)
    return sorted(out)[:5]


def normalize(raw: dict, tz: ZoneInfo) -> dict:
    """Validate and canonicalize an item from the model, the agent or an edit."""
    kind = raw.get("kind")
    title = str(raw.get("title") or "").strip()[:200]
    if not title:
        raise InvalidItem("缺少标题")
    notes = str(raw.get("notes") or "").strip()[:2000]
    all_day = bool(raw.get("allDay"))
    if kind == "event":
        start = _time(raw.get("start"), tz, "start")
        if start is None:
            raise InvalidItem("日程缺少开始时间")
        end = _time(raw.get("end"), tz, "end") or start + (
            timedelta(days=1) if all_day else timedelta(hours=1)
        )
        if end <= start:
            raise InvalidItem("结束时间早于开始时间")
        item = {
            "kind": "event",
            "title": title,
            "start": start.isoformat(timespec="minutes"),
            "end": end.isoformat(timespec="minutes"),
            "allDay": all_day,
            "alertsMinutes": []
            if all_day
            else _ints(raw.get("alertsMinutes"), 0, 40320),
        }
        if raw.get("location"):
            item["location"] = str(raw["location"]).strip()[:200]
    elif kind == "reminder":
        due = _time(raw.get("due"), tz, "due")
        item = {
            "kind": "reminder",
            "title": title,
            "due": due.isoformat(timespec="minutes") if due else None,
            "allDay": all_day and due is not None,
            "earlyHours": []
            if (due is None or all_day)
            else _ints(raw.get("earlyHours"), 1, 720),
        }
        if raw.get("priority") in PRIORITY:
            item["priority"] = raw["priority"]
    else:
        raise InvalidItem(f"未知类型: {kind}")
    if notes:
        item["notes"] = notes
    return item


def _day(d: datetime, today: datetime) -> str:
    label = f"{d.month}月{d.day}日 周{WEEKDAYS[d.weekday()]}"
    delta = (d.date() - today.date()).days
    return {0: f"今天 {label}", 1: f"明天 {label}", 2: f"后天 {label}"}.get(
        delta, label
    )


def _alerts_minutes(minutes: list[int]) -> str:
    def one(m: int) -> str:
        if m == 0:
            return "开始时"
        if m % 1440 == 0:
            return f"提前 {m // 1440} 天"
        if m % 60 == 0:
            return f"提前 {m // 60} 小时"
        return f"提前 {m} 分钟"

    return "、".join(one(m) for m in minutes)


def when_text(item: dict, tz: ZoneInfo) -> str:
    today = datetime.now(tz)
    if item["kind"] == "event":
        start = datetime.fromisoformat(item["start"])
        end = datetime.fromisoformat(item["end"])
        if item.get("allDay"):
            last = end - timedelta(days=1)
            if last.date() <= start.date():
                return f"{_day(start, today)} · 全天"
            return f"{_day(start, today)} – {_day(last, today)} · 全天"
        if end.date() == start.date():
            return f"{_day(start, today)} {start:%H:%M}–{end:%H:%M}"
        return f"{_day(start, today)} {start:%H:%M} – {_day(end, today)} {end:%H:%M}"
    if not item.get("due"):
        return "无截止时间"
    due = datetime.fromisoformat(item["due"])
    return f"截止 {_day(due, today)}" + ("" if item.get("allDay") else f" {due:%H:%M}")


def card(item: dict, tz: ZoneInfo, status: str = "pending", note: str = "") -> str:
    """Short HTML card. Status changes only the first line."""
    e = html.escape
    icon = "📅" if item["kind"] == "event" else "☑️"
    title = f"<b>{e(item['title'])}</b>"
    head = {
        "pending": f"{icon} {title}",
        "editing": f"{icon} {title}\n<i>✏️ 回复这条消息，说要怎么改</i>",
        "applied": f"✅ {title}",
        "rejected": f"<s>{e(item['title'])}</s> · 已忽略",
        "undone": f"<s>{e(item['title'])}</s> · 已撤销",
        "failed": f"⚠️ {title}",
    }[status]
    lines = [head, when_text(item, tz)]
    if status in ("rejected", "undone"):
        return "\n".join(lines)
    if item.get("location"):
        lines.append(f"📍 {e(item['location'])}")
    if item["kind"] == "event" and item.get("alertsMinutes"):
        lines.append(f"⏰ {_alerts_minutes(item['alertsMinutes'])}")
    if item["kind"] == "reminder" and item.get("earlyHours"):
        lines.append(
            f"⏰ 到点提醒、{_alerts_minutes([h * 60 for h in item['earlyHours']])}"
        )
    elif item["kind"] == "reminder" and item.get("due") and not item.get("allDay"):
        lines.append("⏰ 到点提醒")
    if item.get("priority"):
        lines.append(f"优先级 {PRIORITY_TEXT[item['priority']]}")
    if item.get("notes"):
        notes = item["notes"]
        lines.append(f"<i>{e(notes[:160] + ('…' if len(notes) > 160 else ''))}</i>")
    if note:
        lines.append(note)
    return "\n".join(lines)


def buttons(pid: str, status: str) -> list:
    if status in ("pending", "editing", "failed"):
        return [
            [
                {"text": "✅ 添加", "callback_data": f"a:{pid}"},
                {"text": "✏️ 修改", "callback_data": f"e:{pid}"},
                {"text": "✖️ 不要", "callback_data": f"r:{pid}"},
            ]
        ]
    if status == "applied":
        return [[{"text": "↩️ 撤销", "callback_data": f"u:{pid}"}]]
    return []


def apply(item: dict, cfg: Config) -> dict:
    """Write to iCloud. Returns where it went, for undo."""
    tz = ZoneInfo(cfg.timezone)
    secrets = Secrets.load()
    if item["kind"] == "event":
        principal = calendar_caldav.connect(cfg.apple_id, secrets.caldav_app_password)
        calendar = calendar_caldav.get_or_create_calendar(principal, cfg.calendar_name)
        uid, href = calendar_caldav.add_event(
            calendar,
            calendar_caldav.EventSpec(
                summary=item["title"],
                start=datetime.fromisoformat(item["start"]),
                end=datetime.fromisoformat(item["end"]),
                description=item.get("notes", ""),
                location=item.get("location", ""),
                all_day=bool(item.get("allDay")),
                alerts_minutes=tuple(item.get("alertsMinutes") or ()),
                source="telegram",
            ),
            tz,
        )
        return {
            "kind": "event",
            "href": href,
            "uid": uid,
            "calendar": cfg.calendar_name,
        }
    api, probe = reminders_icloud.open_service(cfg, secrets)
    if api is None:
        raise RuntimeError(f"提醒事项不可用：{probe.reason}")
    lst = reminders_icloud.pick_list(api, cfg.reminders_list)
    reminder = reminders_icloud.create_reminder(
        api,
        lst.id,
        item["title"],
        datetime.fromisoformat(item["due"]) if item.get("due") else None,
        cfg.timezone,
        notes=item.get("notes", ""),
        early_hours=tuple(item.get("earlyHours") or ()),
        all_day=bool(item.get("allDay")),
        priority=PRIORITY.get(item.get("priority", ""), 0),
    )
    return {"kind": "reminder", "id": reminder.id, "list": lst.title}


def undo(result: dict, cfg: Config) -> None:
    secrets = Secrets.load()
    if result["kind"] == "event":
        principal = calendar_caldav.connect(cfg.apple_id, secrets.caldav_app_password)
        calendar = calendar_caldav.get_or_create_calendar(principal, cfg.calendar_name)
        calendar_caldav.delete_event(calendar, result["href"])
        return
    api, probe = reminders_icloud.open_service(cfg, secrets)
    if api is None:
        raise RuntimeError(f"提醒事项不可用：{probe.reason}")
    api.reminders.delete(api.reminders.get(result["id"]))


def where_text(result: dict) -> str:
    if result["kind"] == "event":
        return f"已加到日历「{html.escape(result['calendar'])}」"
    return f"已加到提醒事项「{html.escape(result['list'])}」"
