#!/usr/bin/env python3
"""DeepSeek Infra Doctor：一键体检，回答「到底哪里没配好」。

完全离线、不发任何网络请求；依赖缺失时也能跑（核心检查只用 stdlib）。

    python scripts/doctor.py
    python scripts/doctor.py --json

退出码：有 [FAIL] 时为 1，否则 0（[WARN] 不影响退出码——它们是可选能力）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

# (import 名, pip 包名) —— 缺一个就没法 python app.py
CORE_DEPENDENCIES = (
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn[standard]"),
    ("multipart", "multipart"),
    ("pypdf", "pypdf"),
    ("fitz", "PyMuPDF"),
    ("pptx", "python-pptx"),
    ("docx", "python-docx"),
    ("reportlab", "reportlab"),
    ("openpyxl", "openpyxl"),
    ("defusedxml", "defusedxml"),
)

# (import 名, pip 来源, 缺失时降级成什么)
OPTIONAL_DEPENDENCIES = (
    ("sqlite_vec", "requirements-rag.txt", "RAG 向量表回退 SQLite + 哈希 embedding"),
    ("onnxruntime", "requirements-rag.txt", "本地 ONNX embedding 不可用，RAG 用哈希 embedding"),
    ("llama_cpp", "requirements-edge.txt", "端侧 GGUF 推理不可用"),
    ("openai", "pip install openai", "examples/openai_compatible_client.py 走 stdlib 回退"),
)


@dataclass
class CheckResult:
    status: str
    name: str
    detail: str = ""

    def line(self) -> str:
        suffix = f" — {self.detail}" if self.detail else ""
        return f"[{self.status}] {self.name}{suffix}"


def check_python() -> CheckResult:
    version = sys.version_info
    text = f"Python {version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) >= (3, 10):
        return CheckResult(OK, f"{text} >= 3.10")
    return CheckResult(FAIL, f"{text} < 3.10", "请升级 Python（pyproject requires-python >= 3.10）")


def check_core_dependencies() -> list[CheckResult]:
    missing = [pip_name for module, pip_name in CORE_DEPENDENCIES if importlib.util.find_spec(module) is None]
    if not missing:
        return [CheckResult(OK, f"核心依赖齐全（{len(CORE_DEPENDENCIES)} 项）")]
    return [CheckResult(FAIL, "核心依赖缺失", f"pip install -r requirements.txt（缺 {', '.join(missing)}）")]


def check_optional_dependencies() -> list[CheckResult]:
    results: list[CheckResult] = []
    for module, source, fallback in OPTIONAL_DEPENDENCIES:
        if importlib.util.find_spec(module) is None:
            results.append(CheckResult(WARN, f"可选依赖 {module} 未安装", f"{fallback}（来源：{source}）"))
        else:
            results.append(CheckResult(OK, f"可选依赖 {module} 已安装"))
    return results


def load_config():  # noqa: ANN201 - 失败时返回 None，调用方降级
    try:
        from deepseek_infra.core import config

        return config
    except Exception as exc:  # 连 config 都导不进来时其余检查降级，但 doctor 本身不崩
        print(f"[WARN] 无法导入 deepseek_infra.core.config：{exc}", file=sys.stderr)
        return None


def check_static_dir(config) -> CheckResult:  # noqa: ANN001
    if config is None:
        return CheckResult(WARN, "static 目录未检查", "config 不可导入")
    static_dir = Path(config.STATIC_DIR)
    if static_dir.is_dir() and (static_dir / "index.html").exists():
        return CheckResult(OK, f"static 目录存在（{static_dir}）")
    return CheckResult(FAIL, "static 目录缺失或不完整", str(static_dir))


def check_data_dir_writable(config) -> CheckResult:  # noqa: ANN001
    root = Path(config.ROOT) if config is not None else REPO_ROOT
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".doctor-", suffix=".tmp", delete=True):
            pass
    except OSError as exc:
        return CheckResult(FAIL, f"数据目录不可写（{root}）", str(exc))
    return CheckResult(OK, f"数据目录可写（{root}）")


def check_api_keys(config) -> list[CheckResult]:  # noqa: ANN001
    results: list[CheckResult] = []
    deepseek = os.environ.get("DEEPSEEK_API_KEY", "").strip() or (config.settings.deepseek_api_key if config else "")
    tavily = os.environ.get("TAVILY_API_KEY", "").strip() or (config.settings.tavily_api_key if config else "")
    if deepseek:
        results.append(CheckResult(OK, "DeepSeek API Key 已配置"))
    else:
        results.append(CheckResult(WARN, "DeepSeek API Key 未配置", "可在页面设置里临时填写；服务端能力（A2A 执行等）需要环境变量"))
    if tavily:
        results.append(CheckResult(OK, "Tavily API Key 已配置"))
    else:
        results.append(CheckResult(WARN, "Tavily API Key 未配置", "联网搜索不可用；不影响其它能力"))
    return results


def check_port(config) -> CheckResult:  # noqa: ANN001
    port = int(os.environ.get("PORT", "0") or 0) or (config.settings.default_port if config else 8000)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return CheckResult(WARN, f"端口 {port} 已被占用", "可能服务已在运行；换端口用 PORT=<n>")
    finally:
        probe.close()
    return CheckResult(OK, f"端口 {port} 可用")


def check_security_mode(config) -> CheckResult:  # noqa: ANN001
    if config is None:
        return CheckResult(WARN, "安全模式未检查", "config 不可导入")
    host = config.settings.default_host
    mode = config.settings.security_mode
    auth_enabled = config.settings.auth.enabled
    if host != "127.0.0.1" and mode != "strict":
        return CheckResult(WARN, f"HOST={host} 但 SECURITY_MODE={mode}", "局域网暴露建议 SECURITY_MODE=strict（见 docs/SECURITY.md）")
    if not auth_enabled and host != "127.0.0.1":
        return CheckResult(FAIL, "AUTH_DISABLED=1 且监听非回环地址", "局域网内任何人都能直接使用，请开启鉴权")
    return CheckResult(OK, f"安全配置一致（HOST={host} · SECURITY_MODE={mode} · auth={'on' if auth_enabled else 'off'}）")


def run_checks() -> list[CheckResult]:
    config = load_config()
    results: list[CheckResult] = [check_python()]
    results.extend(check_core_dependencies())
    results.append(check_static_dir(config))
    results.append(check_data_dir_writable(config))
    results.extend(check_api_keys(config))
    results.extend(check_optional_dependencies())
    results.append(check_port(config))
    results.append(check_security_mode(config))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek Infra environment doctor")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args()

    results = run_checks()
    failed = any(result.status == FAIL for result in results)
    if args.json:
        print(json.dumps({"ok": not failed, "checks": [asdict(result) for result in results]}, ensure_ascii=False, indent=2))
    else:
        print("DeepSeek Infra Doctor")
        for result in results:
            print(result.line())
        print("\n结论：" + ("环境就绪，python app.py 直接起。" if not failed else "存在 [FAIL] 项，按提示修复后重试。"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
