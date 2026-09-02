#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_FIXTURE = ROOT / "compat" / "native-runtime" / "v1" / "control" / "shadow_cases.json"

SEVERITY_BASE_WEIGHT = {
    "critical": 10.0,
    "degraded": 5.0,
    "warning": 2.0,
    "healthy": 0.0,
    "blocked": 15.0,
}
POLICY_CRITICALITY_WEIGHT = {
    "critical": 3.0,
    "high": 2.0,
    "standard": 1.0,
}
PEER_TRANSITIONS = {
    ("PENDING", "VERIFIED"): "ALLOW",
    ("VERIFIED", "ACTIVE"): "ALLOW",
    ("ACTIVE", "SUSPENDED"): "ALLOW",
    ("PENDING", "REVOKED"): "ALLOW",
    ("VERIFIED", "REVOKED"): "ALLOW",
    ("ACTIVE", "REVOKED"): "ALLOW",
    ("SUSPENDED", "REVOKED"): "ALLOW",
}
METADATA_FIELDS = ("provider", "region", "jurisdiction", "siteClass")


class ShadowError(RuntimeError):
    pass


def canonical_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def decision_digest(decision: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_dumps(decision).encode("utf-8")).hexdigest()


def _risk_debt(action: dict[str, Any], now_unix: int) -> float:
    sev = str(action.get("severity") or "warning").lower()
    base = SEVERITY_BASE_WEIGHT.get(sev, 2.0)
    created = int(action.get("createdAtUnix") or now_unix)
    age_seconds = max(0, now_unix - created)
    age_days = age_seconds / 86400.0
    age_factor = 1.0 + min(10.0, age_days * 0.5)
    crit = str(action.get("policyCriticality") or "standard").lower()
    crit_multiplier = POLICY_CRITICALITY_WEIGHT.get(crit, 1.0)
    slo_factor = 1.5 if action.get("sloBreached") is True else 1.0
    return round(base * age_factor * crit_multiplier * slo_factor, 3)


def _maintenance_allowed(action: dict[str, Any], now_minute: int) -> tuple[bool, str]:
    window = action.get("maintenanceWindow")
    action_type = str(action.get("type") or "").upper()
    severity = str(action.get("severity") or "warning").lower()
    if action_type == "CREATE_REPAIR_JOB" and severity in {"critical", "blocked"}:
        return True, "CRITICAL_DURABILITY_OVERRIDE"
    if action_type == "START_DR_DRILL" and severity in {"critical", "blocked"}:
        return True, "CRITICAL_DR_STALENESS_OVERRIDE"
    if not isinstance(window, dict):
        return True, "NO_MAINTENANCE_WINDOW"
    try:
        start_h, start_m = (int(part) for part in str(window.get("start") or "").split(":", 1))
        end_h, end_m = (int(part) for part in str(window.get("end") or "").split(":", 1))
    except ValueError:
        return False, "INVALID_MAINTENANCE_WINDOW"
    start_value = start_h * 60 + start_m
    end_value = end_h * 60 + end_m
    if start_value == end_value:
        inside = True
    elif start_value < end_value:
        inside = start_value <= now_minute < end_value
    else:
        inside = now_minute >= start_value or now_minute < end_value
    return inside, "WITHIN_MAINTENANCE_WINDOW" if inside else "OUTSIDE_MAINTENANCE_WINDOW"


def evaluate_scheduler(snapshot: dict[str, Any]) -> dict[str, Any]:
    now_unix = int(snapshot.get("nowUnix") or 0)
    now_minute = int(snapshot.get("nowMinute") or 0)
    live_raw = snapshot.get("liveEpochs")
    live_epochs: dict[str, Any] = live_raw if isinstance(live_raw, dict) else {}
    actions = list(snapshot.get("actions") or [])
    scored: list[tuple[float, int, dict[str, Any]]] = []
    admissions: list[dict[str, str]] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        action_id = str(action.get("actionId") or "")
        epoch = int(action.get("executionEpoch") or 0)
        if action_id == "":
            admissions.append({"actionId": "", "decision": "REJECT", "reason": "EMPTY_ACTION_ID"})
            continue
        if epoch == 0:
            admissions.append({"actionId": action_id, "decision": "REJECT", "reason": "ZERO_EXECUTION_EPOCH"})
            continue
        live = int(live_epochs.get(action_id) or 0)
        if epoch < live:
            admissions.append({"actionId": action_id, "decision": "REJECT", "reason": "STALE_EXECUTION_EPOCH"})
            continue
        allowed, reason = _maintenance_allowed(action, now_minute)
        if not allowed:
            admissions.append({"actionId": action_id, "decision": "REJECT", "reason": reason})
            continue
        admissions.append({"actionId": action_id, "decision": "ADMIT", "reason": reason})
        score = _risk_debt(action, now_unix)
        kind = str(action.get("type") or "").upper()
        if kind == "CREATE_REPAIR_JOB":
            score += 1.0
        elif kind == "START_DR_DRILL":
            score += 0.5
        scored.append((score, index, action))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return {
        "orderedActionIds": [str(item[2].get("actionId") or "") for item in scored],
        "admissions": admissions,
    }


