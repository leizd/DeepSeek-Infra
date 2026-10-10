from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deepseek_infra.infra.data import reminders


def test_create_and_mark_due_reminder(tmp_settings) -> None:
    due_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()

    created = reminders.create_reminder({"title": "晨会", "content": "准备要点", "dueAt": due_at})
    due = reminders.due_reminders(datetime.now(timezone.utc))
    remaining = reminders.load_reminders()

    assert created["id"]
    assert due[0]["title"] == "晨会"
    assert remaining[0]["notified"] is True


def test_due_reminders_compares_datetimes_across_iso_precisions(tmp_settings) -> None:
    due_at = datetime(2026, 5, 10, 8, 0, 0, tzinfo=timezone.utc).isoformat()
    now = datetime(2026, 5, 10, 8, 0, 0, 500000, tzinfo=timezone.utc)

    created = reminders.create_reminder({"title": "precision", "dueAt": due_at})
    due = reminders.due_reminders(now)

    assert due[0]["id"] == created["id"]


def test_parse_natural_reminder_extracts_chinese_time() -> None:
    now = datetime(2026, 5, 10, 8, 0, 0)

    parsed = reminders.parse_natural_reminder("明早 9 点提醒我准备晨会要点", now=now)

    assert parsed is not None
    assert parsed["content"] == "准备晨会要点"
    assert "2026-05-11T09:00:00" in parsed["dueAt"]


def test_parse_due_at_rejects_bad_value() -> None:
    with pytest.raises(Exception):
        reminders.parse_due_at("not a date")


def test_reminder_corrupt_storage_delete_and_invalid_due_rows(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    reminders.REMINDERS_DIR.mkdir(parents=True)
    reminders.REMINDERS_FILE.write_text("not-json", encoding="utf-8")
    assert reminders.load_reminders() == []
    reminders.REMINDERS_FILE.write_text("{}", encoding="utf-8")
    assert reminders.load_reminders() == []
    reminders.REMINDERS_FILE.write_text('[null,{"id":"bad","dueAt":"bad"},{"id":"done","dueAt":"2020-01-01T00:00:00+00:00","notified":true}]', encoding="utf-8")
    assert reminders.due_reminders(datetime.now(timezone.utc)) == []
    assert reminders.delete_reminder("") == 0
    assert reminders.delete_reminder("missing") == 0

    created = reminders.create_reminder({"title": "", "content": "x", "dueAt": datetime.now(timezone.utc).isoformat()})
    assert reminders.delete_reminder(created["id"]) == 1
    assert not reminders.REMINDERS_FILE.with_suffix(".tmp").exists()


def test_reminder_due_time_timezone_and_natural_parser_edges() -> None:
    with pytest.raises(Exception):
        reminders.parse_due_at("")
    assert reminders.parse_due_at("2026-01-01T10:00:00").endswith("+00:00")
    assert reminders.parse_due_at("2026-01-01T10:00:00Z").endswith("+00:00")
    assert reminders.parse_natural_reminder("ordinary text") is None
    assert reminders.parse_natural_reminder("提醒 but no time") is None
    now = datetime(2026, 1, 1, 20, 0)
    parsed = reminders.parse_natural_reminder("晚上 9 点提醒我 test", now=now)
    assert parsed is not None and "21:00:00" in parsed["dueAt"]
    next_day = reminders.parse_natural_reminder("9 点提醒我 test", now=datetime(2026, 1, 1, 10, 0))
    assert next_day is not None and "2026-01-02" in next_day["dueAt"]


def test_every_reminder_write_path_is_denied_once_python_is_de_authorized(
    tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`reminders_store` is a declared domain (python -> rust, 4.9.4), so the handover is mechanical.

    All three Python write paths funnel through `_write_reminders` — `create_reminder`,
    `due_reminders`'s delivery marking, and `delete_reminder`. That is exactly why the gate sits there
    and not at each call site: the delivery marking runs on a **poll**, outside any conversation, so a
    per-call-site gate would have to remember a path nobody is looking at.

    The gate is mode-driven because ADR-0049 says "a cutover gate that has not passed leaves the prior
    owner authoritative; it does not permit dual writers" — so the default mode leaving every path
    working is half of what this test asserts.
    """
    from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError

    # The default mode: creation works, and the store is readable.
    overdue = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    created = reminders.create_reminder({"title": "晨会", "content": "准备要点", "dueAt": overdue})
    assert [item["id"] for item in reminders.load_reminders()] == [created["id"]]
    before = reminders.REMINDERS_FILE.read_text(encoding="utf-8")

    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    fresh = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with pytest.raises(PythonWriterMechanicallyDeniedError):
        reminders.create_reminder({"title": "after the cutover", "dueAt": fresh})
    # The delivery poll writes too, and this is the path that makes the choke point necessary.
    with pytest.raises(PythonWriterMechanicallyDeniedError):
        reminders.due_reminders(datetime.now(timezone.utc))
    with pytest.raises(PythonWriterMechanicallyDeniedError):
        reminders.delete_reminder(str(created["id"]))
    assert reminders.REMINDERS_FILE.read_text(encoding="utf-8") == before, "a denied write changed the store"
