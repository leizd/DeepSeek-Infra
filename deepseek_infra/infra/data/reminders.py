"""Local reminder queue for browser notifications."""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from deepseek_infra.core.config import REMINDERS_DIR, REMINDERS_FILE
from deepseek_infra.infra.workspace import mutation_gate
from deepseek_infra.core.errors import AppError, ErrorCode

_LOCK = threading.RLock()
MAX_REMINDERS = 200


def load_reminders() -> list[dict[str, Any]]:
    with _LOCK:
        return _read_reminders()


def create_reminder(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title") or "提醒").strip()[:120] or "提醒"
    content = str(payload.get("content") or "").strip()[:2000]
    due_at = parse_due_at(payload.get("dueAt") or payload.get("due_at"))
    reminder = {
        "id": secrets.token_hex(8),
        "title": title,
        "content": content,
        "dueAt": due_at,
        "createdAt": int(time.time() * 1000),
        "notified": False,
    }
    with _LOCK:
        reminders = [item for item in _read_reminders() if not bool(item.get("notified"))]
        reminders.append(reminder)
        reminders = sorted(reminders, key=lambda item: str(item.get("dueAt") or ""))[-MAX_REMINDERS:]
        _write_reminders(reminders)
    return reminder


def due_reminders(now: datetime | None = None) -> list[dict[str, Any]]:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    due: list[dict[str, Any]] = []
    with _LOCK:
        reminders = _read_reminders()
        for item in reminders:
            if bool(item.get("notified")):
                continue
            try:
                due_at = datetime.fromisoformat(parse_due_at(item.get("dueAt")))
            except AppError:
                continue
            if due_at <= now_utc:
                item["notified"] = True
                item["notifiedAt"] = int(time.time() * 1000)
                due.append(dict(item))
        if due:
            _write_reminders(reminders)
    return due


def delete_reminder(reminder_id: str) -> int:
    reminder_id = str(reminder_id or "").strip()
    if not reminder_id:
        return 0
    with _LOCK:
        reminders = _read_reminders()
        next_reminders = [item for item in reminders if item.get("id") != reminder_id]
        if len(next_reminders) != len(reminders):
            _write_reminders(next_reminders)
            return 1
    return 0


def parse_due_at(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise AppError("Reminder dueAt is required", code=ErrorCode.INVALID_PAYLOAD)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        due_at = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AppError("Reminder dueAt must be an ISO datetime", code=ErrorCode.INVALID_PAYLOAD) from exc
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    return due_at.astimezone(timezone.utc).isoformat()


def parse_natural_reminder(text: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Best-effort Chinese reminder parser used as a lightweight fallback."""

    value = str(text or "").strip()
    if "提醒" not in value:
        return None
    now = now or datetime.now()
    match = re.search(r"(明早|明天|今天|今晚|早上|上午|下午|晚上)?\s*(\d{1,2})(?:[:：点](\d{1,2})?)?", value)
    if not match:
        return None
    period = match.group(1) or ""
    hour = int(match.group(2))
    minute = int(match.group(3) or 0)
    if period in {"下午", "晚上", "今晚"} and hour < 12:
        hour += 12
    if period in {"明早", "明天"}:
        due = now + timedelta(days=1)
    else:
        due = now
    due = due.replace(hour=min(hour, 23), minute=min(minute, 59), second=0, microsecond=0)
    if due <= now:
        due += timedelta(days=1)

    content = re.sub(r"^.*?提醒我", "", value).strip(" ：:，,。") or value
    return {"title": "提醒", "content": content[:2000], "dueAt": due.isoformat()}


def _read_reminders() -> list[dict[str, Any]]:
    if not REMINDERS_FILE.exists():
        return []
    try:
        data = json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _write_reminders(reminders: list[dict[str, Any]]) -> None:
    with mutation_gate.mutation_scope(root=REMINDERS_DIR.parent):
        REMINDERS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = REMINDERS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(reminders, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(REMINDERS_FILE)