def evaluate_risk(snapshot: dict[str, Any]) -> dict[str, Any]:
    risks: list[dict[str, str]] = []
    rank = {"healthy": 1, "warning": 2, "degraded": 3, "critical": 4, "blocked": 5}
    overall = "healthy"
    for item in snapshot.get("capacityTargets") or []:
        if not isinstance(item, dict):
            continue
        target = str(item.get("targetId") or "")
        free_pct = item.get("freePercent")
        days = item.get("estimatedDaysToFull")
        if not isinstance(free_pct, (int, float)):
            severity = "healthy"
            evidence = "unconstrained-quota"
        elif float(free_pct) < 5.0 or (isinstance(days, int) and days < 7):
            severity = "critical"
            evidence = "free-space-critical"
        elif float(free_pct) < 10.0 or (isinstance(days, int) and days < 30):
            severity = "degraded"
            evidence = "free-space-degraded"
        elif float(free_pct) <= 20.0:
            severity = "warning"
            evidence = "free-space-warning"
        else:
            severity = "healthy"
            evidence = "free-space-healthy"
        risks.append({"type": "CAPACITY_EXHAUSTION", "target": target, "severity": severity, "evidence": evidence})
        if rank[severity] > rank[overall]:
            overall = severity
    risks.sort(key=lambda item: item["target"])
    return {"overallRisk": overall, "risks": risks}


def evaluate_wave(snapshot: dict[str, Any]) -> dict[str, Any]:
    existing = str(snapshot.get("existingScheduleDigest") or "")
    incoming = str(snapshot.get("incomingScheduleDigest") or "")
    schedule_id = str(snapshot.get("scheduleId") or "")
    if existing and incoming and existing != incoming:
        return {
            "scheduleId": schedule_id,
            "decision": "SCHEDULE_IDENTITY_CONFLICT",
            "admitWaveIndex": -1,
        }
    planned = str(snapshot.get("plannedRiskDigest") or "")
    fresh = str(snapshot.get("freshRiskDigest") or "")
    if planned and fresh and planned != fresh:
        return {"scheduleId": schedule_id, "decision": "PAUSED_REPLAN", "admitWaveIndex": -1}
    wave_index = int(snapshot.get("admitWaveIndex") or 0)
    waves_raw = snapshot.get("waves")
    actions_raw = snapshot.get("waveActions")
    waves: list[Any] = waves_raw if isinstance(waves_raw, list) else []
    actions: list[Any] = actions_raw if isinstance(actions_raw, list) else []
    if wave_index > 0:
        for wave in waves:
            if isinstance(wave, dict) and int(wave.get("index") or 0) < wave_index:
                if str(wave.get("status") or "") != "COMPLETED":
                    return {"scheduleId": schedule_id, "decision": "WAIT_PREDECESSOR", "admitWaveIndex": wave_index}
        for action in actions:
            if isinstance(action, dict) and int(action.get("waveIndex") or 0) < wave_index:
                if str(action.get("status") or "") != "VERIFIED_SUCCESS":
                    return {"scheduleId": schedule_id, "decision": "WAIT_PREDECESSOR", "admitWaveIndex": wave_index}
    return {"scheduleId": schedule_id, "decision": "ADMIT", "admitWaveIndex": wave_index}


