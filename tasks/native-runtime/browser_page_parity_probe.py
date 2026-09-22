"""browser page-content parity probe, Python side.

Pins what the static controller extracts from a fixture: `title`, `text` and the
`links` list, for every browser fixture. The controller probes pin *which*
controller answered; this one pins *what it read*.

Run against `rust/crates/deepseek-policy/examples/browser_page_parity_probe.rs`;
the two outputs must be byte-identical.

Usage::

    python tasks/native-runtime/browser_page_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example browser_page_parity_probe > ../rust.json

The precondition is the same as the controller probe: the native runtime has no
Playwright engine, so the `playwright` module is made unimportable and the static
fallback is the controller under measurement.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# The documented environment of the static fallback; see the controller probe.
sys.modules["playwright"] = cast(Any, None)

from deepseek_infra.core import config  # noqa: E402
from deepseek_infra.infra.browser import actions, session  # noqa: E402

FIXTURES = [
    "basic.html",
    "download.html",
    "form.html",
    "injection.html",
    "sample-report.html",
]


def main() -> int:
    config.BROWSER_CONTROL_ENABLED = True
    config.BROWSER_REQUIRE_CONFIRM = True
    config.BROWSER_ALLOW_PRIVATE_HOSTS = False
    config.BROWSER_HEADLESS = True
    session.reset_sessions_for_tests()

    out: dict[str, Any] = {}
    for name in FIXTURES:
        fixture = REPO / "tests" / "fixtures" / "browser" / name
        if not fixture.exists():
            raise SystemExit(f"missing fixture: {fixture}")
        opened = actions.execute_browser_action({"action": "open_url", "url": "file:///" + fixture.as_posix()})
        page = opened["result"]["page"]
        session_id = str(opened["session"]["browserSessionId"])
        links = actions.execute_browser_action({"action": "extract_links", "sessionId": session_id})["result"]["links"]
        out[name] = {
            "title": page.get("title") or "",
            "text": page.get("text") or "",
            "links": [
                {
                    "href": str(link.get("href") or ""),
                    "text": str(link.get("text") or ""),
                    "title": str(link.get("title") or ""),
                }
                for link in links
            ],
        }

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
