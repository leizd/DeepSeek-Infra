from __future__ import annotations

from pathlib import Path

from deepseek_infra.web.http_utils import apply_common_headers
from starlette.responses import Response


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_index_uses_csp_safe_theme_boot_and_local_inter() -> None:
    index = read("static/index.html")
    assert '<script src="/theme_boot.js?v=401"></script>' in index
    assert "<script>" not in index
    assert "fonts.googleapis.com" not in index
    assert "fonts.gstatic.com" not in index
    assert '/vendor/inter/inter.css?v=401' in index
    assert (ROOT / "static/vendor/inter/Inter-Variable.ttf").stat().st_size > 100_000
    assert "SIL OPEN FONT LICENSE" in read("static/vendor/inter/OFL.txt")

    response = Response()
    apply_common_headers(response, "/")
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "font-src 'self'" in csp
    assert "fonts.googleapis.com" not in csp


def test_credentials_are_not_written_to_local_storage() -> None:
    chat = read("static/modules/chat.js")
    credentials = read("static/modules/credential_store.js")
    assert "localStorage.setItem(storageKeys.apiKey" not in chat
    assert "localStorage.setItem(storageKeys.tavilyKey" not in chat
    assert "createCredentialSession(storageKeys)" in chat
    assert "safeRemove(local, key)" in credentials
    assert "sessionStorage" in credentials


def test_upload_contract_has_timeout_abort_and_finally_cleanup() -> None:
    upload = read("static/modules/upload_controller.js")
    chat = read("static/modules/chat.js")
    assert "xhr.timeout = positiveTimeout(options.timeoutMs)" in upload
    assert "xhr.ontimeout" in upload
    assert "xhr.onabort" in upload
    assert "cancel()" in upload and "xhr.abort()" in upload
    assert "return await task.promise" in chat
    assert "setUploadActive(false)" in chat
    assert 'data-cancel-upload' in chat


def test_service_worker_precaches_complete_versioned_app_shell() -> None:
    worker = read("static/sw.js")
    for asset in (
        '"/theme_boot.js"',
        '"/vendor/inter/inter.css"',
        '"/vendor/inter/Inter-Variable.ttf"',
        '"/vaultr-brutalist.css"',
        '"/modules/upload_controller.js"',
        '"/modules/credential_store.js"',
        '"/modules/workspace_tabs.js"',
        '"/modules/skill_builder.js"',
    ):
        assert asset in worker
    assert 'const CACHE_NAME = "deepseek-infra-v403"' in worker
    assert "await cache.addAll(CORE_SHELL)" in worker
    assert "Promise.allSettled" in worker
    assert "staleWhileRevalidate" in worker
    assert "url.search = \"\"" in worker
    assert "fetch(event.request).catch(() => caches.match(event.request))" not in worker


def test_workspace_tabs_have_complete_aria_and_keyboard_contract() -> None:
    index = read("static/index.html")
    tabs = read("static/modules/workspace_tabs.js")
    assert index.count('role="tab"') == 5
    assert index.count('role="tabpanel"') == 5
    assert index.count('aria-controls="workspace') == 5
    assert "ArrowRight" in tabs and "ArrowLeft" in tabs
    assert 'event.key === "Home"' in tabs and 'event.key === "End"' in tabs
    assert 'tab.setAttribute("aria-selected", String(selected))' in tabs


def test_frontend_responsibilities_are_split_into_es_modules() -> None:
    chat = read("static/modules/chat.js")
    skills = read("static/modules/skills.js")
    assert 'from "./credential_store.js"' in chat
    assert 'from "./workspace_tabs.js"' in chat
    assert 'from "./skill_builder.js"' in skills
    assert "function defaultBuilderSkill" not in skills
