from __future__ import annotations

import http.client
import json
import threading
import unittest
from http.cookies import SimpleCookie

import deepseek_infra.web.server as server_module
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import url_with_token
from deepseek_infra.web.server import FastAPIServer


class ErrorTests(unittest.TestCase):
    def make_server(self) -> tuple[FastAPIServer, threading.Thread]:
        server, _ = server_module.create_server(0, host="127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def request_json(
        self,
        running_server: FastAPIServer,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> tuple[int, dict[str, object], http.client.HTTPResponse]:
        connection = http.client.HTTPConnection("127.0.0.1", running_server.server_address[1], timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8") or "{}")
            return response.status, payload, response
        finally:
            connection.close()

    def test_app_error_to_response_includes_stable_code(self) -> None:
        error = AppError("Missing key", code=ErrorCode.MISSING_API_KEY)

        self.assertEqual(error.to_response(), {"error": "Missing key", "code": "missing_api_key"})

    def test_app_error_to_response_includes_optional_details(self) -> None:
        error = AppError("Blocked", code=ErrorCode.RECOVERY_PREFLIGHT_CAPACITY, status=409, details={"requiredBytes": 10})

        self.assertEqual(
            error.to_response(),
            {"error": "Blocked", "code": "recovery_preflight_capacity", "details": {"requiredBytes": 10}},
        )

    def test_default_app_error_code_tracks_status_family(self) -> None:
        self.assertEqual(AppError("Bad payload").code, ErrorCode.INVALID_PAYLOAD)
        self.assertEqual(AppError("Broken", status=500).code, ErrorCode.INTERNAL)

    def test_post_unknown_route_returns_not_found_code(self) -> None:
        running_server, thread = self.make_server()
        try:
            status, payload, _ = self.request_json(
                running_server,
                "POST",
                "/api/does-not-exist",
                body=b"{}",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {server_module.settings.auth.token}"},
            )
        finally:
            running_server.shutdown()
            running_server.server_close()
            thread.join(timeout=5)

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "Not found")
        self.assertEqual(payload["code"], ErrorCode.NOT_FOUND.value)

    def test_api_config_requires_auth_and_accepts_bearer(self) -> None:
        running_server, thread = self.make_server()
        try:
            status, payload, _ = self.request_json(running_server, "GET", "/api/config")
            self.assertEqual(status, 401)
            self.assertEqual(payload["code"], ErrorCode.UNAUTHORIZED.value)

            status, payload, _ = self.request_json(
                running_server,
                "GET",
                "/api/config",
                headers={"Authorization": f"Bearer {server_module.settings.auth.token}"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["version"], server_module.APP_VERSION)
        finally:
            running_server.shutdown()
            running_server.server_close()
            thread.join(timeout=5)

    def test_api_config_accepts_cookie_token(self) -> None:
        running_server, thread = self.make_server()
        try:
            status, payload, _ = self.request_json(
                running_server,
                "GET",
                "/api/config",
                headers={"Cookie": f"auth_token={server_module.settings.auth.token}"},
            )
        finally:
            running_server.shutdown()
            running_server.server_close()
            thread.join(timeout=5)

        self.assertEqual(status, 200)
        self.assertEqual(payload["version"], server_module.APP_VERSION)

    def test_api_config_rejects_unknown_host(self) -> None:
        running_server, thread = self.make_server()
        try:
            status, payload, _ = self.request_json(
                running_server,
                "GET",
                "/api/config",
                headers={"Host": "evil.example", "Authorization": f"Bearer {server_module.settings.auth.token}"},
            )
        finally:
            running_server.shutdown()
            running_server.server_close()
            thread.join(timeout=5)

        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], ErrorCode.FORBIDDEN.value)

    def test_root_token_sets_cookie_and_redirects(self) -> None:
        running_server, thread = self.make_server()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", running_server.server_address[1], timeout=5)
            connection.request("GET", f"/?token={server_module.settings.auth.token}")
            response = connection.getresponse()
            response.read()
        finally:
            connection.close()
            running_server.shutdown()
            running_server.server_close()
            thread.join(timeout=5)

        self.assertEqual(response.status, 302)
        self.assertEqual(response.getheader("Location"), "/")
        cookie_header = response.getheader("Set-Cookie") or ""
        self.assertIn("auth_token=", cookie_header)
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("Max-Age=2592000", cookie_header)
        self.assertIn("SameSite=Strict", cookie_header)

    def test_desktop_root_token_sets_cookie_and_serves_index(self) -> None:
        running_server, thread = self.make_server()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", running_server.server_address[1], timeout=5)
            connection.request("GET", f"/?token={server_module.settings.auth.token}&desktop=1")
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()
            running_server.shutdown()
            running_server.server_close()
            thread.join(timeout=5)

        self.assertEqual(response.status, 200)
        self.assertIn("<!doctype html>", body.lower())
        cookie_header = response.getheader("Set-Cookie") or ""
        self.assertIn("auth_token=", cookie_header)
        self.assertIn("HttpOnly", cookie_header)

    def test_auth_cookie_quotes_custom_token_safely(self) -> None:
        cookie = SimpleCookie()
        cookie.load(server_module.auth_cookie_header("a&b c;z"))

        self.assertEqual(cookie["auth_token"].value, "a&b c;z")
        self.assertEqual(cookie["auth_token"]["max-age"], "2592000")

    def test_token_url_is_encoded_for_custom_tokens(self) -> None:
        url = url_with_token("http://127.0.0.1:8000/", "a&b c")

        self.assertEqual(url, "http://127.0.0.1:8000/?token=a%26b+c")

    def test_redact_sensitive_query_hides_token(self) -> None:
        redacted = server_module.redact_sensitive_query('GET /?token=secret&x=1 HTTP/1.1 "http://127.0.0.1:8000/?token=secret"')

        self.assertNotIn("secret", redacted)
        self.assertIn("%5Bredacted%5D", redacted)
        self.assertIn("http://127.0.0.1:8000/?token=%5Bredacted%5D", redacted)

    def test_redact_sensitive_query_handles_path_only_tokens(self) -> None:
        redacted = server_module.redact_sensitive_query("GET /api/chat?token=secret&next=/ HTTP/1.1")

        self.assertNotIn("secret", redacted)
        self.assertIn("/api/chat?token=%5Bredacted%5D&next=%2F", redacted)

    def test_parse_content_length_rejects_invalid_values(self) -> None:
        with self.assertRaises(AppError) as cm:
            server_module.parse_content_length("abc")

        self.assertEqual(cm.exception.code, ErrorCode.INVALID_PAYLOAD)


if __name__ == "__main__":
    unittest.main()


