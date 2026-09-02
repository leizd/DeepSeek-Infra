#!/usr/bin/env python3
"""Secret-free subprocess actions for the real Federation Evidence topology."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.workspace import backup_replication, backup_targets  # noqa: E402


def _document(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError("federation-e2e-command-must-be-object")
    return value


def _register_target(command: dict[str, Any]) -> dict[str, Any]:
    required = {
        "action",
        "bucket",
        "prefix",
        "endpointUrl",
        "region",
        "failureDomain",
        "jurisdiction",
    }
    if set(command) != required:
        raise ValueError("federation-e2e-register-fields-invalid")
    return backup_targets.init_s3_target(
        bucket=str(command["bucket"]),
        prefix=str(command["prefix"]),
        endpoint_url=str(command["endpointUrl"]),
        region=str(command["region"]),
        failure_domain=str(command["failureDomain"]),
        provider="minio",
        jurisdiction=str(command["jurisdiction"]),
        credential_provider={"type": "aws-default-chain"},
        probe=True,
    )


def _rebalance(command: dict[str, Any]) -> dict[str, Any]:
    required = {
        "action",
        "policyId",
        "backupId",
        "sourceTargetId",
        "destinationTargetId",
    }
    if set(command) != required:
        raise ValueError("federation-e2e-rebalance-fields-invalid")
    policy_id = str(command["policyId"])
    backup_id = str(command["backupId"])
    destination_target_id = str(command["destinationTargetId"])
    job = backup_replication.create_rebalance_job(
        policy_id=policy_id,
        backup_id=backup_id,
        source_target_id=str(command["sourceTargetId"]),
        dest_target_id=destination_target_id,
        reason="federation-four-minio-evidence",
        prune_source_after=False,
    )
    result = backup_replication.execute_rebalance_job(
        str(job["jobId"]),
        instance_id="federation-e2e-rebalance",
    )
    target = backup_targets.get_target(destination_target_id)
    status, receipt, commit = backup_replication.authenticate_committed_copy(target, policy_id, backup_id)
    return {
        "result": result,
        "authenticationStatus": status,
        "receipt": receipt,
        "commit": commit,
    }


def main() -> int:
    command = _document(json.load(sys.stdin))
    action = str(command.get("action") or "")
    if action == "register-s3-target":
        result = _register_target(command)
    elif action == "rebalance":
        result = _rebalance(command)
    else:
        raise ValueError("federation-e2e-action-invalid")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
