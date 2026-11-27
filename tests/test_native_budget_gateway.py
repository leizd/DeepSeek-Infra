"""Native gateway budget status.

``GET /api/budget`` is served by the prebuilt gateway. Auth enabled refuses
a missing token. The configured bearer token returns the default ledger
view for scope ``global``. ``scope=agent`` selects that scope. A blank
scope stays ``global``. A missing budget database is not created. Python's
status router and budget helpers are not the owners of those HTTP reads.
"""

from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.gateway import budget_manager
from deepseek_infra.web.routes import status as status_routes

ROOT = Path(__file__).resolve().parents[1]
# The native gateway is a prebuilt Rust binary. A developer checkout and the
# Linux CI runner do not share a path, so resolve it from an explicit override,
# then the conventional build outputs, and skip the module when none exists. A
# missing binary must never be reported as a product failure.
GATEWAY_BINARY_ENV = "DEEPSEEK_GATEWAY_BINARY"
GATEWAY_BINARY_CANDIDATES = (
    ROOT / "bin" / "deepseek-gateway",
    ROOT / "bin" / "deepseek-gateway.exe",
    ROOT / "rust" / "target" / "release" / "deepseek-gateway",
    ROOT / "rust" / "target" / "release" / "deepseek-gateway.exe",
)


def _resolve_native_gateway() -> Path | None:
    override = os.environ.get(GATEWAY_BINARY_ENV, "").strip()
    candidates = (Path(override),) if override else GATEWAY_BINARY_CANDIDATES
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# Kept separate so the skip decision can test for absence while NATIVE_GATEWAY
# stays a plain Path for the call sites and the type checker.
_GATEWAY_BINARY = _resolve_native_gateway()
NATIVE_GATEWAY = _GATEWAY_BINARY or Path("deepseek-gateway-not-built")

pytestmark = pytest.mark.skipif(
    _GATEWAY_BINARY is None,
    reason=(
        "prebuilt native gateway is required; set "
        f"{GATEWAY_BINARY_ENV} or build rust/ -p deepseek-gateway --release"
    ),
)
TOKEN = "budget-auth-token"
PRICES = {
    "deepseek-v4-pro": (0.55, 2.19),
    "deepseek-v4-flash": (0.27, 1.10),
}
ZERO_COUNTERS = (
    "promptTokens",
    "completionTokens",
    "totalTokens",
    "modelCalls",
    "searchCalls",
    "toolCalls",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(port: int, token: str = "", query: str = "") -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/budget{query}",
        headers=headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = exc.read()
    parsed = json.loads(payload.decode("utf-8"))
    assert isinstance(parsed, dict)
    return status, parsed


def _child_env(tmp_settings: Path, port: int) -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "AUTH_TOKEN",
        "AUTH_DISABLED",
        "DEEPSEEK_RUNTIME_MODE",
        "BUDGET_TRACKING_ENABLED",
        "BUDGET_MAX_TOTAL_TOKENS",
        "BUDGET_MAX_AGENT_TOKENS",
        "BUDGET_MAX_SEARCH_CALLS",
        "BUDGET_MAX_TOOL_CALLS",
        "BUDGET_MAX_ESTIMATED_COST_USD",
        "BUDGET_POLICY",
        "BUDGET_PRICE_PRO_INPUT",
        "BUDGET_PRICE_PRO_OUTPUT",
        "BUDGET_PRICE_FLASH_INPUT",
        "BUDGET_PRICE_FLASH_OUTPUT",
    ):
        env.pop(name, None)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-budget.log"
    log_handle = log_path.open("wb")
    process = subprocess.Popen(
        [str(NATIVE_GATEWAY)],
        cwd=str(NATIVE_GATEWAY.parent),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 20
    listening = False
    while time.time() < deadline:
        if process.poll() is not None:
            break
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                listening = True
                break
        except OSError:
            time.sleep(0.1)
    if not listening:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
        raise AssertionError(log_path.read_text(encoding="utf-8", errors="replace"))
    return process, log_handle


def _assert_zero_spend(spend: dict[str, Any], scope: str, day: str) -> None:
    assert spend["scope"] == scope
    assert spend["day"] == day
    for name in ZERO_COUNTERS:
        assert spend[name] == 0
    assert spend["costUsd"] == 0 or spend["costUsd"] == 0.0


def _assert_budget(body: dict[str, Any], scope: str, day: str, database: Path) -> None:
    assert body["ok"] is True
    budget = body["budget"]
    assert isinstance(budget, dict)
    assert budget["enabled"] is True
    assert budget["scope"] == scope
    assert Path(str(budget["databasePath"])) == database
    assert budget["lastError"] == ""
    policy = budget["policy"]
    assert policy["maxTotalTokens"] == 0
    assert policy["maxAgentTokens"] == 0
    assert policy["maxSearchCalls"] == 0
    assert policy["maxToolCalls"] == 0
    assert policy["maxEstimatedCostUsd"] == 0 or policy["maxEstimatedCostUsd"] == 0.0
    assert policy["policy"] == "none"
    pricing = budget["pricing"]
    assert set(pricing) == set(PRICES)
    for model, (input_price, output_price) in PRICES.items():
        assert math.isclose(pricing[model]["inputPerMTok"], input_price, abs_tol=1e-9)
        assert math.isclose(pricing[model]["outputPerMTok"], output_price, abs_tol=1e-9)
    today = budget["today"]
    assert isinstance(today, dict)
    _assert_zero_spend(today, scope, day)
    over = budget["overBudget"]
    assert over["exceeded"] is False
    assert over["reasons"] == []
    _assert_zero_spend(over["spend"], scope, day)


def test_budget_status_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_status(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python status router entered")

    def _refuse_python_budget(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python budget status entered")

    database = tmp_settings / ".budget" / "budget.db"
    assert not database.exists()
    monkeypatch.setattr(status_routes, "create_status_router", _refuse_python_status)
    monkeypatch.setattr(budget_manager, "budget_status", _refuse_python_budget)
    monkeypatch.setattr(budget_manager, "daily_spend", _refuse_python_budget)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        missing_status, missing = _get(port)
        assert missing_status == 401
        assert missing == {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
        wrong_status, wrong = _get(port, "not-the-budget-token")
        assert wrong_status == 401
        assert wrong == {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
        ok_status, ok_body = _get(port, TOKEN)
        assert ok_status == 200
        _assert_budget(ok_body, "global", day, database)
        agent_status, agent = _get(port, TOKEN, "?scope=agent")
        assert agent_status == 200
        _assert_budget(agent, "agent", day, database)
        blank_status, blank = _get(port, TOKEN, "?scope=%20%20")
        assert blank_status == 200
        _assert_budget(blank, "global", day, database)
        assert not database.exists()
        assert not database.parent.exists()
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
