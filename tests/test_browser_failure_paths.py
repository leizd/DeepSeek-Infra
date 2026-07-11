from __future__ import annotations

from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.browser import actions, controller, safety
from deepseek_infra.infra.browser.session import BrowserSession


HTML = """
<html><head><title> Example &amp; page </title><style>hidden</style></head>
<body><main id="content" class="card wide">Hello <b>world</b>
<a id="next" class="button" href="/next" title="Next page"> Continue </a></main>
<script>ignored()</script><noscript>ignored</noscript></body></html>
"""


class FakeHttpResponse:
    def __init__(self, data: bytes, charset: str | None = None) -> None:
        self.data = data
        self.headers = Message()
        if charset:
            self.headers["Content-Type"] = f"text/html; charset={charset}"

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        assert limit == 5_000_000
        return self.data


class FakeDownloadEvent:
    def __init__(self, value: object) -> None:
        self.value = value

    def __enter__(self) -> "FakeDownloadEvent":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeLocator:
    def __init__(self) -> None:
        self.first = self
        self.filled = ""
        self.clicked = False

    def inner_text(self, timeout: int) -> str:
        assert timeout == 2_000
        return "selected text"

    def evaluate(self, script: str) -> Any:
        return "<section>selected</section>" if "outerHTML" in script else [{"href": "https://example.test"}]

    def screenshot(self, type: str) -> bytes:
        assert type == "png"
        return b"selector-png"

    def click(self, timeout: int) -> None:
        assert timeout == 5_000
        self.clicked = True

    def fill(self, text: str, timeout: int) -> None:
        assert timeout == 5_000
        self.filled = text

    def select_option(self, value: str, timeout: int) -> list[str]:
        assert timeout == 5_000
        return [value]

    def element_handle(self) -> object:
        return object()


class FakePage:
    url = "https://example.test/final"

    def __init__(self) -> None:
        self.locator_value = FakeLocator()
        self.mouse = SimpleNamespace(wheel=lambda x, y: setattr(self, "wheel", (x, y)))

    def goto(self, url: str, **kwargs: Any) -> None:
        self.url = url

    def locator(self, selector: str) -> FakeLocator:
        return self.locator_value

    def inner_text(self, selector: str, timeout: int) -> str:
        return "body text"

    def content(self) -> str:
        return HTML

    def title(self) -> str:
        return "Example"

    def screenshot(self, **kwargs: Any) -> bytes:
        return b"page-png"

    def evaluate(self, script: str, handle: object) -> list[dict[str, str]]:
        return [{"href": "https://example.test", "text": "Example"}]


def _playwright_controller() -> controller.PlaywrightController:
    instance = controller.PlaywrightController.__new__(controller.PlaywrightController)
    cast(Any, instance)._page = FakePage()
    cast(Any, instance)._context = SimpleNamespace(close=lambda: None)
    cast(Any, instance)._playwright = SimpleNamespace(stop=lambda: None)
    return instance


def test_static_controller_parses_selects_redirects_and_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {"https://example.test/start": HTML, "https://example.test/next": "<title>Next</title><p>done</p>"}
    monkeypatch.setattr(controller, "read_url_text", lambda url: pages[url])
    saved: list[tuple[str, str]] = []
    def fetch_download(session_id: str, url: str) -> dict[str, str]:
        saved.append((session_id, url))
        return {"sourceUrl": url}

    monkeypatch.setattr(controller.downloads, "fetch_download", fetch_download)
    static = controller.StaticController()

    opened = static.open_url("https://example.test/start")
    assert opened["title"] == "Example & page"
    assert opened["text"] == "Example & page\nHello\nworld\nContinue"
    assert static.read_page("#content")["text"].endswith("Continue")
    assert static.screenshot()["bytes"].startswith(b"\x89PNG")
    assert static.extract_links()["links"][0]["href"] == "https://example.test/next"
    assert static.click("#missing")["static"] is True
    assert static.click("#next")["url"] == "https://example.test/next"
    assert static.type_text("#field", "abc")["chars"] == 3
    assert static.select("select", "v")["value"] == "v"
    assert static.scroll(x=2, y=3)["x"] == 2
    static.html = HTML
    assert static.extract_dom(".card")["html"].startswith("<main")
    assert static.download("https://example.test/file", session_id="browser-1")["sourceUrl"].endswith("/file")
    static.html = HTML
    static.url = "https://example.test/start"
    static.download(selector="#next", session_id="browser-1")
    assert saved[-1] == ("browser-1", "https://example.test/next")
    with pytest.raises(AppError):
        controller.StaticController().download()


