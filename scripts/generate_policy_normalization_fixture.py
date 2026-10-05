"""Freeze the backup-policy normalisation oracle into a checked-in fixture.

`backup_policies.normalize_policy` is the whole of a policy's write semantics: it turns a
client payload into the document that is stored, and every refusal it raises is a refusal
the public API owes the browser. The Go control plane has to reproduce it exactly once
`policy_crud` is Go's, so this script asks the oracle for the answer to a corpus of
payloads and writes both the question and the answer to
`go/internal/store/testdata/policy_normalization_v1.json`.

The fixture is the contract. Regenerating it is how a *deliberate* oracle change is
recorded; a Go port that stops matching it is a regression, not a fixture update.

Usage::

    python scripts/generate_policy_normalization_fixture.py            # write the fixture
    python scripts/generate_policy_normalization_fixture.py --check    # fail on drift
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.workspace import backup_policies  # noqa: E402

FIXTURE = REPO / "go" / "internal" / "policy" / "testdata" / "policy_normalization_v1.json"

RECIPIENT_A = "age1fu59d59ghmr8x2t5dyzjs9xdcjgnakujp7mjy7cz2v7fq6vjqypskh4e62"
RECIPIENT_B = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"

#: `(name, payload)`. `policyId`/`createdAt` are supplied by the harness so the oracle's
#: entropy and clock never reach the fixture.
CASES: list[tuple[str, Any]] = [
    ("minimal", {"name": "nightly"}),
    ("schema-version-1-accepted", {"name": "nightly", "schemaVersion": 1}),
    ("explicit-target", {"name": "nightly", "primaryTargetId": "target_s3_primary"}),
    ("legacy-target-alias", {"name": "nightly", "targetId": "target_s3_legacy"}),
    ("full-schedule", {
        "name": "full",
        "enabled": True,
        "schedule": {"cron": "*/15 1-5 * * 1,3", "timezone": "Asia/Shanghai", "misfirePolicy": "run-once",
                     "catchupWindowSeconds": 3600, "jitterSeconds": 30},
        "scope": {"mode": "project", "projectIds": ["alpha", "beta.gamma"], "includeHistory": False,
                  "includeExternalState": False, "coveragePolicy": "best-effort"},
        "frontendMirror": {"mode": "required", "maxAgeSeconds": 7200, "profileId": "mirror_main"},
        "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A, RECIPIENT_B]},
        "incremental": {"mode": "cdc", "maxChainDepth": 4, "fullIntervalDays": 3, "maxDeltaRatio": 0.5,
                        "largeFileMode": "whole", "largeFileThresholdBytes": 2097152,
                        "scanWorkers": 2, "maxInFlightBytes": 16777216},
        "retry": {"maxAttempts": 5, "initialBackoffSeconds": 30, "maxBackoffSeconds": 600},
        "recoveryObjectives": {"maxRpoSeconds": 900, "maxScrubAgeSeconds": 172800,
                               "maxDrillAgeSeconds": 1209600, "maxReplicaLagSeconds": 120,
                               "maxRtoSeconds": 1800},
        "costObjectives": {"maxMonthlyStorageCostUsd": 12.5, "maxMonthlyEgressCostUsd": 3,
                           "maxRebalanceCostUsdPerDay": 0, "requireKnownRates": True},
        "recoveryDrill": {"enabled": True, "cron": "0 4 * * 0", "provider": "managed-local",
                          "credentialRef": "env:DRILL"},
        "replication": {"enabled": True, "targets": [{"targetId": "target_replica_a", "mode": "best-effort"},
                                                     {"targetId": "managed-local"}],
                        "minCommittedCopies": 2, "minFailureDomains": 1, "minRegions": 1,
                        "maxCopiesPerFailureDomain": 2, "maxReplicaLagSeconds": 600},
        "federatedDurability": {"enabled": True, "minFederatedCopies": 2, "minDistinctFleets": 2,
                                "maxFederatedCopyAge": 86400,
                                "allowedPeerFleets": ["fleet-b", "fleet-a"],
                                "allowedJurisdictions": ["cn-beijing", "eu-west"]},
        "placement": {"minFreeBytes": 1073741824, "minFreePercent": 5.5, "softWatermarkPercent": 70,
                      "hardWatermarkPercent": 85, "maxCopiesPerFailureDomain": 3,
                      "maintenanceWindow": {"timezone": "UTC", "start": "01:00", "end": "05:00"}},
        "recoveryPlacement": {"hotWindowSeconds": 3600, "warmWindowSeconds": 7200,
                              "archiveAfterSeconds": 86400, "hotRestoreP90Seconds": 60,
                              "warmRestoreP90Seconds": 900, "minHotCopies": 0, "minWarmRegions": 2,
                              "enabled": False},
        "retentionPolicyId": "keep-30",
        "policyRevision": 7,
    }),
    ("sections-as-explicit-nulls", {
        "name": "nulls", "schedule": None, "scope": None, "frontendMirror": None, "protection": None,
        "incremental": None, "retry": None, "recoveryObjectives": None, "costObjectives": None,
        "recoveryDrill": None, "replication": None, "federatedDurability": None, "placement": None,
        "recoveryPlacement": None,
    }),
    ("incremental-off-hides-scan-fields", {"name": "off", "incremental": {"mode": "off"}}),
    ("incremental-off-with-scan-workers", {"name": "off2", "incremental": {"mode": "off", "scanWorkers": 3}}),
    ("cost-alias-both-spellings", {
        "name": "cost", "costObjectives": {"maxEstimatedMonthlyStorageUsd": 1,
                                           "maxMonthlyStorageCostUsd": 9,
                                           "maxEstimatedMonthlyEgressUsd": 2},
    }),
    ("cost-zero-is-kept", {"name": "cost0", "costObjectives": {"maxMonthlyStorageCostUsd": 0}}),
    ("recovery-objectives-subset", {"name": "rpo", "recoveryObjectives": {"maxRpoSeconds": 60}}),
    ("replication-disabled-defaults", {"name": "repl", "replication": {}}),
    ("federated-disabled-defaults", {"name": "fed", "federatedDurability": {}}),
    ("frontend-mirror-without-profile", {"name": "mirror", "frontendMirror": {"mode": "excluded"}}),
    ("recovery-drill-empties", {"name": "drill", "recoveryDrill": {"enabled": False, "cron": "",
                                                                   "provider": "", "credentialRef": ""}}),
    # --- refusals ---------------------------------------------------------------
    ("refuse-blank-name", {"name": "   "}),
    ("refuse-missing-name", {}),
    ("refuse-long-name", {"name": "x" * 121}),
    ("refuse-schema-version", {"name": "n", "schemaVersion": 3}),
    ("refuse-secret-marker", {"name": "n", "retentionPolicyId": "AGE-SECRET-KEY-1"}),
    ("refuse-secret-in-nested", {"name": "n", "recoveryDrill": {"credentialRef": "Bearer abc"}}),
    ("refuse-target-id", {"name": "n", "primaryTargetId": "bucket-name"}),
    ("refuse-cron-four-fields", {"name": "n", "schedule": {"cron": "* * * *", "timezone": "UTC"}}),
    ("refuse-cron-range", {"name": "n", "schedule": {"cron": "99 * * * *", "timezone": "UTC"}}),
    ("refuse-cron-nan", {"name": "n", "schedule": {"cron": "x * * * *", "timezone": "UTC"}}),
    ("refuse-cron-zero-step", {"name": "n", "schedule": {"cron": "*/0 * * * *", "timezone": "UTC"}}),
    ("refuse-cron-empty-part", {"name": "n", "schedule": {"cron": "1,,2 * * * *", "timezone": "UTC"}}),
    ("refuse-cron-descending-range", {"name": "n", "schedule": {"cron": "5-3 * * * *", "timezone": "UTC"}}),
    ("refuse-timezone", {"name": "n", "schedule": {"cron": "0 3 * * *", "timezone": "Mars/Phobos"}}),
    ("refuse-blank-timezone", {"name": "n", "schedule": {"cron": "0 3 * * *", "timezone": " "}}),
    ("refuse-schedule-misfire", {"name": "n", "schedule": {"cron": "0 3 * * *", "timezone": "UTC",
                                                           "misfirePolicy": "later"}}),
    ("refuse-schedule-catchup-low", {"name": "n", "schedule": {"cron": "0 3 * * *", "timezone": "UTC",
                                                               "catchupWindowSeconds": 59}}),
    ("refuse-schedule-jitter", {"name": "n", "schedule": {"cron": "0 3 * * *", "timezone": "UTC",
                                                          "jitterSeconds": 3601}}),
    ("refuse-schedule-not-object", {"name": "n", "schedule": "0 3 * * *"}),
    ("refuse-scope-project-without-ids", {"name": "n", "scope": {"mode": "project"}}),
    ("refuse-scope-project-id", {"name": "n", "scope": {"mode": "project", "projectIds": ["Bad Id"]}}),
    ("refuse-scope-mode", {"name": "n", "scope": {"mode": "partial"}}),
    ("refuse-mirror-mode", {"name": "n", "frontendMirror": {"mode": "maybe"}}),
    ("refuse-mirror-profile", {"name": "n", "frontendMirror": {"profileId": "Bad Profile"}}),
    ("refuse-mirror-max-age", {"name": "n", "frontendMirror": {"maxAgeSeconds": 10}}),
    ("refuse-protection-passphrase", {"name": "n", "protection": {"mode": "passphrase"}}),
    ("refuse-protection-missing-mode", {"name": "n", "protection": {}}),
    ("refuse-protection-no-recipients", {"name": "n", "protection": {"mode": "age-recipient", "recipients": []}}),
    ("refuse-protection-not-age1", {"name": "n", "protection": {"mode": "age-recipient", "recipients": ["nope"]}}),
    ("refuse-protection-too-many", {"name": "n", "protection": {"mode": "age-recipient",
                                                                "recipients": [f"age1{i}" for i in range(17)]}}),
    ("refuse-incremental-mode", {"name": "n", "incremental": {"mode": "delta"}}),
    ("refuse-incremental-ratio", {"name": "n", "incremental": {"maxDeltaRatio": 0.95}}),
    ("refuse-incremental-ratio-bool", {"name": "n", "incremental": {"maxDeltaRatio": True}}),
    ("refuse-incremental-depth", {"name": "n", "incremental": {"maxChainDepth": 0}}),
    ("refuse-incremental-large-mode", {"name": "n", "incremental": {"largeFileMode": "chunk"}}),
    ("refuse-retry-order", {"name": "n", "retry": {"initialBackoffSeconds": 900, "maxBackoffSeconds": 60}}),
    ("refuse-retry-attempts", {"name": "n", "retry": {"maxAttempts": 11}}),
    ("refuse-rpo-low", {"name": "n", "recoveryObjectives": {"maxRpoSeconds": 30}}),
    ("refuse-rebalance-negative", {"name": "n", "costObjectives": {"maxRebalanceCostUsdPerDay": -1}}),
    ("refuse-cost-not-number", {"name": "n", "costObjectives": {"maxMonthlyStorageCostUsd": "cheap"}}),
    ("refuse-drill-cron", {"name": "n", "recoveryDrill": {"enabled": True, "cron": "bad"}}),
    ("refuse-replication-not-array", {"name": "n", "replication": {"targets": "target_a"}}),
    ("refuse-replication-entry", {"name": "n", "replication": {"targets": ["target_a"]}}),
    ("refuse-replication-primary-repeat", {"name": "n", "primaryTargetId": "target_a",
                                           "replication": {"targets": [{"targetId": "target_a"}]}}),
    ("refuse-replication-duplicate", {"name": "n", "replication": {"targets": [{"targetId": "target_b"},
                                                                              {"targetId": "target_b"}]}}),
    ("refuse-replication-min-copies", {"name": "n", "replication": {"enabled": True,
                                                                    "targets": [{"targetId": "target_b"}],
                                                                    "minCommittedCopies": 5}}),
    ("refuse-replication-bad-target", {"name": "n", "replication": {"targets": [{"targetId": "bucket"}]}}),
    ("refuse-federated-unknown-field", {"name": "n", "federatedDurability": {"minFederatedCopies": 1,
                                                                             "extra": True}}),
    ("refuse-federated-min-fleets", {"name": "n", "federatedDurability": {"minDistinctFleets": 3,
                                                                          "minFederatedCopies": 2}}),
    ("refuse-federated-enabled-empty", {"name": "n", "federatedDurability": {"enabled": True}}),
    ("refuse-federated-enabled-min-copies", {"name": "n", "federatedDurability": {
        "enabled": True, "minFederatedCopies": 3, "minDistinctFleets": 1,
        "allowedPeerFleets": ["fleet-a"], "allowedJurisdictions": ["cn"]}}),
    ("refuse-federated-duplicate-fleet", {"name": "n", "federatedDurability": {
        "allowedPeerFleets": ["fleet-a", "fleet-a"]}}),
    ("refuse-federated-bad-fleet", {"name": "n", "federatedDurability": {"allowedPeerFleets": ["Fleet A"]}}),
    ("refuse-federated-bad-jurisdiction", {"name": "n", "federatedDurability": {
        "allowedJurisdictions": ["/etc/passwd"]}}),
    ("refuse-federated-peers-not-array", {"name": "n", "federatedDurability": {"allowedPeerFleets": "fleet-a"}}),
    ("refuse-federated-max-age", {"name": "n", "federatedDurability": {"maxFederatedCopyAge": 0}}),
    ("refuse-placement-min-free", {"name": "n", "placement": {"minFreeBytes": -1}}),
    ("refuse-placement-copies", {"name": "n", "placement": {"maxCopiesPerFailureDomain": 17}}),
    ("refuse-recovery-placement-order", {"name": "n", "recoveryPlacement": {"hotWindowSeconds": 100,
                                                                            "warmWindowSeconds": 50}}),
    ("refuse-recovery-placement-not-object", {"name": "n", "recoveryPlacement": "hot"}),
    ("refuse-retention-policy-id", {"name": "n", "retentionPolicyId": "Keep 30"}),
    ("refuse-enabled-not-bool", {"name": "n", "enabled": "yes"}),
    ("refuse-policy-revision-not-int", {"name": "n", "policyRevision": "many"}),
    # --- oracle quirks that are not AppError -----------------------------------
    ("quirk-placement-percent-bool", {"name": "n", "placement": {"minFreePercent": True}}),
    ("quirk-placement-percent-text", {"name": "n", "placement": {"minFreePercent": "lots"}}),
    ("quirk-recovery-placement-text", {"name": "n", "recoveryPlacement": {"hotWindowSeconds": "soon"}}),
]


def outcome(payload: Any, policy_id: str, created_at: str) -> dict[str, Any]:
    """What the oracle answers for one payload, with non-AppError exceptions recorded.

    `updatedAt` is the write clock, so it is masked: the Go port has to reproduce the
    *document*, not this run's timestamp, and a fixture that carried `_now_iso()` would
    only ever match on the second it was generated.
    """
    try:
        normalized = backup_policies.normalize_policy(dict(payload) if isinstance(payload, dict) else payload,
                                                      policy_id=policy_id, created_at=created_at)
    except AppError as exc:
        return {"error": {"message": str(exc), "code": exc.code.value, "status": exc.status}}
    except Exception as exc:  # noqa: BLE001 - the fixture records the exception *class*
        return {"uncaught": type(exc).__name__, "message": str(exc)}
    normalized["updatedAt"] = "<now>"
    return {"ok": normalized}


def build() -> dict[str, Any]:
    cases = []
    for index, (name, payload) in enumerate(CASES):
        policy_id = f"policy_{index:04d}"
        created_at = "2026-10-01T00:00:00Z"
        cases.append({
            "name": name,
            "payload": payload,
            "policyId": policy_id,
            "createdAt": created_at,
            "expected": outcome(payload, policy_id, created_at),
        })
    return {
        "schema": "policy-normalization-fixture-v1",
        "oracle": "deepseek_infra.infra.workspace.backup_policies.normalize_policy",
        "sourceVersion": (REPO / "VERSION").read_text(encoding="utf-8").strip(),
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail instead of writing when the fixture drifts")
    args = parser.parse_args(argv)
    document = build()
    encoded = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.check:
        current = FIXTURE.read_text(encoding="utf-8") if FIXTURE.is_file() else ""
        if current != encoded:
            print(f"policy normalization fixture drifted: {FIXTURE}")
            return 1
        print(f"policy normalization fixture OK ({len(document['cases'])} cases)")
        return 0
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(encoded, encoding="utf-8")
    print(f"wrote {FIXTURE} ({len(document['cases'])} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
