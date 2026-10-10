#!/usr/bin/env python3
"""Offline action oracle: execute the actual 4.8.0 source, not a reimplementation."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "a37735c68398fc8f795babaa269e2de6a5acd567"
CORPUS = ROOT / "compat/native-runtime/v30/state/action_lifecycle_vector.json"
NOW = datetime(2026, 9, 9, 0, 0, tzinfo=timezone.utc)


def capture_baseline() -> dict[str, Any]:
    archive = subprocess.check_output(
        ["git", "archive", "--format=zip", SOURCE_COMMIT, "deepseek_infra", "VERSION"], cwd=ROOT, timeout=30,
    )
    with tempfile.TemporaryDirectory(prefix="native-action-oracle-") as directory:
        snapshot = Path(directory).resolve()
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            for entry in source.infolist():
                target = (snapshot / entry.filename).resolve()
                if not target.is_relative_to(snapshot) or stat.S_ISLNK(entry.external_attr >> 16):
                    raise RuntimeError("unsafe baseline archive entry")
            source.extractall(snapshot)
        # No ambient application configuration, credentials or Python import path.
        environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}}
        environment["DEEPSEEK_INFRA_ROOT"] = str(snapshot)
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--child"],
            cwd=snapshot, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60, check=True,
        )
        return json.loads(result.stdout)


def _run_snapshot() -> dict[str, Any]:
    snapshot = Path.cwd().resolve()
    if snapshot == ROOT or os.environ.get("DEEPSEEK_INFRA_ROOT") != str(snapshot):
        raise RuntimeError("oracle requires an isolated snapshot")

    def deny_network(event: str, args: tuple[Any, ...]) -> None:
        if event.startswith("socket.") or event == "subprocess.Popen":
            raise RuntimeError("offline oracle attempted external execution")

    sys.addaudithook(deny_network)
    sys.path.insert(0, str(snapshot))
    from deepseek_infra.core import config
    from deepseek_infra.infra.workspace import autonomous_action_policy as policy
    from deepseek_infra.infra.workspace import resilience_action_journal as journal
    from deepseek_infra.infra.workspace import resilience_resource_locks as locks
    from deepseek_infra.infra.workspace import resilience_slo_ledger as slo

    if config.ROOT.resolve() != snapshot:
        raise RuntimeError("oracle data root escaped snapshot")
    provenance = {}
    for module in (journal, locks, policy, slo):
        if module.__file__ is None:
            raise RuntimeError("baseline module has no source file")
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(snapshot):
            raise RuntimeError("oracle imported code outside baseline")
        # Frozen v30 fingerprints Windows git-archive extraction (CRLF) of the
        # same blobs. Hash the CRLF form so Linux runners match the fixture.
        payload = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        provenance[path.relative_to(snapshot).as_posix()] = hashlib.sha256(payload).hexdigest()

    cases = []

    def setup(case_id: str) -> dict[str, Any]:
        state = snapshot / "oracle-state" / case_id
        journal.JOURNAL_DIR, journal.JOURNAL_DB = state / "journal", state / "journal/journal.sqlite3"
        slo.SLO_LEDGER_DIR, slo.SLO_LEDGER_DB = state / "slo", state / "slo/slo.sqlite3"
        policy.POLICY_DIR, policy.POLICY_FILE = state / "policy", state / "policy/autonomous_policy.json"
        intent = {"actionId": "oracle-action", "type": "CREATE_REPAIR_JOB", "parameters": {"destTargetId": "target-a"}}
        journal.record_action_intent(intent, now=NOW)
        case: dict[str, Any] = {"id": case_id, "action": intent, "steps": [], "observations": []}
        cases.append(case)
        return case

    def observe(case: dict[str, Any], **outcome: Any) -> None:
        action = journal.get_action("oracle-action")
        assert action is not None
        with journal._connect() as conn:
            resources = locks.list_active_locks(conn)
        case["observations"].append({
            "state": action["state"], "epoch": action["executionEpoch"],
            "owner": action["ownerInstanceId"], "lease_until": action["leaseUntil"],
            "effect_class": action["effectClass"],
            "locks": [{"key": row["lockKey"], "owner": row["ownerInstanceId"], "lease_until": row["leaseUntil"]}
                      for row in sorted(resources, key=lambda row: row["lockKey"])],
            "events": [{"type": row["eventType"], "state": row["state"], "epoch": row["executionEpoch"]}
                       for row in journal.list_action_events("oracle-action")],
            **outcome,
        })

    def claim(case: dict[str, Any]) -> dict[str, Any]:
        case["steps"].append({"op": "admit", "owner": "original", "lease_seconds": 10, "at_seconds": 0})
        admitted, action, reason = journal.admit_and_claim_action("oracle-action", owner_instance_id="original", lease_seconds=10, now=NOW)
        observe(case, admitted=admitted, reason=reason)
        assert admitted and action is not None
        return action

    case = setup("fresh-claim")
    observe(case)
    claim(case)
    for state in ("CLAIMED", "EXECUTING", "RECONCILING", "VERIFYING", "ASSESSING_EFFECT"):
        case = setup(f"takeover-{state}")
        original = claim(case)
        case["steps"].append({"op": "update-state", "state": state, "epoch": 1, "token": "original", "at_seconds": 0})
        journal.update_action_state("oracle-action", state, execution_epoch=1, claim_token=original["claimToken"], now=NOW)
        observe(case)
        case["steps"].append({"op": "admit", "owner": "successor", "lease_seconds": 10, "at_seconds": 11})
        admitted, action, reason = journal.admit_and_claim_action("oracle-action", owner_instance_id="successor", lease_seconds=10, now=NOW + timedelta(seconds=11))
        assert action is not None
        observe(case, admitted=admitted, reason=reason, token_replaced=action["claimToken"] != original["claimToken"])
    case = setup("takeover-at-deadline")
    claim(case)
    case["steps"].append({"op": "admit", "owner": "successor", "lease_seconds": 60, "at_seconds": 10})
    admitted, _, reason = journal.admit_and_claim_action("oracle-action", owner_instance_id="successor", now=NOW + timedelta(seconds=10))
    observe(case, admitted=admitted, reason=reason)
    case = setup("renew-expired-current-token")
    original = claim(case)
    case["steps"].append({"op": "renew", "epoch": 1, "token": "original", "lease_seconds": 10, "at_seconds": 11})
    renewed = journal.renew_action_lease("oracle-action", 1, original["claimToken"], lease_seconds=10, now=NOW + timedelta(seconds=11))
    observe(case, renewed=renewed)
    case = setup("unknown-is-terminal")
    original = claim(case)
    case["steps"].extend([
        {"op": "update-state", "state": "EFFECT_UNKNOWN", "epoch": 1, "token": "original", "at_seconds": 0},
        {"op": "admit", "owner": "resilience-worker", "lease_seconds": 60, "at_seconds": 11},
        {"op": "renew", "epoch": 1, "token": "original", "lease_seconds": 120, "at_seconds": 11},
    ])
    journal.update_action_state("oracle-action", "EFFECT_UNKNOWN", execution_epoch=1, claim_token=original["claimToken"], now=NOW)
    admitted, _, reason = journal.admit_and_claim_action("oracle-action", now=NOW + timedelta(seconds=11))
    renewed = journal.renew_action_lease("oracle-action", 1, original["claimToken"], now=NOW + timedelta(seconds=11))
    observe(case, admitted=admitted, renewed=renewed, reason=reason)
    for case_id in ("repeated-takeover", "stale-token-after-takeover"):
        case = setup(case_id)
        original = claim(case)
        previous = original
        for offset in ((11, 22) if case_id == "repeated-takeover" else (11,)):
            case["steps"].append({"op": "admit", "owner": "successor", "lease_seconds": 10, "at_seconds": offset})
            admitted, action, reason = journal.admit_and_claim_action(
                "oracle-action", owner_instance_id="successor", lease_seconds=10, now=NOW + timedelta(seconds=offset),
            )
            assert admitted and action is not None
            observe(case, admitted=admitted, reason=reason, token_replaced=action["claimToken"] != previous["claimToken"])
            previous = action
        if case_id == "stale-token-after-takeover":
            case["steps"].append({"op": "renew", "epoch": 1, "token": "original", "lease_seconds": 120, "at_seconds": 12})
            renewed = journal.renew_action_lease("oracle-action", 1, original["claimToken"], now=NOW + timedelta(seconds=12))
            observe(case, renewed=renewed)
    imported_paths = []
    for name, module in sys.modules.items():
        filename = getattr(module, "__file__", None)
        if name.startswith("deepseek_infra") and isinstance(filename, str):
            imported_paths.append(Path(filename).resolve())
    outside_baseline = any(not path.is_relative_to(snapshot) for path in imported_paths)
    if outside_baseline:
        raise RuntimeError("runtime import escaped baseline snapshot")
    return {
        "schema_version": 1, "source_version": "4.8.0", "source_commit": SOURCE_COMMIT,
        "execution": "isolated-source-snapshot-real-sqlite", "current_worktree_imported": outside_baseline,
        "source_sha256": provenance, "now": NOW.isoformat(), "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Generate the additive v30 observation fixture")
    mode.add_argument("--check", action="store_true", help="Execute the baseline and compare the committed fixture")
    args = parser.parse_args(argv)
    if args.child:
        print(json.dumps(_run_snapshot()))
        return 0
    result = capture_baseline()
    if args.write:
        encoded = json.dumps(result, indent=2) + "\n"
        if CORPUS.exists():
            if CORPUS.read_text(encoding="utf-8") != encoded:
                raise SystemExit("will not replace an existing fixture; add a reviewed corpus version")
        else:
            CORPUS.parent.mkdir(parents=True, exist_ok=True)
            with CORPUS.open("x", encoding="utf-8", newline="\n") as output:
                output.write(encoded)
    elif args.check:
        if json.loads(CORPUS.read_text(encoding="utf-8")) != result:
            raise SystemExit("action lifecycle fixture differs from actual baseline execution")
        print(json.dumps({"baseline_reproduced": True, "cases": len(result["cases"]), "native_parity_proven": False}))
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