def test_parsed_html_and_selector_helpers_cover_empty_and_missing_content() -> None:
    parsed = controller.ParsedHTML.parse(HTML, base_url="https://example.test/start")

    assert parsed.title == "Example & page"
    assert parsed.links == [{"href": "https://example.test/next", "text": "Continue", "title": "Next page"}]
    assert controller.html_for_selector(HTML, "") == HTML
    assert controller.html_for_selector(HTML, "main").startswith("<main")
    assert controller.html_for_selector(HTML, ".missing") == ""
    assert controller.text_for_selector(HTML, ".missing").startswith("Example & page")
    assert controller.link_href_for_selector(HTML, ".missing", base_url="https://example.test") == ""
    assert controller._tag_by_attr(HTML, "id", "content")
    assert controller._tag_by_class(HTML, "wide")
    assert controller._tag_by_name(HTML, "").startswith("<body")


def test_read_url_text_supports_file_and_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    page = tmp_path / "page.html"
    page.write_text("café", encoding="utf-8")
    assert controller.read_url_text(page.as_uri()) == "café"

    monkeypatch.setattr(controller.urllib.request, "urlopen", lambda url, timeout: FakeHttpResponse("olá".encode("latin-1"), "latin-1"))
    assert controller.read_url_text("https://example.test") == "olá"


def test_playwright_controller_methods_and_screenshot_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = _playwright_controller()

    assert browser.open_url("https://example.test")["title"] == "Example"
    assert browser.read_page("#content")["text"] == "selected text"
    assert browser.screenshot("#content")["bytes"] == b"selector-png"
    assert browser.screenshot()["bytes"] == b"page-png"
    assert browser.click("#button")["selector"] == "#button"
    assert browser.type_text("#field", "hello")["chars"] == 5
    assert browser.select("#choice", "one")["selected"] == ["one"]
    assert browser.scroll(x=4, y=8)["y"] == 8
    assert browser.extract_links()["links"][0]["text"] == "Example"
    assert browser.extract_links("main")["links"][0]["href"] == "https://example.test"
    assert browser.extract_dom()["selector"] == "document"
    assert browser.extract_dom("main")["html"].startswith("<section")
    monkeypatch.setattr(browser._page, "screenshot", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("capture failed")))
    with pytest.raises(RuntimeError, match="capture failed"):
        browser.screenshot()


def test_playwright_download_and_controller_creation_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    browser = _playwright_controller()
    source = tmp_path / "download.bin"
    source.write_bytes(b"payload")
    download = SimpleNamespace(suggested_filename="report.bin", path=lambda: str(source))
    event = FakeDownloadEvent(download)
    cast(Any, browser._page).expect_download = lambda timeout: event
    monkeypatch.setattr(controller.downloads, "save_download_bytes", lambda sid, name, data, source_url: {"name": name, "bytes": data})
    monkeypatch.setattr(controller.downloads, "fetch_download", lambda sid, url: {"sourceUrl": url})

    assert browser.download(selector="#download", session_id="browser-1")["bytes"] == b"payload"
    assert browser.download(url="https://example.test/file", session_id="browser-1")["sourceUrl"].endswith("/file")

    session = BrowserSession("browser-fallback", engine="playwright", profile_dir=str(tmp_path / "profile"))
    monkeypatch.setattr(controller, "playwright_available", lambda: True)
    monkeypatch.setattr(controller, "PlaywrightController", lambda session: (_ for _ in ()).throw(RuntimeError("launch failed")))
    assert isinstance(controller._create_controller(session), controller.StaticController)
    session.engine = "static"
    assert isinstance(controller._create_controller(session), controller.StaticController)


def test_private_and_invalid_browser_urls_are_rejected() -> None:
    invalid = safety.evaluate_action({"action": "open_url", "url": "not-a-url"})
    private = safety.evaluate_action({"action": "open_url", "url": "http://127.0.0.1/admin"})

    assert invalid.allowed is False
    assert private.allowed is False
    assert actions._int("bad", default=7) == 7
    assert actions._optional_bool(None) is None


@pytest.mark.parametrize(
    ("payload", "verdict", "reason"),
    [
        ({"action": "unknown"}, safety.DENY, "unknown_action"),
        ({"action": "open_url"}, safety.DENY, "missing_url"),
        ({"action": "type_text", "selector": "input[type='password']"}, safety.NEEDS_CONFIRMATION, "password_field_requires_confirmation"),
        ({"action": "click", "selector": "button", "reason": "confirm purchase"}, safety.NEEDS_CONFIRMATION, "high_risk_click_requires_confirmation"),
        ({"action": "download", "filename": "installer.exe"}, safety.NEEDS_CONFIRMATION, "executable_download_requires_confirmation"),
        ({"action": "read_page", "requiresConfirmation": True}, safety.NEEDS_CONFIRMATION, "caller_requested_confirmation"),
    ],
)
def test_browser_safety_classifies_each_high_risk_action(
    payload: dict[str, Any], verdict: str, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(safety.config, "BROWSER_CONTROL_ENABLED", True)
    monkeypatch.setattr(safety.config, "BROWSER_REQUIRE_CONFIRM", True)
    decision = safety.evaluate_action(payload)
    assert decision.verdict == verdict
    assert reason in decision.reasons


def test_browser_safety_confirmation_and_disabled_control(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety.config, "BROWSER_CONTROL_ENABLED", False)
    assert safety.evaluate_action({"action": "read_page"}).reasons == ("browser_control_disabled",)
    monkeypatch.setattr(safety.config, "BROWSER_CONTROL_ENABLED", True)
    monkeypatch.setattr(safety.config, "BROWSER_REQUIRE_CONFIRM", True)
    confirmed = safety.evaluate_action({"action": "click", "selector": "buy", "confirmed": True})
    assert confirmed.allowed and "confirmed" in confirmed.reasons


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.com/file",
        "https://user:pass@example.com",
        "https:///missing-host",
        "http://service.internal",
        "http://[::1]",
        "http://224.0.0.1",
    ],
)
def test_url_safety_rejects_credential_and_local_network_variants(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety.config, "BROWSER_ALLOW_PRIVATE_HOSTS", False)
    assert safety.evaluate_url_safety(url)[0] is False


