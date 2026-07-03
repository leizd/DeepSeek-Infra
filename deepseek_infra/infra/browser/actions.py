"""Browser action facade used by tools, Skills, smoke tests, and evals."""

from __future__ import annotations

from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.browser import safety, snapshot
from deepseek_infra.infra.browser.controller import controller_for
from deepseek_infra.infra.browser.session import close_session, create_session, get_session, list_sessions, mark_failed


def execute_browser_action(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AppError("Browser action payload must be an object", code=ErrorCode.INVALID_PAYLOAD, status=400)
    action = safety.normalize_action(payload.get("action"))
    if action == "open_url" and not str(payload.get("sessionId") or "").strip():
        session = create_session(project_id=str(payload.get("projectId") or ""), headless=_optional_bool(payload.get("headless")))
    elif action:
        session = get_session(str(payload.get("sessionId") or ""))
    else:
        session = create_session(project_id=str(payload.get("projectId") or ""), headless=_optional_bool(payload.get("headless")))
    request = {
        **payload,
        "action": action,
        "sessionId": session.browser_session_id,
        "projectId": session.project_id or str(payload.get("projectId") or ""),
        "currentUrl": session.current_url,
    }
    decision = safety.evaluate_action(request)
    if not decision.allowed:
        safety.audit_decision(decision, request, session_id=session.browser_session_id, project_id=session.project_id, outcome=decision.verdict)
        return {
            "ok": False,
            "code": "requires_confirmation" if decision.needs_confirmation else "forbidden",
            "error": "Browser action requires confirmation" if decision.needs_confirmation else "Browser action blocked by safety policy",
            "session": session.to_dict(),
            "safety": decision.to_dict(),
        }

    media_ids: list[str] = []
    try:
        session.touch(status="running")
        result = _dispatch(action, request, session=session)
        for key in ("media", "snapshot", "screenshot"):
            value = result.get(key)
            if isinstance(value, dict) and value.get("mediaId"):
                media_ids.append(str(value["mediaId"]))
        download_media = result.get("downloadMedia")
        if isinstance(download_media, dict) and isinstance(download_media.get("media"), dict):
            media_ids.append(str(download_media["media"].get("mediaId") or ""))
        session.touch(status="idle", current_url=str(result.get("url") or session.current_url))
        safety.audit_decision(decision, request, session_id=session.browser_session_id, project_id=session.project_id, outcome="pass", media_ids=media_ids)
        return {"ok": True, "session": session.to_dict(), "safety": decision.to_dict(), "result": result}
    except Exception as exc:
        mark_failed(session, str(exc))
        safety.audit_decision(decision, request, session_id=session.browser_session_id, project_id=session.project_id, outcome="failed")
        raise


def browser_status() -> dict[str, Any]:
    from deepseek_infra.core import config
    from deepseek_infra.infra.browser.controller import playwright_available

    return {
        "enabled": config.BROWSER_CONTROL_ENABLED,
        "headless": config.BROWSER_HEADLESS,
        "allowPrivateHosts": config.BROWSER_ALLOW_PRIVATE_HOSTS,
        "requireConfirm": config.BROWSER_REQUIRE_CONFIRM,
        "downloadMaxBytes": config.BROWSER_DOWNLOAD_MAX_BYTES,
        "sessionTtlSeconds": config.BROWSER_SESSION_TTL_SECONDS,
        "engine": "playwright",
        "playwrightAvailable": playwright_available(),
        "sessions": list_sessions(),
    }


def _dispatch(action: str, request: dict[str, Any], *, session: Any) -> dict[str, Any]:
    controller = controller_for(session)
    selector = str(request.get("selector") or "")
    if action == "open_url":
        page = controller.open_url(str(request.get("url") or ""))
        return {"url": str(page.get("url") or request.get("url") or ""), "page": _public_page(page), "controller": controller.kind}
    if action == "read_page" or action == "save_snapshot":
        page = controller.read_page(selector)
        saved = snapshot.save_page_snapshot(page, session_id=session.browser_session_id, project_id=session.project_id, selector=selector)
        media = saved["media"]
        return {
            "url": str(page.get("url") or session.current_url),
            "title": str(page.get("title") or ""),
            "text": str(page.get("text") or ""),
            "snapshot": media,
            "segments": saved["segments"],
            "indexed": saved["indexed"],
            "controller": controller.kind,
        }
    if action == "screenshot":
        image = controller.screenshot(selector)
        saved = snapshot.save_screenshot(
            image,
            session_id=session.browser_session_id,
            project_id=session.project_id,
            title=str(request.get("title") or "Browser screenshot"),
        )
        return {"url": str(image.get("url") or session.current_url), "screenshot": saved["media"], "controller": controller.kind}
    if action == "click":
        return {**controller.click(selector), "controller": controller.kind}
    if action == "type_text":
        return {**controller.type_text(selector, str(request.get("text") or "")), "controller": controller.kind}
    if action == "select":
        return {**controller.select(selector, str(request.get("value") or "")), "controller": controller.kind}
    if action == "scroll":
        return {**controller.scroll(x=_int(request.get("x")), y=_int(request.get("y"), default=600)), "controller": controller.kind}
    if action == "download":
        download = controller.download(str(request.get("downloadUrl") or request.get("url") or ""), selector, session_id=session.browser_session_id)
        saved = snapshot.register_download(download, session_id=session.browser_session_id, project_id=session.project_id)
        return {"url": str(download.get("sourceUrl") or session.current_url), "download": download, "downloadMedia": saved, "controller": controller.kind}
    if action == "extract_links":
        return {**controller.extract_links(selector), "controller": controller.kind}
    if action == "extract_dom":
        return {**controller.extract_dom(selector), "controller": controller.kind}
    if action == "close_session":
        closed = close_session(session.browser_session_id)
        return {"url": closed.current_url, "closed": True}
    raise AppError(f"Unsupported browser action: {action}", code=ErrorCode.INVALID_PAYLOAD, status=400)


def _public_page(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": str(page.get("url") or ""),
        "title": str(page.get("title") or ""),
        "text": str(page.get("text") or "")[:20_000],
        "selector": str(page.get("selector") or ""),
    }


def _int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)
