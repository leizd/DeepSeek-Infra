from __future__ import annotations

from pathlib import Path

from deepseek_infra.web.http_utils import apply_common_headers
from starlette.responses import Response


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
SERVER_PATH = ROOT / "deepseek_infra" / "web" / "server.py"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_legacy_frontend_is_retired() -> None:
    assert not (STATIC_DIR / "index.html").exists()
    assert not (STATIC_DIR / "app.js").exists()
    assert not (STATIC_DIR / "modules" / "chat.js").exists()
    assert not (STATIC_DIR / "sw.js").exists()
    assert not (STATIC_DIR / "manifest.webmanifest").exists()

    server_source = SERVER_PATH.read_text(encoding="utf-8")
    assert "DEEPSEEK_FRONTEND" not in server_source
    assert '["legacy"]' not in server_source


def test_react_root_pwa_sources_are_owned_by_frontend_build() -> None:
    index = read("frontend/index.html")
    root_worker = read("frontend/public/sw-root.js")
    root_manifest = read("frontend/public/manifest-root.webmanifest")
    main = read("frontend/src/main.tsx")

    assert '<link rel="manifest" href="/manifest.webmanifest" />' in index
    assert 'register(atRoot ? "/sw.js" : "/ui/sw.js"' in main
    assert 'const CACHE_NAME = "deepseek-react-root-' in root_worker
    assert 'url.pathname.startsWith("/api/")' in root_worker
    assert '"start_url": "/"' in root_manifest
    assert '"scope": "/"' in root_manifest
    assert '"share_target"' in root_manifest


def test_react_runtime_keeps_csp_safe_local_assets() -> None:
    index = read("frontend/index.html")
    assert "fonts.googleapis.com" not in index
    assert "fonts.gstatic.com" not in index
    assert '/icons/favicon.svg' in index
    assert (ROOT / "static" / "icons" / "favicon.svg").is_file()

    response = Response()
    apply_common_headers(response, "/")
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "font-src 'self'" in csp


def test_trace_viewer_is_owned_by_react() -> None:
    assert not (STATIC_DIR / "trace_viewer.html").exists()
    assert not (STATIC_DIR / "modules" / "trace_viewer.js").exists()
    assert not (STATIC_DIR / "modules" / "trace_waterfall.js").exists()

    app = read("frontend/src/app/App.tsx")
    diagnostics = read("frontend/src/features/diagnostics/DiagnosticsDrawer.tsx")
    detail = read("frontend/src/features/trace/TraceDetailView.tsx")
    assert 'path="/trace/:traceId"' in app
    assert '<TraceDetailView traceId={traceId} variant="drawer" />' in diagnostics
    assert "<TraceSpanTree" in detail
    assert "<TraceWaterfall" in detail