def test_url_safety_allows_public_dns_and_private_host_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety.config, "BROWSER_ALLOW_PRIVATE_HOSTS", False)
    assert safety.evaluate_url_safety("https://example.com/path") == (True, "")
    monkeypatch.setattr(safety.config, "BROWSER_ALLOW_PRIVATE_HOSTS", True)
    assert safety.evaluate_url_safety("http://127.0.0.1") == (True, "")


def test_browser_audit_io_error_and_unserializable_args_are_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety.config, "BROWSER_AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(safety.config, "BROWSER_AUDIT_LOG", tmp_path / "audit" / "events.jsonl")
    decision = safety.BrowserSafetyDecision("read_page", safety.ALLOW, "low")
    safety.audit_decision(decision, {"action": "read_page", "value": object()})
    assert safety.config.BROWSER_AUDIT_LOG.exists()
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
        safety.audit_decision(decision, {"action": "read_page"})
    assert safety.normalized_args_hash({"bad": {1, 2}}).startswith("sha256:")


class _DispatchController:
    kind = "stub"

    def open_url(self, url: str) -> dict[str, Any]:
        return {"url": url, "title": "Page", "text": "body"}

    def read_page(self, selector: str) -> dict[str, Any]:
        return {"url": "https://example.test", "title": "Page", "text": selector or "body"}

    def screenshot(self, selector: str) -> dict[str, Any]:
        return {"url": "https://example.test", "bytes": b"png", "selector": selector}

    def click(self, selector: str) -> dict[str, Any]:
        return {"selector": selector}

    def type_text(self, selector: str, text: str) -> dict[str, Any]:
        return {"selector": selector, "text": text}

    def select(self, selector: str, value: str) -> dict[str, Any]:
        return {"selector": selector, "value": value}

    def scroll(self, *, x: int, y: int) -> dict[str, Any]:
        return {"x": x, "y": y}

    def download(self, url: str, selector: str, *, session_id: str) -> dict[str, Any]:
        return {"sourceUrl": url, "selector": selector, "sessionId": session_id}

    def extract_links(self, selector: str) -> dict[str, Any]:
        return {"selector": selector, "links": []}

    def extract_dom(self, selector: str) -> dict[str, Any]:
        return {"selector": selector, "html": "<main/>"}


@pytest.mark.parametrize("action", ["open_url", "read_page", "save_snapshot", "screenshot", "click", "type_text", "select", "scroll", "download", "extract_links", "extract_dom"])
def test_browser_dispatch_covers_all_controller_actions(action: str, monkeypatch: pytest.MonkeyPatch) -> None:
    browser_session = BrowserSession("browser-dispatch", project_id="project", current_url="https://example.test")
    monkeypatch.setattr(actions, "controller_for", lambda _: _DispatchController())
    monkeypatch.setattr(actions.snapshot, "save_page_snapshot", lambda *_args, **_kwargs: {"media": {"mediaId": "page"}, "segments": [], "indexed": True})
    monkeypatch.setattr(actions.snapshot, "save_screenshot", lambda *_args, **_kwargs: {"media": {"mediaId": "shot"}})
    monkeypatch.setattr(actions.snapshot, "register_download", lambda *_args, **_kwargs: {"media": {"mediaId": "download"}})
    request = {"url": "https://example.test", "downloadUrl": "https://example.test/file", "selector": "main", "text": "hello", "value": "one", "x": "bad", "y": "bad"}
    assert actions._dispatch(action, request, session=browser_session)["controller"] == "stub"


def test_browser_dispatch_rejects_unknown_action(monkeypatch: pytest.MonkeyPatch) -> None:
    browser_session = BrowserSession("browser-dispatch")
    monkeypatch.setattr(actions, "controller_for", lambda _: _DispatchController())
    with pytest.raises(AppError, match="Unsupported browser action"):
        actions._dispatch("unknown", {}, session=browser_session)
