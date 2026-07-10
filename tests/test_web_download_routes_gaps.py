"""Gap tests for download routes."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.web.routes.downloads import DownloadsRouteDeps, create_downloads_router


@pytest.fixture
def downloads_client() -> Iterator[TestClient]:
    deps = DownloadsRouteDeps(
        resolve_generated_file=lambda file_id: None,
        download_descriptor=lambda path: ("application/octet-stream", "file.bin"),
    )
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_downloads_router(deps))
    with patch("deepseek_infra.web.routes.downloads.require_api_auth", lambda request: None):
        yield TestClient(app)


def test_download_missing_file_returns_404(downloads_client: TestClient) -> None:
    resp = downloads_client.get("/api/download?id=missing")
    assert resp.status_code == 404


def test_download_serves_existing_file(downloads_client: TestClient) -> None:
    path = Path("C:/Users/12393/AppData/Local/Temp/opencode") / "gap_download.bin"
    path.write_bytes(b"data")
    try:
        deps = DownloadsRouteDeps(
            resolve_generated_file=lambda file_id: path,
            download_descriptor=lambda path: ("application/octet-stream", "file.bin"),
        )
        app = FastAPI()

        @app.exception_handler(AppError)
        async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
            return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

        app.include_router(create_downloads_router(deps))
        with patch("deepseek_infra.web.routes.downloads.require_api_auth", lambda request: None):
            client = TestClient(app)
            resp = client.get("/api/download?id=exists")
        assert resp.status_code == 200
        assert resp.content == b"data"
    finally:
        path.unlink(missing_ok=True)
