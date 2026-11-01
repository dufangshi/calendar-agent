"""Telegram side of the plugin: owner messages in, confirmation cards out."""

from __future__ import annotations

import html
import json
import logging
import time
from zoneinfo import ZoneInfo

from . import agent, conversation, extract, items, pockymoe, store
from .config import Config, Secrets
from .telegram import Bot, TelegramError

LOGGER = logging.getLogger(__name__)

HELP = (
    "直接发给我要安排的事就行，例如：\n"
    "· 明天下午三点和 Alice 开会一小时\n"
    "· 周五 5 点前交报告，提前一天提醒我\n"
    "· 帮我规划一下这周的复习时间\n"
    "我会先发卡片，你点「添加」才会写进 iCloud。"
)


def bot_for(secrets: Secrets) -> Bot:
    return Bot(secrets.telegram_bot_token)


def configured(cfg: Config, secrets: Secrets) -> bool:
    return bool(secrets.telegram_bot_token and cfg.telegram_owner_id)


def send_cards(
    conn,
    bot: Bot,
    cfg: Config,
    raw_items: list,
    source: str,
    source_text: str,
    reply_to: int | None = None,
) -> list[dict]:
    """Validate items and send one card each. Used for Telegram and the agent's `propose`."""
    tz = ZoneInfo(cfg.timezone)
    results = []
    for raw in raw_items[:10]:
        try:
            item = items.normalize(raw, tz)
        except items.InvalidItem as exc:
            results.append({"ok": False, "error": str(exc), "item": raw})
            continue
        pid = store.add_proposal(conn, item["kind"], item, source, source_text)
        sent = bot.send(
            cfg.telegram_owner_id,
            items.card(item, tz),
            items.buttons(pid, "pending"),
            reply_to,
        )
        store.update_proposal(
            conn, pid, chat_id=sent["chat"]["id"], message_id=sent["message_id"]
        )
        results.append({"ok": True, "id": pid, "title": item["title"]})
        conversation.record(
            conn,
            cfg.telegram_owner_id,
            "bot",
            f"发了确认卡片：{item['title']}（{items.when_text(item, tz)}）",
        )
    return results


def _refresh(conn, bot: Bot, cfg: Config, pid: str, note: str = "") -> None:
    row = store.proposal(conn, pid)
    item = json.loads(row["item"])
    bot.edit(
        row["chat_id"],
        row["message_id"],
        items.card(item, ZoneInfo(cfg.timezone), row["status"], note),
        items.buttons(pid, row["status"]),
    )


def _forward(
    conn,
    bot: Bot,
    cfg: Config,
    text: str,
    message_id: int,
    context: str = "",
    images=None,
) -> None:
    try:
        agent.forward(conn, cfg, text, message_id, context, images)
    except pockymoe.PockymoeError as exc:
        bot.send(
            cfg.telegram_owner_id,
            f"⚠️ 对话线程不可用：{html.escape(str(exc))}",
            reply_to=message_id,
        )


def _incoming_image(bot: Bot, message: dict) -> str | None:
    """Save the photo (or image file) of a message; returns its media name."""
    file_id, suffix = None, ".jpg"
    if message.get("photo"):
        file_id = message["photo"][-1]["file_id"]  # the largest size
    document = message.get("document") or {}
    if document.get("mime_type", "").startswith("image/"):
        file_id = document["file_id"]
        suffix = ".png" if document["mime_type"] == "image/png" else ".jpg"
    if not file_id:
        return None
    return conversation.save_image(message["message_id"], bot.download(file_id), suffix)


def _say(
    conn,
    bot: Bot,
    cfg: Config,
    text: str,
    reply_to: int | None = None,
    plain: bool = True,
) -> None:
    bot.send(
        cfg.telegram_owner_id, html.escape(text) if plain else text, reply_to=reply_to
    )
    conversation.record(conn, cfg.telegram_owner_id, "bot", text)


