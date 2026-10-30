"""Minimal Telegram Bot API client. The token never leaves this module."""

from __future__ import annotations

import requests


class TelegramError(Exception):
    pass


class Bot:
    def __init__(self, token: str):
        self._base = f"https://api.telegram.org/bot{token}"
        self._http = requests.Session()

    def _call(self, method: str, http_timeout: float = 30, **params):
        try:
            resp = self._http.post(
                f"{self._base}/{method}", json=params, timeout=http_timeout
            )
        except requests.RequestException as exc:
            # The exception text can embed the URL, and with it the token.
            raise TelegramError(f"{method}: {type(exc).__name__}") from None
        data = resp.json() if resp.content else {}
        if not data.get("ok"):
            raise TelegramError(
                f"{method}: {data.get('description', resp.status_code)}"
            )
        return data["result"]

    def updates(self, offset: int | None, wait: int) -> list[dict]:
        return self._call(
            "getUpdates",
            http_timeout=wait + 15,
            offset=offset,
            timeout=wait,
            allowed_updates=["message", "callback_query"],
        )

    def send(
        self, chat_id: int, text: str, buttons=None, reply_to: int | None = None
    ) -> dict:
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        if reply_to:
            params["reply_parameters"] = {
                "message_id": reply_to,
                "allow_sending_without_reply": True,
            }
        return self._call("sendMessage", **params)

    def edit(self, chat_id: int, message_id: int, text: str, buttons=None) -> None:
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {"inline_keyboard": buttons or []},
        }
        try:
            self._call("editMessageText", **params)
        except TelegramError as exc:
            if "message is not modified" not in str(exc):
                raise

    def answer(self, callback_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError:
            pass  # Expired callbacks are harmless.

    def typing(self, chat_id: int) -> None:
        try:
            self._call("sendChatAction", chat_id=chat_id, action="typing")
        except TelegramError:
            pass
