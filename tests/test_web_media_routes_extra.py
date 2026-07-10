"""Extra tests for media routes to cover edge cases."""

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
from deepseek_infra.web.routes.media import MediaRouteDeps, create_media_router


async def _read_multipart_form(_request: Any) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    return {"title": ["Upload"]}, [{"filename": "f.png", "content_type": "image/png", "data": b"data"}]


async def _read_multipart_too_many(_request: Any) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    uploads = [{"filename": f"f{i}.png", "content_type": "image/png", "data": b"x"} for i in range(25)]
    return {}, uploads


@pytest.fixture
def client(tmp_settings: Path) -> Iterator[TestClient]:
    deps = MediaRouteDeps(read_multipart_form=_read_multipart_form)
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_media_router(deps))
    with patch("deepseek_infra.web.routes.media.require_api_auth", lambda request: None):
        yield TestClient(app)


@pytest.fixture
def client_too_many(tmp_settings: Path) -> Iterator[TestClient]:
    deps = MediaRouteDeps(read_multipart_form=_read_multipart_too_many)
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_media_router(deps))
    with patch("deepseek_infra.web.routes.media.require_api_auth", lambda request: None):
        yield TestClient(app)


def test_media_upload_via_multipart(client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.media.ingestion.ingest_upload", return_value={"mediaId": "m1"}):
        boundary = "----WebKitFormBoundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="f.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
            f"data\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        resp = client.post(
            "/api/media?projectId=p1",
            content=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
    assert resp.status_code == 200


def test_media_upload_too_many(client_too_many: TestClient) -> None:
    boundary = "----WebKitFormBoundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="f.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
        f"data\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    resp = client_too_many.post(
        "/api/media",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert resp.status_code == 413


def test_media_upload_no_files(client: TestClient) -> None:
    async def empty_form(_request: Any) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
        return {}, []

    deps = MediaRouteDeps(read_multipart_form=empty_form)
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_media_router(deps))
    with patch("deepseek_infra.web.routes.media.require_api_auth", lambda request: None):
        test_client = TestClient(app)
    boundary = "----WebKitFormBoundary"
    body = f"--{boundary}--\r\n".encode("utf-8")
    resp = test_client.post(
        "/api/media",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert resp.status_code == 400


def test_media_json_register(client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.media.ingestion.register_from_payload", return_value={"mediaId": "m1"}):
        resp = client.post("/api/media", json={"type": "webpage", "title": "T", "html": "<h1>X</h1>"})
    assert resp.status_code == 200


def test_media_list_get(client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.media.library.list_media", return_value=[]):
        resp = client.get("/api/media?projectId=p1&type=webpage&status=ready")
    assert resp.status_code == 200

    with patch("deepseek_infra.web.routes.media.library.get_media", return_value={"mediaId": "m1"}):
        resp = client.get("/api/media/m1")
    assert resp.status_code == 200


def test_media_patch_invalid_and_valid(client: TestClient) -> None:
    resp = client.patch("/api/media/m1", json={"unknown": "x"})
    assert resp.status_code == 400

    resp = client.patch("/api/media/m1", json="not an object")
    assert resp.status_code == 400

    with patch("deepseek_infra.web.routes.media.library.update_media", return_value={"mediaId": "m1"}):
        resp = client.patch("/api/media/m1", json={"title": "T2"})
    assert resp.status_code == 200


def test_media_process(client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.media.ingestion.process_media", return_value={"mediaId": "m1", "status": "ready"}):
        resp = client.post("/api/media/m1/process", json={"force": True})
    assert resp.status_code == 200


def test_media_segments(client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.media.library.get_media", return_value={"mediaId": "m1"}), \
         patch("deepseek_infra.web.routes.media.library.list_segments", return_value=[]):
        resp = client.get("/api/media/m1/segments")
    assert resp.status_code == 200


def test_media_delete(client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.media.library.delete_media", return_value=1):
        resp = client.delete("/api/media/m1")
    assert resp.status_code == 200


def test_media_upload_unsupported_type(client: TestClient) -> None:
    async def bad_form(_request: Any) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
        return {}, [{"filename": "f.exe", "content_type": "application/x-msdownload", "data": b"data"}]

    deps = MediaRouteDeps(read_multipart_form=bad_form)
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_media_router(deps))
    with patch("deepseek_infra.web.routes.media.require_api_auth", lambda request: None):
        test_client = TestClient(app)
    boundary = "----WebKitFormBoundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="f.exe"\r\n'
        f"Content-Type: application/x-msdownload\r\n\r\n"
        f"data\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    resp = test_client.post(
        "/api/media",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert resp.status_code == 400
