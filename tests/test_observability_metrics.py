from __future__ import annotations

import http.client
import json
import threading
import unittest

import deepseek_infra.web.server as server_module
from deepseek_infra.infra.observability.health import healthz, readyz
from deepseek_infra.infra.observability.metrics import render_prometheus
from deepseek_infra.web.server import FastAPIServer


class HealthMetricsUnitTests(unittest.TestCase):
    def test_healthz_reports_runtime_identity(self) -> None:
        data = healthz()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["runtime"], "local")
        self.assertEqual(data["provider"], "deepseek")
        self.assertIn("version", data)
        self.assertIn("auth_enabled", data)

    def test_readyz_reports_checks(self) -> None:
        data = readyz()
        self.assertIn(data["status"], {"ready", "degraded"})
        self.assertIn("tracing", data["checks"])
        self.assertIn("model_provider", data["checks"])

    def test_prometheus_text_has_named_metrics(self) -> None:
        text = render_prometheus()
        for name in [
            "ai_requests_total",
            "ai_agent_runs_total",
            "ai_model_calls_total",
            "ai_semantic_cache_hits_total",
            "ai_tokens_total",
            "ai_run_latency_ms_avg",
            "ai_trace_enabled",
        ]:
            self.assertIn(f"# TYPE {name} ", text)
            self.assertRegex(text, rf"(?m)^{name} ")


class HealthMetricsRouteTests(unittest.TestCase):
    def make_server(self) -> tuple[FastAPIServer, threading.Thread]:
        server, _ = server_module.create_server(0, host="127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def get(self, server: FastAPIServer, path: str) -> tuple[int, str, str]:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.getheader("content-type") or "", response.read().decode("utf-8")
        finally:
            connection.close()

    def test_probes_and_metrics_are_unauthenticated(self) -> None:
        server, thread = self.make_server()
        try:
            status, _, body = self.get(server, "/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["status"], "ok")

            status, _, body = self.get(server, "/readyz")
            self.assertEqual(status, 200)
            self.assertIn("checks", json.loads(body))

            status, content_type, body = self.get(server, "/metrics")
            self.assertEqual(status, 200)
            self.assertIn("text/plain", content_type)
            self.assertIn("ai_requests_total", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
