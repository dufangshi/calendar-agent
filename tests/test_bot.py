"""Telegram handling with a fake bot, store in a temp dir and no network."""

import json

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Never reach a real Supervisor from tests.
    monkeypatch.delenv("POCKYMOE_URL", raising=False)
    monkeypatch.delenv("POCKYMOE_TOKEN", raising=False)
    monkeypatch.setenv("CALENDAR_AGENT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CALENDAR_AGENT_DATA_DIR", str(tmp_path / "data"))
    import importlib

    from calendar_agent import config, store

    importlib.reload(config)
    importlib.reload(store)
    from calendar_agent import agent, bot, conversation, extract, items

    for module in (conversation, items, extract, agent, bot):
        importlib.reload(module)
    cfg = config.Config(
        apple_id="me@example.com", telegram_owner_id=42, model_connection_id="conn"
    )
    return cfg, bot, store, items, extract, agent, monkeypatch


class FakeBot:
    def __init__(self):
        self.sent, self.edits, self.answers, self.next_id = [], [], [], 100

    def send(self, chat_id, text, buttons=None, reply_to=None):
        self.next_id += 1
        self.sent.append(
            {
                "chat": chat_id,
                "text": text,
                "buttons": buttons,
                "reply_to": reply_to,
                "id": self.next_id,
            }
        )
        return {"chat": {"id": chat_id}, "message_id": self.next_id}

    def edit(self, chat_id, message_id, text, buttons=None):
        self.edits.append({"id": message_id, "text": text, "buttons": buttons})

    def answer(self, callback_id, text=""):
        self.answers.append(text)

    def typing(self, chat_id):
        pass


def message(text, mid=1, reply_to=None, sender=42):
    m = {
        "message_id": mid,
        "from": {"id": sender},
        "chat": {"id": sender, "type": "private"},
        "text": text,
    }
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return m


def test_message_becomes_card_and_approve_writes_then_undo(env):
    cfg, bot, store, items, extract, _agent, mp = env
    fake = FakeBot()
    mp.setattr(extract, "enabled", lambda c: True)
    mp.setattr(
        extract,
        "message",
        lambda c, t, *a: {
            "action": "propose",
            "items": [{"kind": "event", "title": "组会", "start": "2030-01-07T14:00"}],
        },
    )
    written = []
    mp.setattr(
        items,
        "apply",
        lambda item, c: (
            written.append(item)
            or {"kind": "event", "href": "h", "uid": "u", "calendar": "Agent"}
        ),
    )
    mp.setattr(items, "undo", lambda result, c: written.remove(written[0]))
    conn = store.connect()

    bot._handle_text(conn, fake, cfg, message("明天两点组会"))
    card = fake.sent[-1]
    assert card["reply_to"] == 1 and "组会" in card["text"]
    pid = card["buttons"][0][0]["callback_data"].split(":")[1]

    bot._handle_callback(
        conn, fake, cfg, {"id": "c1", "data": f"a:{pid}", "from": {"id": 42}}
    )
    assert written and store.proposal(conn, pid)["status"] == "applied"
    assert (
        fake.edits[-1]["text"].startswith("✅")
        and "已加到日历「Agent」" in fake.edits[-1]["text"]
    )

    bot._handle_callback(
        conn, fake, cfg, {"id": "c2", "data": f"u:{pid}", "from": {"id": 42}}
    )
    assert not written and store.proposal(conn, pid)["status"] == "undone"


def test_reply_to_card_edits_it_in_place(env):
    cfg, bot, store, _items, extract, _agent, mp = env
    fake = FakeBot()
    mp.setattr(extract, "enabled", lambda c: True)
    mp.setattr(
        extract,
        "edit",
        lambda c, item, t: {
            "action": "propose",
            "items": [{**item, "start": "2030-01-07T15:00", "end": None}],
        },
    )
    conn = store.connect()
    sent = bot.send_cards(
        conn,
        fake,
        cfg,
        [{"kind": "event", "title": "组会", "start": "2030-01-07T14:00"}],
        "telegram",
        "",
    )
    pid = sent[0]["id"]
    card_id = fake.sent[-1]["id"]
    bot._handle_callback(
        conn, fake, cfg, {"id": "c", "data": f"e:{pid}", "from": {"id": 42}}
    )
    assert store.proposal(conn, pid)["status"] == "editing"
    bot._handle_text(conn, fake, cfg, message("改到三点", mid=7, reply_to=card_id))
    row = store.proposal(conn, pid)
    assert row["status"] == "pending" and json.loads(row["item"])["start"].startswith(
        "2030-01-07T15:00"
    )
    assert fake.edits[-1]["id"] == card_id


def test_planning_goes_to_agent_and_strangers_are_ignored(env):
    cfg, bot, store, _items, extract, agent, mp = env
    fake = FakeBot()
    forwarded = []
    mp.setattr(extract, "enabled", lambda c: True)
    mp.setattr(extract, "message", lambda c, t, *a: {"action": "agent"})
    mp.setattr(
        agent, "forward", lambda conn, c, text, mid, *a: forwarded.append((text, mid))
    )
    conn = store.connect()
    bot._handle_text(conn, fake, cfg, message("帮我规划这周", mid=9))
    assert forwarded == [("帮我规划这周", 9)]
    assert not bot._from_owner(cfg, {"message": message("hi", sender=7)})
    assert not bot._from_owner(
        cfg, {"message": {**message("hi"), "chat": {"id": 42, "type": "group"}}}
    )
    assert bot._from_owner(cfg, {"callback_query": {"from": {"id": 42}}})


def test_photo_then_follow_up_sends_the_picture_and_the_earlier_turns(env):
    cfg, bot, store, _items, extract, _agent, mp = env
    fake = FakeBot()
    fake.download = lambda file_id: b"\xff\xd8jpeg"
    calls = []

    def fake_message(c, text, context="", images=None):
        calls.append((text, context, images))
        return (
            {"action": "reply", "text": "收到"}
            if len(calls) == 1
            else {
                "action": "propose",
                "items": [
                    {
                        "kind": "event",
                        "title": "CSC108 A3 OH",
                        "start": "2030-01-07T14:00",
                    }
                ],
            }
        )

    mp.setattr(extract, "enabled", lambda c: True)
    mp.setattr(extract, "message", fake_message)
    conn = store.connect()
    photo = {
        **message("我是 Fangshi", mid=20),
        "photo": [{"file_id": "small"}, {"file_id": "big"}],
    }
    photo["caption"] = photo.pop("text")
    bot._handle_text(conn, fake, cfg, photo)
    assert calls[0][0] == "[图片 1] 我是 Fangshi" and len(calls[0][2]) == 1

    bot._handle_text(
        conn, fake, cfg, message("根据上面图中的日程，帮我添加 CSC108 A3 OH", mid=22)
    )
    _text, context, images = calls[1]
    assert "主人：[图片 1] 我是 Fangshi" in context and "你：收到" in context
    assert len(images) == 1 and images[0]["mediaType"] == "image/jpeg"
    assert "CSC108 A3 OH" in fake.sent[-1]["text"]
