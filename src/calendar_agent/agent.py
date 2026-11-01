"""The conversation thread the plugin keeps in Pockymoe (DeepSeek Harness by default).

Telegram messages that need more than a one-shot extraction are queued to it.
Its final reply of each turn is relayed back to Telegram; it proposes calendar
items through `calendar-agent propose`, which sends cards for the owner to confirm.
"""

from __future__ import annotations

import os
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import pockymoe, store
from .config import DATA_DIR, Config

WORKSPACE = DATA_DIR / "agent"
SKILL_TEMPLATE = Path(__file__).with_name("agent_skill.md")


def command() -> str:
    """How the thread runs this CLI: the installed plugin, or this checkout."""
    project = os.environ.get("POCKYMOE_PLUGIN_DIR") or str(
        Path(__file__).resolve().parents[2]
    )
    return f"uv run --quiet --project {shlex.quote(project)} calendar-agent"


def write_skill() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    text = SKILL_TEMPLATE.read_text().replace("{{CA}}", command())
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = WORKSPACE / name
        if not path.exists() or path.read_text() != text:
            path.write_text(text)


def ensure_thread(conn, cfg: Config) -> str:
    write_skill()
    thread = store.get(conn, "agentThreadId")
    if thread:
        try:
            status = pockymoe.call("status", threadId=thread)
            if not status.get("closedAt"):
                return thread
        except pockymoe.PockymoeError:
            pass
    workspace = pockymoe.call(
        "workspaceEnsure", absPath=str(WORKSPACE), label="日程管家"
    )
    created = pockymoe.call(
        "create",
        timeout=180,
        workspaceId=workspace["id"],
        provider=cfg.agent_provider,
        agentId=cfg.agent_id or None,
        model=cfg.agent_model or "default",
        title="日程管家",
        approvalMode="yolo",
    )
    thread = created.get("threadId") or created.get("id") or created["thread"]["id"]
    store.put(conn, "agentThreadId", thread)
    return thread


def forward(
    conn,
    cfg: Config,
    text: str,
    reply_to: int,
    context: str = "",
    images: list[Path] | None = None,
) -> None:
    thread = ensure_thread(conn, cfg)
    sent_at = datetime.now(UTC).isoformat()
    body = f"[Telegram] {text}"
    if context:
        body = f"[Telegram] 最近的对话（旧→新）：\n{context}\n\n当前消息：{text}"
    if images:
        # The thread works in WORKSPACE; copies there are readable with its file tools.
        inbox = WORKSPACE / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        names = []
        for i, path in enumerate(images, 1):
            target = inbox / path.name
            target.write_bytes(path.read_bytes())
            names.append(f"图片 {i}: inbox/{path.name}")
        body += "\n\n附图（在你的工作目录里，用读文件/看图工具打开）：\n" + "\n".join(
            names
        )
    pockymoe.call(
        "send",
        threadId=thread,
        text=body,
        delivery="queue",
        kind="task",
        subject="Telegram 消息",
        clientRequestId=f"tg-{reply_to}",
    )
    pending = store.get(conn, "agentPending", [])
    pending.append({"at": sent_at, "replyTo": reply_to})
    store.put(conn, "agentPending", pending)


def finished_replies(conn) -> list[tuple[int, str]]:
    """(telegram message id, reply text) for forwarded messages whose turn ended."""
    pending = store.get(conn, "agentPending", [])
    thread = store.get(conn, "agentThreadId")
    if not pending or not thread:
        return []
    turns = pockymoe.call(
        "transcript", threadId=thread, limit=max(3, len(pending) + 1)
    )["turns"]
    out, left, used = [], [], set()
    for entry in pending:
        since = datetime.fromisoformat(entry["at"]) - timedelta(seconds=5)
        turn = next(
            (
                t
                for t in turns
                if t["id"] not in used
                and t.get("startedAt")
                and datetime.fromisoformat(t["startedAt"]) >= since
            ),
            None,
        )
        if turn is None or turn["status"] in ("inProgress", "recovering", "queued"):
            # Give up on a reply after a day; the thread keeps the history.
            if datetime.now(UTC) - datetime.fromisoformat(entry["at"]) < timedelta(
                days=1
            ):
                left.append(entry)
            continue
        used.add(turn["id"])
        messages = [
            i["text"]
            for i in turn["items"]
            if i.get("kind") == "agentMessage" and i.get("text")
        ]
        if turn["status"] != "completed":
            text = "⚠️ 这条没处理完，稍后再试，或者换个说法。"
        else:
            text = messages[-1] if messages else ""
        if text:
            out.append((entry["replyTo"], text))
    store.put(conn, "agentPending", left)
    return out
