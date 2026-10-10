"""Shared HTTP helpers for the DeepSeek Infra FastAPI surface."""

from __future__ import annotations

import json
import re
import secrets
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from deepseek_infra.core.config import settings
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import clean_filename, local_ip

AUTH_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
IMMUTABLE_FRONTEND_CACHE_CONTROL = "public, max-age=31536000, immutable"
_BUILD_SCOPED_FRONTEND_PATH = re.compile(
    r"^/(?:ui/)?(?:sw(?:-root)?-[0-9a-f]{16}\.js|workspace-assets-[0-9a-f]{16}\.json)$"
)
_HASHED_FRONTEND_ASSET_PATH = re.compile(
    r"^/ui/assets/.+-[A-Za-z0-9_-]{8,}\.(?:css|js|mjs|png|svg|webp|woff2?)$",
    re.IGNORECASE,
)


def frontend_cache_control(path: str, content_type: str = "") -> str:
    """Return the explicit cache contract for API and frontend resources."""
    if path.startswith("/api/"):
        return "no-store"
    if path in {"/index.html", "/ui/index.html", "/ui/workspace-assets.json"}:
        return "no-store"
    if content_type.lower().startswith("text/html"):
        return "no-store"
    if _BUILD_SCOPED_FRONTEND_PATH.fullmatch(path) or _HASHED_FRONTEND_ASSET_PATH.fullmatch(path):
        return IMMUTABLE_FRONTEND_CACHE_CONTROL
    return "no-cache"


def apply_common_headers(response: Response, path: str) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if path == "/api/file-source":
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'self'; "
            "base-uri 'self'; "
            "frame-ancestors 'self'"
        )
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: http: https:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = frontend_cache_control(path, response.headers.get("Content-Type", ""))


def json_response(data: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=headers)


def require_api_auth(request: Request) -> None:
    if not settings.auth.enabled:
        return
    require_allowed_host(request)
    provided = auth_token_from_headers(request.headers.get("Authorization", ""), request.headers.get("Cookie", ""))
    if not secrets.compare_digest(provided, settings.auth.token):
        raise AppError("Auth required", code=ErrorCode.UNAUTHORIZED, status=401)


def require_allowed_host(request: Request) -> None:
    host = host_without_port(request.headers.get("Host", ""))
    if host not in allowed_auth_hosts():
        raise AppError("Host not allowed", code=ErrorCode.FORBIDDEN, status=403)


async def read_json_body(request: Request, max_bytes: int = 2_000_000) -> dict[str, Any]:
    content_length = parse_content_length(request.headers.get("Content-Length", "0"))
    if content_length <= 0:
        raise AppError("Request body is empty", code=ErrorCode.INVALID_PAYLOAD)
    if content_length > max_bytes:
        raise AppError("Request body is too large", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
    raw = await request.body()
    try:
        body = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise AppError(f"Invalid JSON: {exc}", code=ErrorCode.INVALID_PAYLOAD) from exc
    if not isinstance(body, dict):
        raise AppError("Request body must be a JSON object", code=ErrorCode.INVALID_PAYLOAD)
    return body


def request_port(request: Request) -> int:
    server = request.scope.get("server")
    if isinstance(server, tuple) and len(server) >= 2:
        try:
            return int(server[1])
        except (TypeError, ValueError):
            pass
    host = str(request.headers.get("Host") or "")
    try:
        parsed = urlsplit(f"http://{host}")
        return int(parsed.port or 80)
    except (TypeError, ValueError):
        return 0


def request_base_url(request: Request) -> str:
    host_header = str(request.headers.get("Host") or "").split(",", 1)[0].strip()
    if host_header and "/" not in host_header and "\\" not in host_header and host_without_port(host_header) in allowed_auth_hosts():
        return f"http://{host_header}"
    port = request_port(request)
    return f"http://127.0.0.1:{port}" if port else "http://127.0.0.1"


def parse_content_length(value: str) -> int:
    try:
        content_length = int(value)
    except (TypeError, ValueError) as exc:
        raise AppError("Invalid Content-Length", code=ErrorCode.INVALID_PAYLOAD) from exc
    if content_length < 0:
        raise AppError("Invalid Content-Length", code=ErrorCode.INVALID_PAYLOAD)
    return content_length


def auth_cookie_header(token: str) -> str:
    cookie = SimpleCookie()
    cookie["auth_token"] = token
    cookie["auth_token"]["path"] = "/"
    cookie["auth_token"]["samesite"] = "Strict"
    cookie["auth_token"]["httponly"] = True
    cookie["auth_token"]["max-age"] = str(AUTH_COOKIE_MAX_AGE_SECONDS)
    return cookie.output(header="").strip()


def expired_auth_cookie_header() -> str:
    cookie = SimpleCookie()
    cookie["auth_token"] = ""
    cookie["auth_token"]["path"] = "/"
    cookie["auth_token"]["samesite"] = "Strict"
    cookie["auth_token"]["httponly"] = True
    cookie["auth_token"]["max-age"] = "0"
    return cookie.output(header="").strip()


def host_without_port(value: str) -> str:
    host = value.strip().split(",", 1)[0].strip()
    if host.startswith("["):
        return host.split("]", 1)[0].lstrip("[").lower()
    return host.split(":", 1)[0].lower()


def allowed_auth_hosts() -> set[str]:
    hosts = {"localhost", "127.0.0.1", "::1"}
    configured_host = settings.default_host.strip().lower()
    if configured_host and configured_host != "0.0.0.0":
        hosts.add(configured_host)
    lan_ip = local_ip()
    if lan_ip:
        hosts.add(lan_ip.lower())
    hosts.update(host_without_port(host) for host in settings.auth.allowed_hosts)
    return {host for host in hosts if host}


def allowed_cors_origin(origin: str, port: int) -> str:
    origin = origin.strip()
    if not origin or port <= 0:
        return ""
    try:
        parsed = urlsplit(origin)
        origin_port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme != "http" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        return ""
    if origin_port is None:
        origin_port = 80
    if origin_port != port:
        return ""
    host = (parsed.hostname or "").lower()
    if host not in allowed_auth_hosts():
        return ""
    return origin


def content_disposition_header(disposition: str, filename: str) -> str:
    safe_name = clean_filename(filename)
    ascii_name = safe_name.encode("ascii", errors="ignore").decode("ascii") or "document"
    ascii_name = ascii_name.replace('"', "")
    return f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(safe_name)}'


def auth_token_from_headers(authorization: str, cookie_header: str) -> str:
    prefix = "bearer "
    authorization = authorization.strip()
    if authorization.lower().startswith(prefix):
        return authorization[len(prefix) :].strip()

    if cookie_header:
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get("auth_token")
        if morsel is not None:
            return morsel.value
    return ""


def truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
