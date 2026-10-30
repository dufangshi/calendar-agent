"""One short model call that turns a Telegram message into proposals.

Anything that needs planning, a look at the existing calendar, configuration
or a conversation goes to the agent thread instead.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from . import pockymoe, rules
from .config import Config

SYSTEM = """你把用户发给日程助手的消息转成 JSON，只输出 JSON。
现在是 {now}（{tz}，周{weekday}）。
输出三种之一：
{{"action":"propose","items":[...]}}  用户明确要加日程或待办，信息足够
{{"action":"agent"}}  需要规划、查已有日程、改设置、多步讨论，或你不确定
{{"action":"reply","text":"..."}}  只需一句话回答，不涉及日历
item：
日程 {{"kind":"event","title":"","start":"YYYY-MM-DDTHH:MM","end":"可省","allDay":false,"location":"可省","notes":"可省","alertsMinutes":[15]}}
待办 {{"kind":"reminder","title":"","due":"YYYY-MM-DDTHH:MM 或 null","allDay":false,"notes":"可省","earlyHours":[]}}
规则：有起止时间的会议、约会、课程是日程；"提醒我做某事"、任务、截止是待办。
时间用本地时间，不带时区。没说提醒时：日程提前 15 分钟，待办只在到点提醒。
标题简短，用用户的语言。"""

EDIT = """这是一条待确认的{kind}：
{item}
用户要求修改：{text}
输出 {{"action":"propose","items":[修改后的这一条]}}；改不了就输出 {{"action":"agent"}}。"""


def _now(cfg: Config) -> tuple[str, str, str]:
    now = datetime.now(ZoneInfo(cfg.timezone))
    return now.strftime("%Y-%m-%d %H:%M"), cfg.timezone, "一二三四五六日"[now.weekday()]


def _json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("model returned no JSON")
    return json.loads(match.group(0))


def _ask(cfg: Config, user: str) -> dict:
    now, tz, weekday = _now(cfg)
    system = SYSTEM.format(now=now, tz=tz, weekday=weekday)
    owner_rules = rules.load()
    if owner_rules:
        system += "\n主人的长期规则：\n" + "\n".join(f"- {r}" for r in owner_rules[:30])
    text = pockymoe.complete(
        cfg.model_connection_id,
        cfg.model,
        system,
        user,
        max_tokens=800,
    )
    result = _json(text)
    if result.get("action") not in ("propose", "agent", "reply"):
        return {"action": "agent"}
    return result


def enabled(cfg: Config) -> bool:
    return bool(cfg.model_connection_id) and pockymoe.available()


def message(cfg: Config, text: str) -> dict:
    return _ask(cfg, text)


def edit(cfg: Config, item: dict, text: str) -> dict:
    kind = "日程" if item["kind"] == "event" else "待办"
    return _ask(
        cfg,
        EDIT.format(kind=kind, item=json.dumps(item, ensure_ascii=False), text=text),
    )
