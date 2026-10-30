"""Telegram side of the plugin: owner messages in, confirmation cards out."""

from __future__ import annotations

import html
import json
import logging
from zoneinfo import ZoneInfo

from . import agent, extract, items, pockymoe, store
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


def _forward(conn, bot: Bot, cfg: Config, text: str, message_id: int) -> None:
    try:
        agent.forward(conn, cfg, text, message_id)
    except pockymoe.PockymoeError as exc:
        bot.send(
            cfg.telegram_owner_id,
            f"⚠️ 对话线程不可用：{html.escape(str(exc))}",
            reply_to=message_id,
        )


def _handle_text(conn, bot: Bot, cfg: Config, message: dict) -> None:
    text = (message.get("text") or message.get("caption") or "").strip()
    mid = message["message_id"]
    if not text:
        bot.send(cfg.telegram_owner_id, "目前只看得懂文字消息。", reply_to=mid)
        return
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

    replied = message.get("reply_to_message")
    if replied:
        row = store.proposal_by_message(
            conn, message["chat"]["id"], replied["message_id"]
        )
        if row and row["status"] in ("pending", "editing", "failed"):
            _edit(conn, bot, cfg, row, text, mid)
            return

    if not extract.enabled(cfg):
        _forward(conn, bot, cfg, text, mid)
        return
    try:
        result = extract.message(cfg, text)
    except Exception as exc:  # noqa: BLE001 - any model failure falls back to the agent
        LOGGER.warning("extraction failed: %s", exc)
        result = {"action": "agent"}
    if result["action"] == "propose" and result.get("items"):
        sent = send_cards(conn, bot, cfg, result["items"], "telegram", text, mid)
        if not any(r["ok"] for r in sent):
            _forward(conn, bot, cfg, text, mid)
    elif result["action"] == "reply" and result.get("text"):
        bot.send(cfg.telegram_owner_id, html.escape(result["text"]), reply_to=mid)
    else:
        _forward(conn, bot, cfg, text, mid)


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
        bot.send(cfg.telegram_owner_id, html.escape(text[:3800]), reply_to=reply_to)
    return len(replies)


def tick(cfg: Config, wait: int = 45) -> dict:
    secrets = Secrets.load()
    if not configured(cfg, secrets):
        return {"telegram": "notConfigured"}
    bot = bot_for(secrets)
    conn = store.connect()
    handled = relayed = 0
    relayed += relay_agent_replies(conn, bot, cfg)
    # Keep polls short while the agent is working, so its reply goes out promptly.
    poll = 10 if store.get(conn, "agentPending") else wait
    try:
        updates = bot.updates(store.get(conn, "tgOffset"), poll)
    except TelegramError as exc:
        return {"telegram": "error", "error": str(exc)}
    for update in updates:
        # Advance first: a message that crashes the handler is not retried forever.
        store.put(conn, "tgOffset", update["update_id"] + 1)
        if not _from_owner(cfg, update):
            continue
        try:
            if "callback_query" in update:
                _handle_callback(conn, bot, cfg, update["callback_query"])
            elif "message" in update:
                _handle_text(conn, bot, cfg, update["message"])
            handled += 1
        except Exception as exc:
            LOGGER.exception("update failed")
            try:
                bot.send(
                    cfg.telegram_owner_id, f"⚠️ 出错了：{html.escape(str(exc)[:300])}"
                )
            except TelegramError:
                pass
    relayed += relay_agent_replies(conn, bot, cfg)
    return {"telegram": "ok", "handled": handled, "relayed": relayed}
