from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class Handler(BaseHTTPRequestHandler):
    server_version = "DeepSeekHybridStub/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/healthz":
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/chat/completions":
            self._send(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            request = json.loads(self.rfile.read(length))
        except (TypeError, ValueError, json.JSONDecodeError):
            self._send(400, {"error": {"message": "invalid request"}})
            return
        if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
            self._send(400, {"error": {"message": "messages required"}})
            return
        self._send(
            200,
            {
                "id": "chatcmpl-hybrid-upstream",
                "model": request.get("model"),
                "choices": [{"message": {"content": "hybrid upstream stub", "reasoning_content": ""}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6},
            },
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline DeepSeek-compatible upstream for hybrid E2E only.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9080)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
