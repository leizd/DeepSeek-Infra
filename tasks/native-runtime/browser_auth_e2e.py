"""Compare native launch-token browser responses with the isolated legacy oracle."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

from skills_mutation_routes_e2e import native
from skills_read_routes_e2e import REPO, TOKEN, digest, source_digest

HEADERS = ["location", "set-cookie", "content-type", "cache-control", "referrer-policy", "content-security-policy", "x-frame-options", "x-content-type-options"]


def body_value(body: bytes, content_type: str) -> Any:
    return json.loads(body) if content_type.startswith("application/json") else body.decode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=REPO / "artifacts/browser-auth-e2e.json")
    args = parser.parse_args()
    gateway = args.gateway.resolve()
    if not gateway.is_file():
        parser.error("the compiled native gateway is required")
    with tempfile.TemporaryDirectory(prefix="deepseek-browser-auth-") as directory:
        root = Path(directory)
        os.environ.update(DEEPSEEK_INFRA_ROOT=root.as_posix(), DEEPSEEK_INFRA_STATIC_DIR=root.as_posix(), AUTH_TOKEN=TOKEN, DEEPSEEK_RUNTIME_MODE="python")
        os.environ.pop("AUTH_DISABLED", None)
        sys.path.insert(0, str(REPO))
        from fastapi import FastAPI, Request
        from fastapi.responses import FileResponse
        from fastapi.testclient import TestClient
        from deepseek_infra.web import http_utils, server

        index = root / "ui/index.html"
        index.parent.mkdir()
        index.write_text("<main>native auth fixture</main>", encoding="utf-8")
        server.frontend_index_path = lambda: index
        app = FastAPI()

        @app.api_route("/{path:path}", methods=["GET", "HEAD"])
        async def static_or_auth(request: Request, path: str) -> Any:
            response = server.handle_auth_token_redirect(request)
            if response is None:
                response = FileResponse(index, media_type="text/html")
            http_utils.apply_common_headers(response, request.url.path)
            return response

        paths = [f"{base}?token={TOKEN}{extra}" for base in ["/", "/ui", "/ui/"]
                 for extra in ["", "&desktop=1", "&desktop=false", "&desktop=%20TRUE%20", "&share=discarded"]]
        paths += ["/?token=wrong", "/?token=", f"/?token=wrong&token={TOKEN}", f"/?token={TOKEN}&token=wrong",
                  f"/?%74oken={TOKEN}", f"/ui/?token={TOKEN.replace('-', '%2D')}"]
        results = []
        with native(gateway, root, "python") as port, TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as client:
            for method in ["GET", "HEAD"]:
                for path in paths:
                    expected = client.request(method, path)
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    try:
                        connection.request(method, path)
                        response = connection.getresponse()
                        raw = response.read()
                        expected_headers = {name: expected.headers.get(name, "") for name in HEADERS}
                        actual_headers = {name: response.getheader(name, "") for name in HEADERS}
                        oracle = {"status": expected.status_code, "headers": expected_headers,
                                  "body": body_value(expected.content, expected_headers["content-type"]) if method == "GET" else ""}
                        actual = {"status": response.status, "headers": actual_headers,
                                  "body": body_value(raw, actual_headers["content-type"]) if method == "GET" else ""}
                        results.append({"method": method, "path": path.replace(TOKEN, "<test-token>"), "ok": oracle == actual,
                                        "oracleHash": digest(oracle), "nativeHash": digest(actual),
                                        "difference": {} if oracle == actual else {"oracle": oracle, "native": actual}})
                    finally:
                        connection.close()
        logs = (root / "native.log").read_text(encoding="utf-8", errors="replace")
        logs_hide_token = TOKEN not in logs
    failures = [row for row in results if not row["ok"]]
    report = {"status": "PASS" if not failures and logs_hide_token else "FAIL", "releaseQualified": False,
              "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(), "sourceSha256": source_digest(),
              "gatewaySha256": hashlib.sha256(gateway.read_bytes()).hexdigest(), "probeSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "caseCount": len(results), "differences": failures, "logsHideToken": logs_hide_token, "cases": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "caseCount": len(results), "differenceCount": len(failures), "logsHideToken": logs_hide_token}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
