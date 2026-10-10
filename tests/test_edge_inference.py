from __future__ import annotations

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.gateway.edge_inference import EdgeOptions, decide_edge_route, edge_manager


def _fake_options() -> EdgeOptions:
    return EdgeOptions(
        enabled=True,
        provider="fake",
        model_path="",
        model_name="edge-fake",
        chat_format="",
        n_ctx=4096,
        n_threads=0,
        n_gpu_layers=0,
        max_tokens=128,
        temperature=0.0,
        top_p=1.0,
        simple_max_chars=6000,
    )


def _decision(payload: dict[str, object], *, cloud_available: bool = True, available: bool = True):
    options = _fake_options()
    status = edge_manager.status(options)
    if not available:
        status = {**status, "available": False, "dependencyAvailable": False}
    return decide_edge_route(payload, options=options, status=status, cloud_available=cloud_available)


def test_fake_provider_simple_query_routes_to_edge() -> None:
    route = _decision({"messages": [{"role": "user", "content": "Summarize this note."}]})

    assert route.use_edge is True
    assert route.reason == "simple_task_local"
    assert route.provider == "fake"


def test_current_news_query_routes_to_cloud() -> None:
    route = _decision({"messages": [{"role": "user", "content": "What is the latest news today?"}]})

    assert route.use_edge is False
    assert route.reason == "complex_task_cloud"


def test_image_attachment_routes_to_cloud() -> None:
    route = _decision(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Describe this image.",
                    "attachments": [{"imageData": "data:image/png;base64,AAAA"}],
                }
            ]
        }
    )

    assert route.use_edge is False
    assert route.reason == "unsupported_payload"


def test_simple_query_falls_back_to_edge_when_cloud_unavailable() -> None:
    route = _decision({"messages": [{"role": "user", "content": "hello"}]}, cloud_available=False)

    assert route.use_edge is True
    assert route.reason == "cloud_unavailable_simple_local"


def test_forced_local_unavailable_returns_409() -> None:
    with pytest.raises(AppError) as exc:
        _decision({"edgeMode": "local", "messages": [{"role": "user", "content": "hello"}]}, available=False)

    assert exc.value.status == 409


def test_unsupported_provider_status_suggests_provider_fix() -> None:
    options = EdgeOptions(
        enabled=True,
        provider="unknown_provider",
        model_path="",
        model_name="edge-bad",
        chat_format="",
        n_ctx=4096,
        n_threads=0,
        n_gpu_layers=0,
        max_tokens=128,
        temperature=0.0,
        top_p=1.0,
        simple_max_chars=6000,
    )

    status = edge_manager.status(options)

    assert status["providerSupported"] is False
    assert status["available"] is False
    assert any("EDGE_PROVIDER" in suggestion for suggestion in status["suggestions"])
    assert not any("Install" in suggestion for suggestion in status["suggestions"])
