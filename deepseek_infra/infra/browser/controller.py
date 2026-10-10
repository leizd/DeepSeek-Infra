"""Playwright controller with a static HTML fallback."""

from __future__ import annotations

import base64
import html
import re
import threading
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.browser import downloads
from deepseek_infra.infra.browser.session import BrowserSession

_controllers: dict[str, "BrowserController"] = {}
_lock = threading.RLock()


class BrowserController:
    kind = "base"

    def open_url(self, url: str) -> dict[str, Any]:
        raise NotImplementedError

    def read_page(self, selector: str = "") -> dict[str, Any]:
        raise NotImplementedError

    def screenshot(self, selector: str = "") -> dict[str, Any]:
        raise NotImplementedError

    def click(self, selector: str) -> dict[str, Any]:
        raise NotImplementedError

    def type_text(self, selector: str, text: str) -> dict[str, Any]:
        raise NotImplementedError

    def select(self, selector: str, value: str) -> dict[str, Any]:
        raise NotImplementedError

    def scroll(self, *, x: int = 0, y: int = 600) -> dict[str, Any]:
        raise NotImplementedError

    def download(self, url: str = "", selector: str = "", *, session_id: str = "") -> dict[str, Any]:
        raise NotImplementedError

    def extract_links(self, selector: str = "") -> dict[str, Any]:
        raise NotImplementedError

    def extract_dom(self, selector: str = "") -> dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        return None


def controller_for(session: BrowserSession) -> BrowserController:
    with _lock:
        existing = _controllers.get(session.browser_session_id)
        if existing is not None:
            return existing
        controller = _create_controller(session)
        _controllers[session.browser_session_id] = controller
        session.touch(controller_kind=controller.kind)
        return controller


def close_controller(session_id: str) -> None:
    with _lock:
        controller = _controllers.pop(str(session_id or ""), None)
    if controller is not None:
        controller.close()


def _create_controller(session: BrowserSession) -> BrowserController:
    if session.engine == "playwright" and playwright_available():
        try:
            return PlaywrightController(session)
        except Exception:
            return StaticController()
    return StaticController()


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False
    return True


