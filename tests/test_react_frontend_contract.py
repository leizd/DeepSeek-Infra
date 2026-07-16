from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_react_frontend_is_an_isolated_versioned_build() -> None:
    package = json.loads(read("frontend/package.json"))
    assert package["version"] == "4.0.2"
    assert package["engines"]["node"] == ">=22.12.0"
    assert package["scripts"]["build"] == "tsc --noEmit && vite build"
    assert package["scripts"]["test"] == "vitest run"
    assert package["dependencies"] == {"react": "19.2.7", "react-dom": "19.2.7"}
    assert (ROOT / "frontend/package-lock.json").is_file()
    assert "registry=https://registry.npmjs.org/" in read("frontend/.npmrc")
    assert "registry.npmmirror.com" not in read("frontend/package-lock.json")

    vite = read("frontend/vite.config.ts")
    assert 'base: "/ui/"' in vite
    assert 'new URL("../static/ui", import.meta.url)' in vite
    assert '"/api": "http://127.0.0.1:8000"' in vite
    assert "static/ui/" in read(".gitignore")


def test_react_migration_starts_with_typed_protocol_boundaries() -> None:
    required = (
        "frontend/src/domain/chat/types.ts",
        "frontend/src/domain/chat/streamReducer.ts",
        "frontend/src/domain/conversation/types.ts",
        "frontend/src/api/httpClient.ts",
        "frontend/src/api/chatStream.ts",
    )
    for path in required:
        assert (ROOT / path).is_file(), path

    reducer = read("frontend/src/domain/chat/streamReducer.ts")
    stream = read("frontend/src/api/chatStream.ts")
    client = read("frontend/src/api/httpClient.ts")
    assert "export function applyStreamEvent" in reducer
    assert "document." not in reducer and "fetch(" not in reducer
    assert "export async function* streamChat" in stream
    assert "AsyncGenerator<ChatStreamEvent>" in stream
    assert "localStorage" not in client and "sessionStorage" not in client


def test_build_packaging_and_ci_include_the_react_frontend() -> None:
    workflow = read(".github/workflows/ci.yml")
    dockerfile = read("Dockerfile")
    release = read("scripts/release.py")
    exe = read("scripts/build_exe.py")
    agents = read("AGENTS.md")

    assert "  frontend:" in workflow
    for command in (
        "npm ci --prefix frontend",
        "npm run typecheck --prefix frontend",
        "npm test --prefix frontend",
        "npm run build --prefix frontend",
    ):
        assert command in workflow
    assert "RC_CI_FRONTEND" in workflow
    assert "FROM node:24-bookworm-slim AS frontend-builder" in dockerfile
    assert "COPY --from=frontend-builder /build/static/ui ./static/ui" in dockerfile
    assert "build_frontend(root)" in release
    assert "FRONTEND_BUILD_SCRIPT" in exe
    assert "isolated React + TypeScript + Vite app" in agents


def test_browser_gate_covers_react_preview_and_spa_fallback() -> None:
    smoke = read("scripts/smoke_frontend_browser.py")
    preflight = read("scripts/preflight_release.py")
    assert 'checks["reactPreview"] = "PASS"' in smoke
    assert "ui/projects/example" in smoke
    assert '"reactPreview"' in preflight
