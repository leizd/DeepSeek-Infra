"""Gap tests for media routes."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.web.routes.media import MediaRouteDeps, create_media_router


@pytest.fixture
def media_client() -> Iterator[tuple[TestClient, Any]]:
    deps = MediaRouteDeps(read_multipart_form=lambda request: ({"title": ["x"]}, []))
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_media_router(deps))
    with patch("deepseek_infra.web.routes.media.require_api_auth", lambda request: None):
        yield TestClient(app), deps


def test_media_patch_rejects_non_object(media_client: tuple[TestClient, Any]) -> None:
    client, _ = media_client
    with patch("deepseek_infra.web.routes.media.read_json_body", return_value=["not", "an", "object"]) as read_json:
        resp = client.patch("/api/media/m1", json={"title": "x"})
    assert resp.status_code == 400
    read_json.assert_called_once()


def test_media_patch_accepts_object(media_client: tuple[TestClient, Any]) -> None:
    client, _ = media_client
    with patch("deepseek_infra.web.routes.media.library.update_media", return_value={"mediaId": "m1"}) as update:
        resp = client.patch("/api/media/m1", json={"title": "New"})
    assert resp.status_code == 200
    update.assert_called_once()
