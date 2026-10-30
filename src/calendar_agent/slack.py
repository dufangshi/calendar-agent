"""Slack through Slack's hosted MCP server, authorized as the owner (read-only).

OAuth uses the public client id of the Slack MCP connector that OpenAI's Codex
plugin publishes, with PKCE and a loopback redirect, so no Slack app has to be
installed. The owner opens the authorize link in any browser; after "Allow" the
browser lands on an unreachable http://127.0.0.1 page whose full address they
paste back (`auth-finish`). Tokens live in a 0600 file and refresh themselves.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as pysecrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from .config import CONFIG_DIR, DATA_DIR, ensure_data_dirs

CLIENT_ID = "11843774967.11905492103734"
AUTHORIZE_URL = "https://slack.com/oauth/v2_user/authorize"
TOKEN_URL = "https://slack.com/api/oauth.v2.user.access"
MCP_URL = "https://mcp.slack.com/mcp"
RESOURCE = "https://mcp.slack.com"
# Reading only: monitoring and triage never need to post or edit.
SCOPES = (
    "channels:history channels:read groups:history groups:read im:history im:read "
    "mpim:history mpim:read search:read.public search:read.private search:read.im "
    "search:read.mpim search:read.files search:read.users users:read users:read.email "
    "canvases:read files:read lists:read emoji:read reactions:read"
)

TOKEN_FILE = CONFIG_DIR / "slack.json"
PENDING_FILE = DATA_DIR / "slack-auth-pending.json"
PENDING_TTL = 900


class SlackError(Exception):
    pass


def _write_private(path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def auth_url() -> str:
    """Start an authorization and return the link for the owner to open."""
    ensure_data_dirs()
    verifier = pysecrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = pysecrets.token_urlsafe(24)
    port = 40000 + pysecrets.randbelow(20000)
    redirect = f"http://127.0.0.1:{port}/callback/{pysecrets.token_urlsafe(9)}"
    _write_private(
        PENDING_FILE,
        {"verifier": verifier, "state": state, "redirect": redirect, "at": time.time()},
    )
    query = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": redirect,
        "scope": SCOPES,
        "resource": RESOURCE,
    }
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


def _save_tokens(data: dict) -> dict:
    authed = data.get("authed_user") or data
    token = authed.get("access_token") or data.get("access_token")
    if not token:
        raise SlackError(f"Slack 没有返回 token：{data.get('error', 'unknown')}")
    record = {
        "access_token": token,
        "refresh_token": authed.get("refresh_token") or data.get("refresh_token"),
        "expires_at": time.time()
        + float(authed.get("expires_in") or data.get("expires_in") or 43200),
        "team": (data.get("team") or {}).get("name"),
        "team_id": (data.get("team") or {}).get("id"),
        "user_id": authed.get("id"),
        "scope": authed.get("scope") or data.get("scope"),
    }
    _write_private(TOKEN_FILE, record)
    return record


def auth_finish(callback: str) -> dict:
    """Exchange the code in the pasted callback address. Returns team/user, no token."""
    if not PENDING_FILE.exists():
        raise SlackError("没有进行中的授权，请重新获取链接")
    pending = json.loads(PENDING_FILE.read_text())
    if time.time() - pending["at"] > PENDING_TTL:
        PENDING_FILE.unlink(missing_ok=True)
        raise SlackError("授权链接已过期（15 分钟），请重新获取")
    actual = urlsplit(callback.strip())
    expected = urlsplit(pending["redirect"])
    if (actual.scheme, actual.netloc, actual.path) != (
        expected.scheme,
        expected.netloc,
        expected.path,
    ):
        raise SlackError(
            "这不是本次授权的回调地址，请复制「允许」之后浏览器地址栏里的完整网址"
        )
    params = parse_qs(actual.query)
    if params.get("error"):
        raise SlackError(f"Slack 拒绝了授权：{params['error'][0]}")
    if params.get("state", [""])[0] != pending["state"] or not params.get("code"):
        raise SlackError("回调地址不完整或来自另一轮授权，请重新获取链接")
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "grant_type": "authorization_code",
            "code": params["code"][0],
            "code_verifier": pending["verifier"],
            "redirect_uri": pending["redirect"],
        },
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok", True) or "error" in data and not data.get("access_token"):
        raise SlackError(f"换取 token 失败：{data.get('error', resp.status_code)}")
    record = _save_tokens(data)
    PENDING_FILE.unlink(missing_ok=True)
    return {"team": record["team"], "user": record["user_id"], "scope": record["scope"]}


def status() -> dict:
    if not TOKEN_FILE.exists():
        return {"state": "notConfigured"}
    record = json.loads(TOKEN_FILE.read_text())
    return {
        "state": "ok"
        if record.get("refresh_token") or record["expires_at"] > time.time()
        else "expired",
        "team": record.get("team"),
        "user": record.get("user_id"),
        "expiresIn": int(record["expires_at"] - time.time()),
    }


def access_token() -> str:
    if not TOKEN_FILE.exists():
        raise SlackError("Slack 未授权")
    record = json.loads(TOKEN_FILE.read_text())
    if record["expires_at"] - time.time() > 300:
        return record["access_token"]
    if not record.get("refresh_token"):
        raise SlackError("Slack 授权已过期，需要重新授权")
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": record["refresh_token"],
        },
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok", True) or not (
        data.get("access_token") or (data.get("authed_user") or {}).get("access_token")
    ):
        raise SlackError(f"刷新 Slack token 失败（{data.get('error')}），需要重新授权")
    saved = _save_tokens(data)
    if not saved.get("refresh_token"):
        saved["refresh_token"] = record["refresh_token"]
        _write_private(TOKEN_FILE, saved)
    return saved["access_token"]


class Mcp:
    """Minimal MCP streamable-HTTP client for the Slack server."""

    def __init__(self) -> None:
        self._http = requests.Session()
        self._session_id: str | None = None
        self._next = 0
        self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "calendar-agent", "version": "0.1"},
            },
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _post(self, body: dict) -> requests.Response:
        headers = {
            "Authorization": f"Bearer {access_token()}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        resp = self._http.post(MCP_URL, json=body, headers=headers, timeout=60)
        if resp.status_code == 401:
            raise SlackError("Slack 拒绝了 token，需要重新授权")
        resp.raise_for_status()
        self._session_id = resp.headers.get("Mcp-Session-Id", self._session_id)
        return resp

    def _request(self, method: str, params: dict) -> dict:
        self._next += 1
        resp = self._post(
            {"jsonrpc": "2.0", "id": self._next, "method": method, "params": params}
        )
        if "text/event-stream" in resp.headers.get("Content-Type", ""):
            messages = [
                json.loads(line[5:])
                for line in resp.text.splitlines()
                if line.startswith("data:") and line[5:].strip()
            ]
            message = next((m for m in messages if m.get("id") == self._next), {})
        else:
            message = resp.json()
        if "error" in message:
            raise SlackError(f"{method}: {message['error'].get('message', 'error')}")
        return message.get("result", {})

    def tools(self) -> list[dict]:
        return self._request("tools/list", {}).get("tools", [])

    def call(self, name: str, arguments: dict) -> dict:
        tool = next((t for t in self.tools() if t["name"] == name), None)
        if (
            tool is None
            or (tool.get("annotations") or {}).get("readOnlyHint") is not True
        ):
            raise SlackError("只能调用存在且标注为只读的 Slack 工具")
        return self._request("tools/call", {"name": name, "arguments": arguments})
