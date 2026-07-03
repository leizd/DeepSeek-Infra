"""Runtime Doctor — pre-flight environment & runtime readiness checks.

The doctor answers a single question: *why won't it start?* It walks the
environment a fresh DeepSeek Infra install needs (Python version, installed
deps, ``.env``, API key, writable data root, static assets, port availability,
local auth token, live health probes) and emits one PASS / WARNING / FAIL per
check.

Offline mode (``--offline``) never touches the network and never requires a
DeepSeek API key, so it is safe to run in CI. Live mode additionally probes
``/healthz`` / ``/readyz`` / ``/metrics`` against a running server.

The check functions are pure and side-effect-light (they may create the named
data dirs to confirm writability) so they can be unit-tested with a tmp path.
"""

from __future__ import annotations

import importlib
import json
import os
import secrets
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

LABEL = {STATUS_PASS: "PASS", STATUS_WARN: "WARNING", STATUS_FAIL: "FAIL"}

REQUIRED_IMPORTS: tuple[tuple[str, str], ...] = (
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("multipart", "multipart"),
    ("defusedxml", "defusedxml"),
    ("openpyxl", "openpyxl"),
    ("pypdf", "pypdf"),
    ("PyMuPDF", "fitz"),
    ("python-pptx", "pptx"),
    ("python-docx", "docx"),
    ("reportlab", "reportlab"),
)
OPTIONAL_IMPORTS: tuple[tuple[str, str], ...] = (
    ("customtkinter", "customtkinter"),
    ("pywebview", "webview"),
)
DEFAULT_DATA_DIRS: tuple[str, ...] = (".traces", ".agent-runs", ".a2a", ".local-rag", ".semantic-cache")
MIN_PYTHON: tuple[int, int] = (3, 10)


@dataclass(slots=True)
class CheckResult:
    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return LABEL.get(self.status, self.status.upper())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DoctorOptions:
    root: Path
    static_dir: Path
    offline: bool = True
    base_url: str = "http://127.0.0.1:8000"
    token: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    data_dirs: tuple[str, ...] = DEFAULT_DATA_DIRS
    min_python: tuple[int, int] = MIN_PYTHON
    required_imports: tuple[tuple[str, str], ...] = REQUIRED_IMPORTS
    optional_imports: tuple[tuple[str, str], ...] = OPTIONAL_IMPORTS
    health_paths: tuple[str, ...] = ("/healthz", "/readyz", "/metrics")
    probe_timeout: float = 3.0


def mask_token(token: str) -> str:
    """Return a non-reversible hint of a token for logs/output."""
    if not token:
        return ""
    if len(token) <= 8:
        return "***"
    return token[:4] + "…" + token[-4:]


def check_python_version(min_version: tuple[int, int] = MIN_PYTHON) -> CheckResult:
    actual = sys.version_info[:2]
    if actual >= min_version:
        return CheckResult("python", STATUS_PASS, f"Python {actual[0]}.{actual[1]} >= {min_version[0]}.{min_version[1]}", {"actual": list(actual), "min": list(min_version)})
    return CheckResult("python", STATUS_FAIL, f"Python {actual[0]}.{actual[1]} is older than required {min_version[0]}.{min_version[1]}", {"actual": list(actual), "min": list(min_version)})