def _handle_text(conn, bot: Bot, cfg: Config, message: dict) -> None:
    text = (message.get("text") or message.get("caption") or "").strip()
    mid = message["message_id"]
    chat = message["chat"]["id"]
    if text in ("/start", "/help"):
        bot.send(cfg.telegram_owner_id, HELP)
        return
    if text == "/pending":
        rows = conn.execute(
            "SELECT id FROM proposals WHERE status IN ('pending','editing') ORDER BY created_at"
        ).fetchall()
        if not rows:
            bot.send(cfg.telegram_owner_id, "没有待确认的卡片。")
        for row in rows:
            p = store.proposal(conn, row["id"])
            bot.send(
                cfg.telegram_owner_id,
                items.card(json.loads(p["item"]), ZoneInfo(cfg.timezone), p["status"]),
                items.buttons(p["id"], p["status"]),
            )
        return
    bot.typing(cfg.telegram_owner_id)
    image = _incoming_image(bot, message)
    if not text and not image:
        bot.send(cfg.telegram_owner_id, "目前只看得懂文字和图片。", reply_to=mid)
        return
    context = conversation.recent(conn, chat, mid)
    conversation.record(conn, chat, "owner", text, mid, image)
    images = context.images + ([conversation.MEDIA_DIR / image] if image else [])
    images = images[-conversation.MAX_IMAGES :]
    shown = text or "（只发了图片）"
    if image:
        shown = f"[图片 {len(images)}] {text}".strip()

    replied = message.get("reply_to_message")
    if replied and not image:
        row = store.proposal_by_message(conn, chat, replied["message_id"])
        if row and row["status"] in ("pending", "editing", "failed"):
            _edit(conn, bot, cfg, row, text, mid)
            return

    if not extract.enabled(cfg):
        _forward(conn, bot, cfg, shown, mid, context.transcript, images)
        return
    try:
        result = extract.message(
            cfg, shown, context.transcript, conversation.encoded(images)
        )
    except Exception as exc:  # noqa: BLE001 - any model failure falls back to the agent
        LOGGER.warning("extraction failed: %s", exc)
        result = {"action": "agent"}
    if result["action"] == "propose" and result.get("items"):
        sent = send_cards(conn, bot, cfg, result["items"], "telegram", text, mid)
        if not any(r["ok"] for r in sent):
            _forward(conn, bot, cfg, shown, mid, context.transcript, images)
    elif result["action"] == "reply" and result.get("text"):
        _say(conn, bot, cfg, result["text"], mid)
    else:
        _forward(conn, bot, cfg, shown, mid, context.transcript, images)


def _edit(conn, bot: Bot, cfg: Config, row, text: str, mid: int) -> None:
    item = json.loads(row["item"])
    tz = ZoneInfo(cfg.timezone)
    if extract.enabled(cfg):
        try:
            result = extract.edit(cfg, item, text)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("edit extraction failed: %s", exc)
            result = {"action": "agent"}
        if result["action"] == "propose" and result.get("items"):
            try:
                updated = items.normalize(result["items"][0], tz)
            except items.InvalidItem as exc:
                bot.send(
                    cfg.telegram_owner_id,
                    f"没改成：{html.escape(str(exc))}",
                    reply_to=mid,
                )
                return
            store.update_proposal(
                conn, row["id"], item=updated, kind=updated["kind"], status="pending"
            )
            _refresh(conn, bot, cfg, row["id"], "<i>已按你的意思改好</i>")
            return
    store.update_proposal(conn, row["id"], status="rejected")
    _refresh(conn, bot, cfg, row["id"])
    _forward(
        conn,
        bot,
        cfg,
        f"主人要修改这张卡片（旧卡片已作废，请 propose 一张新的）：\n{json.dumps(item, ensure_ascii=False)}\n修改要求：{text}",
        mid,
    )


def _handle_callback(conn, bot: Bot, cfg: Config, query: dict) -> None:
    action, _, pid = (query.get("data") or "").partition(":")
    row = store.proposal(conn, pid)
    if row is None:
        bot.answer(query["id"], "这张卡片已失效")
        return
    status = row["status"]
    if action == "a" and status in ("pending", "editing", "failed"):
        bot.answer(query["id"], "正在添加…")
        # Visible at once; writing to iCloud takes a few seconds.
        store.update_proposal(conn, pid, status="adding")
        _refresh(conn, bot, cfg, pid)
        try:
            result = items.apply(json.loads(row["item"]), cfg)
        except Exception as exc:  # noqa: BLE001 - shown on the card, retry stays possible
            LOGGER.warning("apply failed: %s", exc)
            store.update_proposal(conn, pid, status="failed", error=str(exc))
            _refresh(
                conn, bot, cfg, pid, f"<i>写入失败：{html.escape(str(exc)[:200])}</i>"
            )
            return
        store.update_proposal(conn, pid, status="applied", result=result, error=None)
        _refresh(conn, bot, cfg, pid, f"<i>{items.where_text(result)}</i>")
    elif action == "r" and status in ("pending", "editing", "failed"):
        store.update_proposal(conn, pid, status="rejected")
        bot.answer(query["id"], "好，不加")
        _refresh(conn, bot, cfg, pid)
    elif action == "e" and status in ("pending", "failed"):
        store.update_proposal(conn, pid, status="editing")
        bot.answer(query["id"], "回复这张卡片说怎么改")
        _refresh(conn, bot, cfg, pid)
    elif action == "u" and status == "applied":
        try:
            items.undo(json.loads(row["result"]), cfg)
        except Exception as exc:  # noqa: BLE001
            bot.answer(query["id"], "撤销失败")
            _refresh(
                conn, bot, cfg, pid, f"<i>撤销失败：{html.escape(str(exc)[:200])}</i>"
            )
            return
        store.update_proposal(conn, pid, status="undone")
        bot.answer(query["id"], "已撤销")
        _refresh(conn, bot, cfg, pid)
    else:
        bot.answer(query["id"])