def evaluate_federation(snapshot: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, str]] = []
    local_fleet = str(snapshot.get("localFleetId") or "")
    for item in snapshot.get("federationTransitions") or []:
        if not isinstance(item, dict):
            continue
        peer = str(item.get("peerFleetId") or "")
        current = str(item.get("from") or "")
        nxt = str(item.get("to") or "")
        if peer == "" or peer == local_fleet:
            results.append({"peerFleetId": peer, "from": current, "to": nxt, "decision": "REJECT", "code": "FEDERATION_PEER_SAME_FLEET"})
            continue
        metadata_raw = item.get("metadata")
        metadata: dict[str, Any] = metadata_raw if isinstance(metadata_raw, dict) else {}
        if any(not str(metadata.get(field) or "") for field in METADATA_FIELDS):
            results.append({"peerFleetId": peer, "from": current, "to": nxt, "decision": "REJECT", "code": "FEDERATION_PEER_METADATA_INVALID"})
            continue
        if current == "REVOKED" and nxt != "REVOKED":
            results.append({"peerFleetId": peer, "from": current, "to": nxt, "decision": "REJECT", "code": "FEDERATION_PEER_REVOKED"})
            continue
        if current == nxt:
            results.append({"peerFleetId": peer, "from": current, "to": nxt, "decision": "ALLOW", "code": "IDEMPOTENT"})
            continue
        if nxt == "ACTIVE" and current == "PENDING":
            results.append({"peerFleetId": peer, "from": current, "to": nxt, "decision": "REJECT", "code": "FEDERATION_PEER_NOT_VERIFIED"})
            continue
        allowed = PEER_TRANSITIONS.get((current, nxt))
        if allowed != "ALLOW":
            results.append({"peerFleetId": peer, "from": current, "to": nxt, "decision": "REJECT", "code": "FEDERATION_PEER_STATE_TRANSITION_INVALID"})
            continue
        results.append({"peerFleetId": peer, "from": current, "to": nxt, "decision": "ALLOW", "code": "OK"})
    return {"transitions": results}


def evaluate(snapshot: dict[str, Any]) -> dict[str, Any]:
    decision = {
        "schema": "control-shadow-decision-v1",
        "mutationDenied": True,
        "scheduler": evaluate_scheduler(snapshot),
        "risk": evaluate_risk(snapshot),
        "wave": evaluate_wave(snapshot),
        "federation": evaluate_federation(snapshot),
    }
    decision["digest"] = decision_digest(decision)
    return decision


def load_fixture(path: Path = DEFAULT_FIXTURE) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ShadowError(f"shadow fixture cases missing: {path}")
    return cases


def check_fixture(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    cases = load_fixture(path)
    passed = 0
    for case in cases:
        name = str(case.get("name") or "")
        snapshot = case.get("snapshot")
        expect = case.get("expect")
        if not name or not isinstance(snapshot, dict) or not isinstance(expect, dict):
            raise ShadowError(f"invalid shadow case {name!r}")
        decision = evaluate(snapshot)
        if decision.get("digest") != expect.get("digest"):
            raise ShadowError(f"pythonDecisionDigest mismatch for {name}: {decision.get('digest')}")
        if decision.get("scheduler") != expect.get("scheduler"):
            raise ShadowError(f"scheduler mismatch for {name}")
        if decision.get("risk") != expect.get("risk"):
            raise ShadowError(f"risk mismatch for {name}")
        if decision.get("wave") != expect.get("wave"):
            raise ShadowError(f"wave mismatch for {name}")
        if decision.get("federation") != expect.get("federation"):
            raise ShadowError(f"federation mismatch for {name}")
        if decision.get("mutationDenied") is not True:
            raise ShadowError(f"shadow mutation must stay denied for {name}")
        passed += 1
    return {"ok": True, "passed": passed, "total": len(cases)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Python control-plane shadow oracle")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write-expect", action="store_true")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args(argv)
    try:
        if args.write_expect:
            cases = load_fixture(args.fixture)
            rendered = []
            for case in cases:
                snapshot = case["snapshot"]
                decision = evaluate(snapshot)
                rendered.append(
                    {
                        "name": case["name"],
                        "snapshot": snapshot,
                        "expect": {
                            "digest": decision["digest"],
                            "scheduler": decision["scheduler"],
                            "risk": decision["risk"],
                            "wave": decision["wave"],
                            "federation": decision["federation"],
                            "mutationDenied": True,
                        },
                    }
                )
            payload = {
                "schema_version": 1,
                "source_commit": "a37735c68398fc8f795babaa269e2de6a5acd567",
                "kernel": "control-shadow-decision-v1",
                "cases": rendered,
            }
            args.fixture.parent.mkdir(parents=True, exist_ok=True)
            args.fixture.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if args.check or not args.write_expect:
            report = check_fixture(args.fixture)
            print(json.dumps(report, indent=2, sort_keys=True))
    except ShadowError as exc:
        print(f"control plane shadow FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
