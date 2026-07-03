from __future__ import annotations

import json
from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.infra.browser import session as browser_session
from deepseek_infra.infra.browser.actions import execute_browser_action
from deepseek_infra.infra.browser.downloads import sanitize_filename
from deepseek_infra.infra.media import library
from deepseek_infra.infra.rag import local_rag
from deepseek_infra.infra.workspace import projects


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "browser"


def _enable_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "BROWSER_CONTROL_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_REQUIRE_CONFIRM", True)
    monkeypatch.setattr(config, "BROWSER_ALLOW_PRIVATE_HOSTS", False)


def test_browser_control_is_disabled_by_default(tmp_settings: Path) -> None:
    result = execute_browser_action({"action": "open_url", "url": (FIXTURES / "basic.html").as_uri()})

    assert result["ok"] is False
    assert result["code"] == "forbidden"
    assert "browser_control_disabled" in result["safety"]["reasons"]


def test_browser_read_page_saves_media_snapshot_and_rag(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_browser(monkeypatch)
    project = projects.create_project("Browser Project")
    opened = execute_browser_action({"action": "open_url", "projectId": project["projectId"], "url": (FIXTURES / "basic.html").as_uri()})
    session_id = opened["session"]["browserSessionId"]

    read = execute_browser_action({"action": "read_page", "sessionId": session_id, "selector": "#content"})
    snapshot = read["result"]["snapshot"]
    segments = library.list_segments(snapshot["mediaId"])
    hits = local_rag.search_media_index("Browser snapshots Local RAG", project_id=project["projectId"], media_id=snapshot["mediaId"], limit=3)

    assert read["ok"] is True
    assert snapshot["type"] == "webpage"
    assert snapshot["source"]["kind"] == "browser"
    assert snapshot["source"]["browserSessionId"] == session_id
    assert segments[0]["type"] == "webpage_text"
    assert segments[0]["citation"]["uri"].startswith(f"browser://{session_id}")
    assert hits
    assert hits[0].metadata["sourceType"] == "media"


def test_browser_screenshot_and_extract_links(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_browser(monkeypatch)
    opened = execute_browser_action({"action": "open_url", "url": (FIXTURES / "basic.html").as_uri()})
    session_id = opened["session"]["browserSessionId"]

    screenshot = execute_browser_action({"action": "screenshot", "sessionId": session_id})
    links = execute_browser_action({"action": "extract_links", "sessionId": session_id})

    assert screenshot["result"]["screenshot"]["type"] == "screenshot"
    assert screenshot["result"]["screenshot"]["mimeType"] == "image/png"
    assert any(link["href"].endswith("download.html") for link in links["result"]["links"])


def test_browser_blocks_private_hosts_by_default(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_browser(monkeypatch)
    result = execute_browser_action({"action": "open_url", "url": "http://127.0.0.1:8000/private"})

    assert result["ok"] is False
    assert result["safety"]["risk"] == "critical"
    assert any(reason.startswith("unsafe_url:") for reason in result["safety"]["reasons"])


def test_browser_form_submit_and_password_typing_require_confirmation(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_browser(monkeypatch)
    opened = execute_browser_action({"action": "open_url", "url": (FIXTURES / "form.html").as_uri()})
    session_id = opened["session"]["browserSessionId"]

    submit = execute_browser_action({"action": "click", "sessionId": session_id, "selector": "button.submit", "reason": "Submit form"})
    password = execute_browser_action({"action": "type_text", "sessionId": session_id, "selector": "#password", "text": "secret"})

    assert submit["code"] == "requires_confirmation"
    assert "high_risk_click_requires_confirmation" in submit["safety"]["reasons"]
    assert password["code"] == "requires_confirmation"
    assert "password_field_requires_confirmation" in password["safety"]["reasons"]


def test_browser_download_uses_isolated_directory(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_browser(monkeypatch)
    monkeypatch.setattr(config, "BROWSER_REQUIRE_CONFIRM", False)
    opened = execute_browser_action({"action": "open_url", "url": (FIXTURES / "download.html").as_uri()})
    session_id = opened["session"]["browserSessionId"]

    result = execute_browser_action({"action": "download", "sessionId": session_id, "selector": "#download-report"})
    download = result["result"]["download"]

    assert download["isolated"] is True
    assert Path(download["path"]).is_file()
    assert Path(download["path"]).is_relative_to(config.BROWSER_DOWNLOADS_DIR)
    assert result["result"]["downloadMedia"]["media"]["type"] == "webpage"


def test_browser_snapshot_redacts_secrets_and_audit_is_written(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_browser(monkeypatch)
    opened = execute_browser_action({"action": "open_url", "url": (FIXTURES / "injection.html").as_uri()})
    session_id = opened["session"]["browserSessionId"]
    read = execute_browser_action({"action": "read_page", "sessionId": session_id})

    segments = library.list_segments(read["result"]["snapshot"]["mediaId"])
    audit_lines = config.BROWSER_AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    audit = [json.loads(line) for line in audit_lines]

    assert "sk-browser-secret-value" not in json.dumps(segments, ensure_ascii=False)
    assert any(entry["action"] == "read_page" and entry["taint"] == "untrusted_browser" for entry in audit)
    assert all("requestId" in entry and "riskLevel" in entry for entry in audit)


def test_browser_close_session_is_idempotent(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_browser(monkeypatch)
    opened = execute_browser_action({"action": "open_url", "url": (FIXTURES / "basic.html").as_uri()})
    session_id = opened["session"]["browserSessionId"]

    first = execute_browser_action({"action": "close_session", "sessionId": session_id})
    second = execute_browser_action({"action": "close_session", "sessionId": session_id})
    missing = execute_browser_action({"action": "close_session", "sessionId": "browser_missing0000000"})

    assert first["ok"] is True
    assert second["ok"] is True
    assert missing["ok"] is True
    assert first["result"]["closed"] is True
    assert second["result"]["closed"] is True


def test_browser_expired_session_is_cleaned_up(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_browser(monkeypatch)
    opened = execute_browser_action({"action": "open_url", "url": (FIXTURES / "basic.html").as_uri()})
    session_id = opened["session"]["browserSessionId"]

    session = browser_session.get_session(session_id)
    session.last_access = 0.0
    expired_count = browser_session.close_expired_sessions()

    assert expired_count >= 1
    with pytest.raises(Exception):
        browser_session.get_session(session_id)


def test_browser_download_filename_is_strictly_sanitized() -> None:
    assert sanitize_filename("../../../etc/passwd") == "etc-passwd"
    assert sanitize_filename("file..name..txt") == "file.name.txt"
    assert sanitize_filename("--leading-trailing--") == "leading-trailing"
    assert sanitize_filename("") == "download.bin"
    assert sanitize_filename("a" * 200).endswith(".bin") is False
    assert len(sanitize_filename("a" * 200)) <= 80


def test_browser_download_respects_byte_limit(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_browser(monkeypatch)
    monkeypatch.setattr(config, "BROWSER_REQUIRE_CONFIRM", False)
    monkeypatch.setattr(config, "BROWSER_DOWNLOAD_MAX_BYTES", 16)
    oversized = tmp_settings / "oversized.bin"
    oversized.write_bytes(b"x" * 32)

    opened = execute_browser_action({"action": "open_url", "url": oversized.as_uri()})
    session_id = opened["session"]["browserSessionId"]
    result = execute_browser_action({"action": "download", "sessionId": session_id, "downloadUrl": oversized.as_uri()})

    assert result["ok"] is False
    assert result["code"] in {"forbidden", "upload_too_large", "internal"}
