"""The owner's long-lived rules, one per line, editable by the agent thread."""

from __future__ import annotations

from .config import DATA_DIR, ensure_data_dirs

RULES_FILE = DATA_DIR / "rules.txt"


def load() -> list[str]:
    if not RULES_FILE.exists():
        return []
    return [line for line in RULES_FILE.read_text().splitlines() if line.strip()]


def save(rules: list[str]) -> None:
    ensure_data_dirs()
    RULES_FILE.write_text("".join(f"{r.strip()}\n" for r in rules if r.strip()))
