from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from calendar_agent import items

TZ = ZoneInfo("America/Toronto")


def test_event_defaults_and_card():
    item = items.normalize(
        {
            "kind": "event",
            "title": " 组会 ",
            "start": "2030-01-07T14:00",
            "alertsMinutes": [15, "x", 15],
        },
        TZ,
    )
    assert item["end"].startswith("2030-01-07T15:00")
    assert item["alertsMinutes"] == [15]
    card = items.card(item, TZ)
    assert card.splitlines()[0] == "📅 <b>组会</b>"
    assert "1月7日 周一 14:00–15:00" in card
    assert "⏰ 提前 15 分钟" in card
    assert [b["text"] for b in items.buttons("p", "pending")[0]] == [
        "✅ 添加",
        "✏️ 修改",
        "✖️ 不要",
    ]


def test_reminder_card_and_rejected_is_two_lines():
    item = items.normalize(
        {
            "kind": "reminder",
            "title": "交 <A2>",
            "due": "2030-01-09T17:00",
            "earlyHours": [24],
            "priority": "high",
        },
        TZ,
    )
    card = items.card(item, TZ)
    assert "&lt;A2&gt;" in card, "titles are HTML-escaped"
    assert "⏰ 到点提醒、提前 1 天" in card
    assert "优先级 高" in card
    assert len(items.card(item, TZ, "rejected").splitlines()) == 2
    assert items.buttons("p", "rejected") == []
    assert items.buttons("p", "applied")[0][0]["callback_data"] == "u:p"


def test_dateless_reminder_and_relative_day_labels():
    assert items.normalize({"kind": "reminder", "title": "买牛奶"}, TZ)["due"] is None
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).replace(hour=9, minute=0)
    item = items.normalize(
        {"kind": "event", "title": "x", "start": tomorrow.strftime("%Y-%m-%dT%H:%M")},
        TZ,
    )
    assert items.when_text(item, TZ).startswith("明天 ")


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "event", "title": "x"},
        {
            "kind": "event",
            "title": "x",
            "start": "2030-01-07T14:00",
            "end": "2030-01-07T13:00",
        },
        {"kind": "event", "title": "", "start": "2030-01-07T14:00"},
        {"kind": "task", "title": "x"},
        {"kind": "reminder", "title": "x", "due": "tomorrow"},
    ],
)
def test_invalid_items(raw):
    with pytest.raises(items.InvalidItem):
        items.normalize(raw, TZ)


def test_reminder_list_is_kept_and_shown_and_adding_has_no_buttons():
    item = items.normalize(
        {
            "kind": "reminder",
            "title": "交 A2",
            "due": "2030-01-09T17:00",
            "list": "学校",
        },
        TZ,
    )
    assert item["list"] == "学校"
    assert "🗂 学校" in items.card(item, TZ)
    assert "正在添加" in items.card(item, TZ, "adding")
    assert items.buttons("p", "adding") == []


def test_setup_keys_documented_for_the_agent_are_editable():
    from calendar_agent import cli

    for key in (
        "apple_id",
        "telegram_owner_id",
        "reminders_account_id",
        "reminders_person_id",
    ):
        assert key in cli.EDITABLE
