"""Dedicated, cryptographically authenticated Federation HTTP surface."""

from __future__ import annotations

import ipaddress
import logging
import secrets
import tempfile
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.web.http_utils import apply_common_headers, json_response, parse_content_length, read_json_body

MAX_FEDERATION_JSON_BYTES = 8 * 1024 * 1024
MAX_FEDERATION_COMPONENT_BYTES = 512 * 1024 * 1024
OPERATOR_TOKEN_HEADER = "X-Federation-Operator-Token"

logger = logging.getLogger(__name__)


class FederationHttpError(RuntimeError):
    """Transport-level fail-closed error with a stable public code."""

    def __init__(self, code: str, *, status: int = 409) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _domain_status(code: str) -> int:
    if code in {"FEDERATION_OPERATOR_AUTH_REQUIRED"}:
        return 401
    if code in {
        "FEDERATION_OPERATOR_LOOPBACK_REQUIRED",
        "FEDERATION_PEER_REVOKED",
        "FEDERATION_PEER_NOT_ACTIVE",
    }:
        return 403
    if code.endswith("_NOT_FOUND") or code == "FEDERATION_TRANSFER_NOT_FOUND":
        return 404
    if code.endswith("_TOO_LARGE") or code == "FEDERATION_COMPONENT_TOO_LARGE":
        return 413
    return 409


def _error_response(code: str, status: int) -> JSONResponse:
    return json_response({"error": code, "code": code}, status=status)


