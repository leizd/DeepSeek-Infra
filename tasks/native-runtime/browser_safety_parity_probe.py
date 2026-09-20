"""browser safety parity probe, Python side.

Pins evaluate_action / evaluate_url_safety: disabled-by-default, missing URL,
private hosts, high-risk click, password typing, public https allow.

Usage::

    python tasks/native-runtime/browser_safety_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example browser_safety_parity_probe > ../rust.json
"""

from __future__ import annotations

import json
import sys
from typing import Any

REPO = __import__("pathlib").Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from deepseek_infra.core import config  # noqa: E402
from deepseek_infra.infra.browser import safety  # noqa: E402


def view(payload: dict[str, Any], *, enabled: bool, confirm: bool = True, private: bool = False) -> dict[str, Any]:
    config.BROWSER_CONTROL_ENABLED = enabled
    config.BROWSER_REQUIRE_CONFIRM = confirm
    config.BROWSER_ALLOW_PRIVATE_HOSTS = private
    decision = safety.evaluate_action(payload)
    return decision.to_dict()


def main() -> int:
    out = {
        "disabled": view({"action": "open_url", "url": "https://example.com/"}, enabled=False),
        "missing-url": view({"action": "open_url"}, enabled=True),
        "private-ip": view({"action": "open_url", "url": "http://127.0.0.1:8000/private"}, enabled=True),
        "localhost": view({"action": "open_url", "url": "http://localhost/admin"}, enabled=True),
        "credentials": view({"action": "open_url", "url": "https://user:pass@example.com/"}, enabled=True),
        "public": view({"action": "open_url", "url": "https://example.com/docs"}, enabled=True),
        "click-submit": view(
            {"action": "click", "selector": "button.submit", "reason": "Submit form"},
            enabled=True,
        ),
        "type-password": view({"action": "type_text", "selector": "#password", "text": "secret"}, enabled=True),
        "click-confirmed": view(
            {"action": "click", "selector": "ok", "confirmed": True},
            enabled=True,
        ),
        "unknown": view({"action": "explode"}, enabled=True),
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
