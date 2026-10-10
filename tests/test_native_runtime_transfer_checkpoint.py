from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import backup_replication as br
from scripts.native_runtime_contract import validate_corpus


ROOT = Path(__file__).resolve().parents[1]


def _replay(
    *,
    local_parts: list[Any],
    remote_parts: list[Any],
    source_chunks: list[bytes],
    next_offset: int,
    upload_id: str,
    now: str,
    upload_missing: bool,
) -> dict[str, Any]:
    progress_state: dict[str, Any] = {
        "multipartUploadId": upload_id,
        "parts": copy.deepcopy(local_parts),
        "nextOffset": next_offset,
    }
    if upload_missing:
        progress_state["multipartRestart"] = {
            "previousUploadId": upload_id,
            "reason": "provider-upload-not-found",
            "restartedAt": now,
        }
        progress_state.pop("multipartUploadId", None)
        progress_state["parts"] = []
        progress_state["nextOffset"] = 0
        return {"outcome": "restart", "progress": progress_state, "error": None}

    stream = iter(source_chunks)
    local_parts = [dict(item) for item in list(progress_state.get("parts") or []) if isinstance(item, dict)]
    remote_parts = sorted(
        [dict(item) for item in remote_parts if isinstance(item, dict)],
        key=br._part_number,
    )
    conflict_reason: str | None = None
    if len(remote_parts) < len(local_parts):
        conflict_reason = f"remote-part-count-behind:{len(remote_parts)}<{len(local_parts)}"
    canonical_remote: list[dict[str, Any]] = []
    expected_local_offset = 0
    for index, remote_part in enumerate(remote_parts, start=1):
        if br._part_number(remote_part) != index:
            conflict_reason = conflict_reason or f"non-contiguous-remote-part:{br._part_number(remote_part)}"
            break
        try:
            source_chunk = next(stream)
        except StopIteration:
            conflict_reason = conflict_reason or "remote-parts-exceed-source"
            break
        matches, match_reason = br._part_matches_source(remote_part, source_chunk)
        if not matches:
            conflict_reason = conflict_reason or f"remote-part-{index}-{match_reason}"
            break
        if index <= len(local_parts):
            local_part = local_parts[index - 1]
            if br._part_number(local_part) != index:
                conflict_reason = conflict_reason or f"non-contiguous-local-part:{br._part_number(local_part)}"
                break
            local_size = int(local_part.get("size") or len(source_chunk))
            if local_size != int(remote_part.get("size") or 0):
                conflict_reason = conflict_reason or f"part-{index}-local-remote-size-conflict"
                break
            local_etag = br._normalized_etag(local_part.get("etag"))
            remote_etag = br._normalized_etag(remote_part.get("etag"))
            if len(local_etag) in {32, 64} and local_etag != remote_etag:
                conflict_reason = conflict_reason or f"part-{index}-local-remote-etag-conflict"
                break
            local_checksum = str(local_part.get("checksumSha256") or "")
            if local_checksum and local_checksum != hashlib.sha256(source_chunk).hexdigest():
                conflict_reason = conflict_reason or f"part-{index}-local-checksum-conflict"
                break
            expected_local_offset += len(source_chunk)
        canonical_remote.append(br._canonical_progress_part(remote_part, source_chunk))

    local_next_offset = int(progress_state.get("nextOffset") or 0)
    if conflict_reason is None and local_next_offset != expected_local_offset:
        conflict_reason = f"local-offset-conflict:{local_next_offset}!={expected_local_offset}"
    if conflict_reason is not None:
        progress_state["multipartQuarantine"] = {
            "uploadId": upload_id,
            "reason": conflict_reason,
            "localParts": local_parts,
            "remoteParts": remote_parts,
            "quarantinedAt": now,
        }
        progress_state.pop("multipartUploadId", None)
        progress_state["parts"] = []
        progress_state["nextOffset"] = 0
        return {
            "outcome": "conflict",
            "progress": progress_state,
            "error": f"multipart-reconciliation-conflict:{conflict_reason}",
        }

    progress_state["parts"] = canonical_remote
    progress_state["nextOffset"] = sum(int(item["size"]) for item in canonical_remote)
    reconcile_status = "remote-ahead-adopted" if len(remote_parts) > len(local_parts) else "remote-matches-local"
    progress_state["multipartReconciliation"] = {
        "status": reconcile_status,
        "uploadId": upload_id,
        "localPartCount": len(local_parts),
        "remotePartCount": len(remote_parts),
        "reconciledAt": now,
    }
    return {"outcome": "adopt", "progress": progress_state, "error": None}


def test_v21_multipart_checkpoint_matches_python_4_8_0_helpers() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v21/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == "a37735c68398fc8f795babaa269e2de6a5acd567"
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"
    assert fixture["ast_matches_source_commit"] == {
        "_part_number": True,
        "_normalized_etag": True,
        "_part_matches_source": True,
        "_canonical_progress_part": True,
        "_quarantine_multipart_progress": True,
    }
    for case in fixture["cases"]:
        result = _replay(
            local_parts=case["local_parts"],
            remote_parts=case["remote_parts"],
            source_chunks=[bytes.fromhex(item) for item in case["source_chunks"]],
            next_offset=case["next_offset"],
            upload_id=fixture["upload_id"],
            now=fixture["now"],
            upload_missing=bool(case.get("upload_missing")),
        )
        assert result["outcome"] == case["expected_outcome"], case["name"]
        assert result["error"] == case["expected_error"], case["name"]
        assert result["progress"] == case["expected_progress"], case["name"]