def _try_import(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


def check_requirements(specs: tuple[tuple[str, str], ...]) -> CheckResult:
    missing: list[str] = []
    for display, import_name in specs:
        if not _try_import(import_name):
            missing.append(display)
    if missing:
        return CheckResult("requirements", STATUS_FAIL, f"missing required packages: {', '.join(missing)}", {"missing": missing})
    return CheckResult("requirements", STATUS_PASS, f"{len(specs)} required packages importable", {"count": len(specs)})


def check_optional_requirements(specs: tuple[tuple[str, str], ...]) -> CheckResult:
    missing: list[str] = []
    for display, import_name in specs:
        if not _try_import(import_name):
            missing.append(display)
    if missing:
        return CheckResult("optional_requirements", STATUS_WARN, f"optional GUI packages not importable: {', '.join(missing)}", {"missing": missing})
    return CheckResult("optional_requirements", STATUS_PASS, f"{len(specs)} optional packages importable", {"count": len(specs)})


def check_edge_router() -> CheckResult:
    from deepseek_infra.infra.gateway.edge_inference import edge_inference_status

    status = edge_inference_status()
    provider = str(status.get("provider") or "llama_cpp")
    suggestions = [str(item) for item in status.get("suggestions") or [] if str(item).strip()]
    if not status.get("enabled"):
        return CheckResult(
            "edge_router",
            STATUS_PASS,
            "Edge Router disabled (optional); set EDGE_INFERENCE_ENABLED=1 to enable local routing",
            status,
        )
    if status.get("available"):
        quantization = str(status.get("quantization") or "unknown")
        return CheckResult(
            "edge_router",
            STATUS_PASS,
            f"Edge Router available via {provider} (quantization={quantization})",
            status,
        )
    detail = "; ".join(suggestions) if suggestions else "Edge Router enabled but unavailable; inspect edgeInference status"
    return CheckResult("edge_router", STATUS_WARN, detail, status)


def check_env_file(root: Path) -> CheckResult:
    env_path = root / ".env"
    if env_path.exists():
        return CheckResult("env_file", STATUS_PASS, ".env present", {"path": str(env_path)})
    example = root / ".env.example"
    if example.exists():
        return CheckResult("env_file", STATUS_WARN, ".env missing; copy .env.example and fill in DEEPSEEK_API_KEY", {"path": str(env_path), "example": str(example)})
    return CheckResult("env_file", STATUS_WARN, ".env missing", {"path": str(env_path)})


def check_api_key() -> CheckResult:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return CheckResult("api_key", STATUS_PASS, "DEEPSEEK_API_KEY is set", {"masked": mask_token(key), "length": len(key)})
    return CheckResult("api_key", STATUS_WARN, "DEEPSEEK_API_KEY is not set; cloud chat / multi-agent / A2A tasks will fail", {})


def check_root_writable(root: Path) -> CheckResult:
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult("root_writable", STATUS_FAIL, f"cannot create data root {root}: {exc}", {"path": str(root)})
    probe = root / f".doctor-{secrets.token_hex(4)}.probe"
    try:
        probe.write_text("doctor", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult("root_writable", STATUS_FAIL, f"data root {root} is not writable: {exc}", {"path": str(root)})
    return CheckResult("root_writable", STATUS_PASS, f"data root {root} is writable", {"path": str(root)})


def check_static_dir(static_dir: Path) -> CheckResult:
    if not static_dir.exists():
        return CheckResult("static_dir", STATUS_FAIL, f"static directory missing: {static_dir}", {"path": str(static_dir)})
    if not static_dir.is_dir():
        return CheckResult("static_dir", STATUS_FAIL, f"static path is not a directory: {static_dir}", {"path": str(static_dir)})
    index = static_dir / "index.html"
    if index.exists():
        return CheckResult("static_dir", STATUS_PASS, "static directory present with index.html", {"path": str(static_dir)})
    return CheckResult("static_dir", STATUS_WARN, "static directory present but index.html missing", {"path": str(static_dir)})


def check_data_dirs(root: Path, names: tuple[str, ...]) -> CheckResult:
    created: list[str] = []
    failures: list[str] = []
    for name in names:
        target = root / name
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / f".doctor-{secrets.token_hex(4)}.probe"
            probe.write_text("doctor", encoding="utf-8")
            probe.unlink()
            created.append(name)
        except OSError as exc:
            failures.append(f"{name} ({exc})")
    if failures:
        return CheckResult("data_dirs", STATUS_FAIL, f"cannot create/write data dirs: {', '.join(failures)}", {"ok": created, "failed": failures})
    return CheckResult("data_dirs", STATUS_PASS, f"{len(created)} data dirs writable", {"dirs": created})


def check_port(host: str, port: int) -> CheckResult:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        return CheckResult("port", STATUS_PASS, f"{host}:{port} is free", {"host": host, "port": port, "free": True})
    except OSError as exc:
        return CheckResult("port", STATUS_WARN, f"{host}:{port} is occupied ({exc.__class__.__name__}); server may fail to bind", {"host": host, "port": port, "free": False})
    finally:
        sock.close()


def check_token_file(root: Path) -> CheckResult:
    token_path = root / ".auth-token"
    if not token_path.exists():
        return CheckResult("auth_token", STATUS_WARN, ".auth-token missing; server will generate one on first start", {"path": str(token_path), "present": False})
    try:
        value = token_path.read_text(encoding="utf-8").splitlines()[0].strip()
    except OSError as exc:
        return CheckResult("auth_token", STATUS_WARN, f"cannot read .auth-token: {exc}", {"path": str(token_path), "present": True})
    if not value:
        return CheckResult("auth_token", STATUS_WARN, ".auth-token is empty", {"path": str(token_path), "present": True})
    return CheckResult("auth_token", STATUS_PASS, f".auth-token present ({mask_token(value)})", {"path": str(token_path), "present": True, "masked": mask_token(value)})


def _probe_url(url: str, timeout: float) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read(64)
            code = response.getcode()
            return code == 200, str(code)
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, str(exc)


def check_health_endpoints(base_url: str, paths: tuple[str, ...], timeout: float) -> list[CheckResult]:
    results: list[CheckResult] = []
    for path in paths:
        url = base_url.rstrip("/") + "/" + path.lstrip("/")
        ok, detail = _probe_url(url, timeout)
        status = STATUS_PASS if ok else STATUS_FAIL
        results.append(CheckResult(f"health:{path}", status, f"{url} -> {detail}", {"url": url, "ok": ok}))
    return results


def run_doctor(options: DoctorOptions) -> list[CheckResult]:
    """Run all configured checks in order and return their results."""
    results: list[CheckResult] = [
        check_python_version(options.min_python),
        check_requirements(options.required_imports),
        check_optional_requirements(options.optional_imports),
        check_edge_router(),
        check_env_file(options.root),
        check_api_key(),
        check_root_writable(options.root),
        check_static_dir(options.static_dir),
        check_data_dirs(options.root, options.data_dirs),
        check_port(options.host, options.port),
        check_token_file(options.root),
    ]
    if not options.offline:
        results.extend(check_health_endpoints(options.base_url, options.health_paths, options.probe_timeout))
    return results


def summarize(results: list[CheckResult]) -> dict[str, Any]:
    counts = {STATUS_PASS: 0, STATUS_WARN: 0, STATUS_FAIL: 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    overall = STATUS_FAIL if counts[STATUS_FAIL] else (STATUS_WARN if counts[STATUS_WARN] else STATUS_PASS)
    return {"overall": overall, "counts": counts, "checks": [r.to_dict() for r in results]}


def render_text(results: list[CheckResult]) -> str:
    lines = [f"[{r.label}] {r.name}: {r.detail}" for r in results]
    summary = summarize(results)
    counts = summary["counts"]
    lines.append("")
    lines.append(f"Doctor summary: {LABEL[summary['overall']]} — {counts[STATUS_PASS]} pass, {counts[STATUS_WARN]} warning, {counts[STATUS_FAIL]} fail")
    return "\n".join(lines)


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(r.status == STATUS_FAIL for r in results) else 0


def dump_json(results: list[CheckResult]) -> str:
    return json.dumps(summarize(results), ensure_ascii=False, indent=2)
