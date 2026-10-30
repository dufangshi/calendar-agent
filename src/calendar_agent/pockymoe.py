"""Pockymoe CLI API client. As a plugin the Supervisor passes POCKYMOE_URL and
POCKYMOE_TOKEN (the `pockymoeCli` permission); nothing is stored here."""

from __future__ import annotations

import os

import requests


class PockymoeError(Exception):
    pass


def available() -> bool:
    return bool(os.environ.get("POCKYMOE_URL") and os.environ.get("POCKYMOE_TOKEN"))


def call(operation: str, timeout: float = 60, **body) -> dict:
    url = os.environ.get("POCKYMOE_URL")
    token = os.environ.get("POCKYMOE_TOKEN")
    if not (url and token):
        raise PockymoeError("not running as a Pockymoe plugin (no CLI credential)")
    try:
        resp = requests.post(
            f"{url.rstrip('/')}/api/cli",
            json={"operation": operation, **body},
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise PockymoeError(f"{operation}: {type(exc).__name__}") from None
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        raise PockymoeError(f"{operation}: {data.get('message', resp.status_code)}")
    return data


def complete(
    connection_id: str, model: str, system: str, user: str, max_tokens: int = 1024
) -> str:
    return call(
        "modelComplete",
        timeout=120,
        connectionId=connection_id,
        model=model,
        system=system,
        user=user,
        maxTokens=max_tokens,
    )["text"]