def _is_loopback(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def _require_operator(request: Request, token: str) -> None:
    if not _is_loopback(request):
        raise FederationHttpError("FEDERATION_OPERATOR_LOOPBACK_REQUIRED", status=403)
    supplied = request.headers.get(OPERATOR_TOKEN_HEADER, "")
    if not supplied or not secrets.compare_digest(supplied, token):
        raise FederationHttpError("FEDERATION_OPERATOR_AUTH_REQUIRED", status=401)


async def _json(request: Request) -> dict[str, Any]:
    return await read_json_body(request, max_bytes=MAX_FEDERATION_JSON_BYTES)


def create_federation_app(*, node: Any, operator_token: str) -> FastAPI:
    """Create the dedicated Federation API without exposing local workspace auth."""

    if not isinstance(operator_token, str) or len(operator_token) < 16:
        raise ValueError("federation operator token must contain at least 16 characters")
    app = FastAPI(title="DeepSeek Infra Federation", version="1")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        apply_common_headers(response, request.url.path)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return json_response(exc.to_response(), status=exc.status)

    @app.exception_handler(FederationHttpError)
    async def federation_http_error_handler(_: Request, exc: FederationHttpError) -> JSONResponse:
        return _error_response(exc.code, exc.status)

    @app.exception_handler(Exception)
    async def federation_domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code and all(character.isupper() or character.isdigit() or character == "_" for character in code):
            return _error_response(code, _domain_status(code))
        logger.exception("federation_route_error", extra={"path": request.url.path})
        return _error_response(ErrorCode.INTERNAL.value, 500)

    @app.get("/federation/v1/health")
    async def health() -> JSONResponse:
        return json_response(await run_in_threadpool(node.health))

    @app.post("/federation/v1/peer/readiness")
    async def readiness(request: Request) -> JSONResponse:
        await _json(request)
        return json_response(await run_in_threadpool(node.issue_readiness))

    @app.post("/federation/v1/peer/challenges/respond")
    async def respond_challenge(request: Request) -> JSONResponse:
        return json_response(await run_in_threadpool(node.respond_challenge, await _json(request)))

    @app.post("/federation/v1/peer/ingress-grants")
    async def issue_ingress_grant(request: Request) -> JSONResponse:
        return json_response(await run_in_threadpool(node.issue_ingress_grant, await _json(request)))

    @app.post("/federation/v1/peer/transfers/{transfer_id}/declaration")
    async def declare_replica(transfer_id: str, request: Request) -> JSONResponse:
        return json_response(await run_in_threadpool(node.declare_replica, transfer_id, await _json(request)))

    @app.put("/federation/v1/peer/transfers/{transfer_id}/components/{component_digest}")
    async def receive_component(transfer_id: str, component_digest: str, request: Request) -> JSONResponse:
        grant_id = str(request.query_params.get("grantId") or "")
        write_id = str(request.query_params.get("writeId") or "")
        expected_size = int(await run_in_threadpool(node.expected_component_size, transfer_id, component_digest, grant_id))
        if expected_size <= 0 or expected_size > MAX_FEDERATION_COMPONENT_BYTES:
            raise FederationHttpError("FEDERATION_COMPONENT_TOO_LARGE", status=413)
        content_length = parse_content_length(request.headers.get("Content-Length", "0"))
        if content_length != expected_size:
            raise FederationHttpError("FEDERATION_COMPONENT_LENGTH_MISMATCH")
        observed = 0
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as stream:
            async for chunk in request.stream():
                observed += len(chunk)
                if observed > expected_size:
                    raise FederationHttpError("FEDERATION_COMPONENT_LENGTH_MISMATCH")
                stream.write(chunk)
            if observed != expected_size:
                raise FederationHttpError("FEDERATION_COMPONENT_LENGTH_MISMATCH")
            stream.seek(0)
            result = await run_in_threadpool(
                node.receive_component,
                transfer_id,
                component_digest,
                grant_id=grant_id,
                write_id=write_id,
                content=stream,
            )
        return json_response(result)

    @app.get("/federation/v1/peer/transfers/{transfer_id}")
    async def reconcile_transfer(transfer_id: str, request: Request) -> JSONResponse:
        grant_id = str(request.query_params.get("grantId") or "")
        return json_response(await run_in_threadpool(node.reconcile_transfer, transfer_id, grant_id))

    @app.post("/federation/v1/peer/transfers/{transfer_id}/commit")
    async def commit_replica(transfer_id: str, request: Request) -> JSONResponse:
        return json_response(await run_in_threadpool(node.commit_replica, transfer_id, await _json(request)))

    @app.post("/federation/v1/operator/challenges")
    async def issue_challenge(request: Request) -> JSONResponse:
        _require_operator(request, operator_token)
        return json_response(await run_in_threadpool(node.issue_challenge, await _json(request)))

    @app.post("/federation/v1/operator/challenges/verify")
    async def verify_challenge(request: Request) -> JSONResponse:
        _require_operator(request, operator_token)
        return json_response(await run_in_threadpool(node.verify_challenge, await _json(request)))

    @app.post("/federation/v1/operator/readiness/verify")
    async def verify_readiness(request: Request) -> JSONResponse:
        _require_operator(request, operator_token)
        return json_response(await run_in_threadpool(node.verify_readiness, await _json(request)))

    @app.post("/federation/v1/operator/transfers")
    async def propose_transfer(request: Request) -> JSONResponse:
        _require_operator(request, operator_token)
        return json_response(await run_in_threadpool(node.propose_transfer, await _json(request)))

    @app.post("/federation/v1/operator/ingress-grants/verify")
    async def verify_ingress_grant(request: Request) -> JSONResponse:
        _require_operator(request, operator_token)
        return json_response(await run_in_threadpool(node.verify_ingress_grant, await _json(request)))

    @app.post("/federation/v1/operator/transfers/{transfer_id}/remote-verifying")
    async def mark_remote_verifying(transfer_id: str, request: Request) -> JSONResponse:
        _require_operator(request, operator_token)
        return json_response(await run_in_threadpool(node.mark_remote_verifying, transfer_id, await _json(request)))

    @app.post("/federation/v1/operator/transfers/{transfer_id}/replica-attestations/verify")
    async def verify_replica_attestation(transfer_id: str, request: Request) -> JSONResponse:
        _require_operator(request, operator_token)
        return json_response(await run_in_threadpool(node.verify_replica_attestation, transfer_id, await _json(request)))

    @app.post("/federation/v1/operator/transfers/{transfer_id}/dr-drills")
    async def run_dr_drill(transfer_id: str, request: Request) -> JSONResponse:
        _require_operator(request, operator_token)
        return json_response(await run_in_threadpool(node.run_dr_drill, transfer_id, await _json(request)))

    @app.post("/federation/v1/operator/transfers/{transfer_id}/dr-attestations/verify")
    async def verify_dr_attestation(transfer_id: str, request: Request) -> JSONResponse:
        _require_operator(request, operator_token)
        return json_response(await run_in_threadpool(node.verify_dr_attestation, transfer_id, await _json(request)))

    return app
