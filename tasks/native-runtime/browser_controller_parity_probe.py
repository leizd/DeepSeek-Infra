"""browser controller/engine-state parity probe, Python side.

Pins `session.controller_kind` and the controller a *result* reports across the
states the oracle distinguishes: a session nothing has answered yet, a refused
action, a dispatched action, and a failed one. Also pins the engine-availability
answer that selects the controller.

**The precondition this runs under.** The native runtime has no Playwright
engine, so the deployment it corresponds to is the one where
`playwright_available()` is false and the static fallback is the controller. The
`playwright` module is therefore made unimportable before anything imports it.
That is the documented environment of the static fallback, not a stand-in for
the code under test: the controller-selection state machine runs untouched.

The host's real answer is reported on **stderr** and stays outside the compared
bytes on purpose — the two sides are *supposed* to disagree there, and that
disagreement is exactly the engine gap this probe does not cover.

Usage::

    python tasks/native-runtime/browser_controller_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example browser_controller_parity_probe > ../rust.json
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, cast

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HOST_PLAYWRIGHT_IMPORTABLE = importlib.util.find_spec("playwright") is not None

# The precondition: no browser engine is importable, so `playwright_available()`
# answers false and `_create_controller` takes the static fallback. A `None` entry
# makes `import playwright.sync_api` raise, which is what the oracle probes for.
sys.modules["playwright"] = cast(Any, None)

from deepseek_infra.core import config  # noqa: E402
from deepseek_infra.infra.browser import actions, session  # noqa: E402
from deepseek_infra.infra.browser.controller import playwright_available  # noqa: E402

PRIVATE_URL = "http://127.0.0.1:8000/private"


def main() -> int:
    config.BROWSER_CONTROL_ENABLED = True
    config.BROWSER_REQUIRE_CONFIRM = True
    config.BROWSER_ALLOW_PRIVATE_HOSTS = False
    config.BROWSER_HEADLESS = True
    session.reset_sessions_for_tests()

    fixture = REPO / "tests" / "fixtures" / "browser" / "basic.html"
    if not fixture.exists():
        raise SystemExit(f"missing fixture: {fixture}")
    uri = "file:///" + fixture.as_posix()

    # 1. A session exists but nothing has answered: no controller is recorded yet.
    fresh = actions.execute_browser_action({"action": ""})

    # 2. Refused on the URL, which happens *before* a controller is built.
    blocked = actions.execute_browser_action({"action": "open_url", "url": PRIVATE_URL})

    # 3. Dispatched: a controller answered, so the session records its kind.
    opened = actions.execute_browser_action({"action": "open_url", "url": uri})
    session_id = str(opened["session"]["browserSessionId"])

    # 4. Failed. `download` with no target raises from the controller.
    try:
        actions.execute_browser_action({"action": "download", "sessionId": session_id, "confirmed": True})
        raised = False
    except Exception:
        raised = True

    # A refused action reads the stored session back without touching it, so it is
    # how the failed state is observable through the public API.
    observed = actions.execute_browser_action({"action": "open_url", "sessionId": session_id, "url": PRIVATE_URL})

    # 5. A later success reports the live controller, but the recorded kind is
    #    sticky — `controller_for` hands back the cached controller without
    #    touching the session again. `extract_links` is used because `read_page`
    #    also writes a snapshot into the Python-owned media store.
    read = actions.execute_browser_action({"action": "extract_links", "sessionId": session_id})

    def text(payload: dict[str, Any], key: str) -> str:
        return str(payload.get(key) or "")

    def prefixed(value: Any) -> bool:
        return str(value or "").startswith("failed:")

    out: dict[str, Any] = {
        "blocked-code": text(blocked, "code"),
        "blocked-controller": text(blocked["session"], "controller"),
        "blocked-risk": text(blocked["safety"], "risk"),
        "dispatched-result-controller": text(opened["result"], "controller"),
        "dispatched-session-controller": text(opened["session"], "controller"),
        "dispatched-session-status": text(opened["session"], "status"),
        "engine": text(opened["session"], "engine"),
        "engine-available": bool(playwright_available()),
        "failed-action-raised": raised,
        "failed-controller-is-prefixed": prefixed(observed["session"]["controller"]),
        "failed-status": text(observed["session"], "status"),
        "fresh-controller": text(fresh["session"], "controller"),
        "fresh-status": text(fresh["session"], "status"),
        "live-controller-after-failure": text(read["result"], "controller"),
        "sticky-controller-is-prefixed": prefixed(read["session"]["controller"]),
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")

    # Diagnostics, deliberately outside the compared bytes.
    print(f"# compared under: playwright importable = {HOST_PLAYWRIGHT_IMPORTABLE} (hidden for the run)", file=sys.stderr)
    print(f"# host truth: engine-available would be {HOST_PLAYWRIGHT_IMPORTABLE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