class PlaywrightController(BrowserController):
    kind = "playwright"

    def __init__(self, session: BrowserSession) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        self._timeout_error = PlaywrightTimeoutError
        self._playwright = sync_playwright().start()
        profile_dir = Path(session.profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=session.headless,
            accept_downloads=True,
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def open_url(self, url: str) -> dict[str, Any]:
        self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        return self.read_page()

    def read_page(self, selector: str = "") -> dict[str, Any]:
        locator = self._locator(selector)
        text = locator.inner_text(timeout=2_000) if selector else self._page.inner_text("body", timeout=2_000)
        html_text = locator.evaluate("node => node.outerHTML") if selector else self._page.content()
        title = self._page.title()
        return {"url": self._page.url, "title": title, "text": text, "html": html_text, "selector": selector or "body"}

    def screenshot(self, selector: str = "") -> dict[str, Any]:
        data = self._locator(selector).screenshot(type="png") if selector else self._page.screenshot(type="png", full_page=True)
        return {"url": self._page.url, "mimeType": "image/png", "bytes": data, "selector": selector}

    def click(self, selector: str) -> dict[str, Any]:
        self._locator(selector).click(timeout=5_000)
        return {"url": self._page.url, "selector": selector}

    def type_text(self, selector: str, text: str) -> dict[str, Any]:
        locator = self._locator(selector)
        locator.fill(text, timeout=5_000)
        return {"url": self._page.url, "selector": selector, "chars": len(text)}

    def select(self, selector: str, value: str) -> dict[str, Any]:
        locator = self._locator(selector)
        selected = locator.select_option(value, timeout=5_000)
        return {"url": self._page.url, "selector": selector, "value": value, "selected": selected}

    def scroll(self, *, x: int = 0, y: int = 600) -> dict[str, Any]:
        self._page.mouse.wheel(int(x), int(y))
        return {"url": self._page.url, "x": int(x), "y": int(y)}

    def download(self, url: str = "", selector: str = "", *, session_id: str = "") -> dict[str, Any]:
        if selector:
            with self._page.expect_download(timeout=15_000) as event:
                self._locator(selector).click(timeout=5_000)
            download = event.value
            suggested = download.suggested_filename
            source = download.path()
            data = Path(source).read_bytes() if source else b""
            return downloads.save_download_bytes(session_id, suggested, data, source_url=self._page.url)
        return downloads.fetch_download(session_id, url)

    def extract_links(self, selector: str = "") -> dict[str, Any]:
        script = """
            root => Array.from(root.querySelectorAll('a[href]')).map(a => ({
                text: (a.innerText || a.textContent || '').trim(),
                href: a.href,
                title: a.title || ''
            }))
        """
        links = self._locator(selector).evaluate(script) if selector else self._page.evaluate(script, self._page.locator("body").element_handle())
        return {"url": self._page.url, "links": links if isinstance(links, list) else []}

    def extract_dom(self, selector: str = "") -> dict[str, Any]:
        html_text = self._locator(selector).evaluate("node => node.outerHTML") if selector else self._page.content()
        return {"url": self._page.url, "selector": selector or "document", "html": str(html_text or "")}

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._playwright.stop()

    def _locator(self, selector: str) -> Any:
        if not selector:
            return self._page.locator("body")
        return self._page.locator(selector).first


class StaticController(BrowserController):
    kind = "static_fallback"

    def __init__(self) -> None:
        self.url = ""
        self.title = ""
        self.html = ""
        self.text = ""
        self.links: list[dict[str, str]] = []

    def open_url(self, url: str) -> dict[str, Any]:
        self.url = str(url or "")
        self.html = read_url_text(self.url)
        parsed = ParsedHTML.parse(self.html, base_url=self.url)
        self.title = parsed.title or self.url
        self.text = parsed.text
        self.links = parsed.links
        return self.read_page()

    def read_page(self, selector: str = "") -> dict[str, Any]:
        text = text_for_selector(self.html, selector) if selector else self.text
        return {"url": self.url, "title": self.title, "text": text, "html": self.html, "selector": selector or "body"}

    def screenshot(self, selector: str = "") -> dict[str, Any]:
        return {"url": self.url, "mimeType": "image/png", "bytes": placeholder_png(), "selector": selector}

    def click(self, selector: str) -> dict[str, Any]:
        href = link_href_for_selector(self.html, selector, base_url=self.url)
        if href:
            return self.open_url(href)
        return {"url": self.url, "selector": selector, "static": True}

    def type_text(self, selector: str, text: str) -> dict[str, Any]:
        return {"url": self.url, "selector": selector, "chars": len(text), "static": True}

    def select(self, selector: str, value: str) -> dict[str, Any]:
        return {"url": self.url, "selector": selector, "value": value, "static": True}

    def scroll(self, *, x: int = 0, y: int = 600) -> dict[str, Any]:
        return {"url": self.url, "x": int(x), "y": int(y), "static": True}

    def download(self, url: str = "", selector: str = "", *, session_id: str = "") -> dict[str, Any]:
        target_url = str(url or "")
        if not target_url and selector:
            target_url = link_href_for_selector(self.html, selector, base_url=self.url)
        if not target_url:
            raise AppError("Download URL or link selector is required", code=ErrorCode.INVALID_PAYLOAD, status=400)
        return downloads.fetch_download(session_id, target_url)

    def extract_links(self, selector: str = "") -> dict[str, Any]:
        return {"url": self.url, "links": self.links}

    def extract_dom(self, selector: str = "") -> dict[str, Any]:
        return {"url": self.url, "selector": selector or "document", "html": html_for_selector(self.html, selector) if selector else self.html}


@dataclass(slots=True)
class ParsedHTML:
    title: str
    text: str
    links: list[dict[str, str]]

    @classmethod
    def parse(cls, value: str, *, base_url: str = "") -> "ParsedHTML":
        parser = _TextAndLinksParser(base_url=base_url)
        parser.feed(str(value or ""))
        return cls(title=parser.title.strip(), text="\n".join(part for part in parser.text_parts if part).strip(), links=parser.links)


class _TextAndLinksParser(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.text_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self.title = ""
        self._tag_stack: list[str] = []
        self._current_link: dict[str, str] | None = None
        self._current_link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag_stack.append(tag.lower())
        attr = {key.lower(): str(value or "") for key, value in attrs}
        if tag.lower() == "a" and attr.get("href"):
            self._current_link = {"href": urljoin(self.base_url, attr["href"]), "text": "", "title": attr.get("title", "")}
            self._current_link_text = []

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "a" and self._current_link is not None:
            self._current_link["text"] = " ".join(" ".join(self._current_link_text).split())
            self.links.append(self._current_link)
            self._current_link = None
            self._current_link_text = []
        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        if not data or any(tag in {"script", "style", "noscript"} for tag in self._tag_stack):
            return
        text = " ".join(html.unescape(data).split())
        if not text:
            return
        if self._tag_stack and self._tag_stack[-1] == "title":
            self.title += (" " if self.title else "") + text
        if self._current_link is not None:
            self._current_link_text.append(text)
        self.text_parts.append(text)


def read_url_text(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme == "file":
        path = Path(urllib.request.url2pathname(parsed.path))
        return path.read_text(encoding="utf-8", errors="replace")
    with urllib.request.urlopen(url, timeout=20) as response:
        data = response.read(5_000_000)
        charset = response.headers.get_content_charset() or "utf-8"
    return data.decode(charset, errors="replace")


def placeholder_png() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/l0S6VwAAAABJRU5ErkJggg=="
    )


def text_for_selector(value: str, selector: str) -> str:
    snippet = html_for_selector(value, selector)
    return ParsedHTML.parse(snippet).text if snippet else ParsedHTML.parse(value).text


def html_for_selector(value: str, selector: str) -> str:
    selector = str(selector or "").strip()
    if not selector:
        return str(value or "")
    if selector.startswith("#"):
        return _tag_by_attr(value, "id", selector[1:])
    if selector.startswith("."):
        return _tag_by_class(value, selector[1:])
    return _tag_by_name(value, selector)


def link_href_for_selector(value: str, selector: str, *, base_url: str) -> str:
    snippet = html_for_selector(value, selector)
    match = re.search(r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"']", snippet, flags=re.IGNORECASE)
    return urljoin(base_url, html.unescape(match.group(1))) if match else ""


def _tag_by_attr(value: str, attr: str, expected: str) -> str:
    pattern = rf"<(?P<tag>[A-Za-z][\w:-]*)\b(?=[^>]*\b{re.escape(attr)}=[\"']{re.escape(expected)}[\"'])[^>]*>.*?</(?P=tag)>"
    return _first_regex(value, pattern)


def _tag_by_class(value: str, class_name: str) -> str:
    pattern = rf"<(?P<tag>[A-Za-z][\w:-]*)\b(?=[^>]*\bclass=[\"'][^\"']*\b{re.escape(class_name)}\b[^\"']*[\"'])[^>]*>.*?</(?P=tag)>"
    return _first_regex(value, pattern)


def _tag_by_name(value: str, tag: str) -> str:
    safe_tag = re.escape(str(tag or "").strip().split()[0] if str(tag or "").strip() else "body")
    return _first_regex(value, rf"<(?P<tag>{safe_tag})\b[^>]*>.*?</(?P=tag)>")


def _first_regex(value: str, pattern: str) -> str:
    match = re.search(pattern, str(value or ""), flags=re.IGNORECASE | re.DOTALL)
    return match.group(0) if match else ""
