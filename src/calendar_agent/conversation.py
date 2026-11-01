"""Recent Telegram conversation, so a message can refer to an earlier one
("根据上面图中的日程…"). Images are kept on disk next to the store."""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import store
from .config import DATA_DIR, ensure_data_dirs

MEDIA_DIR = DATA_DIR / "media"
WINDOW_SECONDS = 30 * 60
MAX_TURNS = 10
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  message_id INTEGER,
  role TEXT NOT NULL,          -- owner | bot
  text TEXT NOT NULL DEFAULT '',
  image TEXT,                  -- file name under MEDIA_DIR
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, at);
"""


@dataclass
class Context:
    """What the model sees: the transcript text and the images it mentions."""

    transcript: str = ""
    images: list[Path] = field(default_factory=list)


def _ensure(conn) -> None:
    conn.executescript(SCHEMA)


def save_image(message_id: int, data: bytes, suffix: str = ".jpg") -> str:
    ensure_data_dirs()
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.chmod(0o700)
    name = f"{message_id}{suffix}"
    (MEDIA_DIR / name).write_bytes(data)
    return name


def record(
    conn,
    chat_id: int,
    role: str,
    text: str,
    message_id: int | None = None,
    image: str | None = None,
) -> None:
    _ensure(conn)
    conn.execute(
        "INSERT INTO messages(chat_id,message_id,role,text,image,at) VALUES(?,?,?,?,?,?)",
        (chat_id, message_id, role, text[:4000], image, time.time()),
    )
    # Old turns and their pictures are not needed once out of every window.
    old = conn.execute(
        "SELECT image FROM messages WHERE at < ? AND image IS NOT NULL",
        (time.time() - 7 * 86400,),
    ).fetchall()
    for row in old:
        (MEDIA_DIR / row["image"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM messages WHERE at < ?", (time.time() - 7 * 86400,))
    conn.commit()


def recent(conn, chat_id: int, before_message_id: int) -> Context:
    """Turns in the last half hour before this message, oldest first."""
    _ensure(conn)
    rows = conn.execute(
        "SELECT role,text,image FROM messages WHERE chat_id=? AND at>=? AND (message_id IS NULL OR message_id<>?) ORDER BY id DESC LIMIT ?",
        (chat_id, time.time() - WINDOW_SECONDS, before_message_id, MAX_TURNS),
    ).fetchall()
    lines, images = [], []
    for row in reversed(rows):
        who = "主人" if row["role"] == "owner" else "你"
        text = row["text"]
        if row["image"]:
            path = MEDIA_DIR / row["image"]
            if path.exists():
                images.append(path)
                text = f"[图片 {len(images)}] {text}".strip()
        if text:
            lines.append(f"{who}：{text}")
    return Context("\n".join(lines), images[-MAX_IMAGES:])


def encoded(paths: list[Path]) -> list[dict]:
    """Images for Pockymoe modelComplete, newest last, within size limits."""
    out = []
    for path in paths[-MAX_IMAGES:]:
        data = path.read_bytes()
        if len(data) > MAX_IMAGE_BYTES:
            continue
        kind = "image/png" if path.suffix == ".png" else "image/jpeg"
        out.append({"mediaType": kind, "data": base64.b64encode(data).decode()})
    return out


def connect():
    conn = store.connect()
    _ensure(conn)
    return conn