def _from_owner(cfg: Config, update: dict) -> bool:
    if "callback_query" in update:
        return update["callback_query"]["from"]["id"] == cfg.telegram_owner_id
    message = update.get("message") or {}
    return (
        message.get("from", {}).get("id") == cfg.telegram_owner_id
        and message.get("chat", {}).get("type") == "private"
    )


def relay_agent_replies(conn, bot: Bot, cfg: Config) -> int:
    if not store.get(conn, "agentPending") or not pockymoe.available():
        return 0
    try:
        replies = agent.finished_replies(conn)
    except pockymoe.PockymoeError as exc:
        LOGGER.warning("agent poll failed: %s", exc)
        return 0
    for reply_to, text in replies:
        _say(conn, bot, cfg, text[:3800], reply_to)
    return len(replies)


def _enrollment(cfg: Config, secrets: Secrets) -> dict:
    """Bot token set, owner not yet: tell each private sender their user id, nothing else."""
    bot = bot_for(secrets)
    conn = store.connect()
    try:
        updates = bot.updates(store.get(conn, "tgOffset"), 20)
    except TelegramError as exc:
        return {"telegram": "error", "error": str(exc)}
    for update in updates:
        store.put(conn, "tgOffset", update["update_id"] + 1)
        message = update.get("message") or {}
        if message.get("chat", {}).get("type") == "private" and message.get("from"):
            user = message["from"]["id"]
            bot.send(
                user,
                f"你的 Telegram user id 是 <code>{user}</code>。\n把这个数字发给 Pockymoe 里的日程管家，它会把你设为唯一能使用这个机器人的人。",
            )
    return {"telegram": "enrolling", "seen": len(updates)}


def _handle_update(conn, bot: Bot, cfg: Config, update: dict) -> bool:
    # Advance first: a message that crashes the handler is not retried forever.
    store.put(conn, "tgOffset", update["update_id"] + 1)
    if not _from_owner(cfg, update):
        return False
    try:
        if "callback_query" in update:
            _handle_callback(conn, bot, cfg, update["callback_query"])
        elif "message" in update:
            _handle_text(conn, bot, cfg, update["message"])
    except Exception as exc:
        LOGGER.exception("update failed")
        try:
            bot.send(cfg.telegram_owner_id, f"⚠️ 出错了：{html.escape(str(exc)[:300])}")
        except TelegramError:
            pass
    return True


def tick(cfg: Config, budget: float = 52) -> dict:
    """Listen for about a minute, so the next scheduled tick takes over within
    seconds and a button press is answered right away."""
    secrets = Secrets.load()
    if secrets.telegram_bot_token and not cfg.telegram_owner_id:
        return _enrollment(cfg, secrets)
    if not configured(cfg, secrets):
        return {"telegram": "notConfigured"}
    bot = bot_for(secrets)
    conn = store.connect()
    handled = relayed = 0
    deadline = time.monotonic() + budget
    while True:
        relayed += relay_agent_replies(conn, bot, cfg)
        left = int(deadline - time.monotonic())
        if left < 2:
            break
        # Short polls while the agent works, so its reply goes out promptly.
        poll = min(left, 10 if store.get(conn, "agentPending") else 45)
        try:
            updates = bot.updates(store.get(conn, "tgOffset"), poll)
        except TelegramError as exc:
            return {"telegram": "error", "error": str(exc), "handled": handled}
        for update in updates:
            handled += _handle_update(conn, bot, cfg, update)
    return {"telegram": "ok", "handled": handled, "relayed": relayed}
