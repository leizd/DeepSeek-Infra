"""Fresh-process authority recovery: new interpreter, no inherited handles."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import (
    backup_authority_provider,
    backup_control,
    backup_control_authority,
    backup_control_recovery,
)


@pytest.fixture
def control_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "control"
    root.mkdir()
    db = root / "control.sqlite3"
    monkeypatch.setattr(backup_control, "CONTROL_DIR", root)
    monkeypatch.setattr(backup_control, "CONTROL_DB", db)
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)
    backup_authority_provider.reset_authority_replica_provider()
    return db


def test_subprocess_fresh_process_detects_remote_authority(
    control_db: Path, tmp_path: Path
) -> None:
    """Process B starts with only bootstrap path — never auto-genesis when remote exists."""
    auth_root = tmp_path / "remote-auth"
    auth_root.mkdir()
    control_dir = control_db.parent
    # Process A equivalent: create authority history under auth_root.
    backup_control_authority.configure_authority_anchor_roots([auth_root])
    backup_control.create_policy({"policyId": "pre-disaster", "policyRevision": 1, "enabled": True})
    g_before = None
    with backup_control._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT authority_generation FROM control_authority_head WHERE id = 1"
        ).fetchone()
        g_before = int(row["authority_generation"]) if row else 0
    assert g_before >= 1
    boot_before = backup_control_recovery.get_control_recovery_state()["bootEpoch"]

    # Wipe local DB (disaster).
    for path in (control_db, Path(str(control_db) + "-wal"), Path(str(control_db) + "-shm")):
        if path.is_file():
            path.unlink()

    bootstrap = tmp_path / "bootstrap.json"
    bootstrap.write_text(
        json.dumps(
            {
                "controlAuthority": {
                    "replicas": [
                        {"replicaId": "fs-a", "kind": "filesystem", "root": str(auth_root)}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    script = textwrap.dedent(
        f"""
        import json, sys
        from pathlib import Path
        from deepseek_infra.infra.workspace import (
            backup_authority_provider,
            backup_control,
            backup_control_authority,
            backup_control_recovery,
        )
        control_dir = Path({str(control_dir)!r})
        backup_control.CONTROL_DIR = control_dir
        backup_control.CONTROL_DB = control_dir / "control.sqlite3"
        backup_control_authority.configure_authority_anchor_roots(None)
        backup_control_authority.configure_authority_anchor_stores(None)
        backup_authority_provider.reset_authority_replica_provider()
        # Fresh process: only bootstrap — no inherited stores/globals.
        assert backup_control_authority.get_authority_anchor_stores() == []
        assert backup_control_authority.get_authority_anchor_roots() == []
        backup_authority_provider.install_provider_from_bootstrap(
            bootstrap_path={str(bootstrap)!r}
        )
        verdict = backup_control_recovery.resolve_startup_authority_verdict()
        out = {{
            "verdict": verdict.get("verdict"),
            "allowWorkers": verdict.get("allowWorkers"),
            "allowMutations": verdict.get("allowMutations"),
            "remoteReplicaCount": verdict.get("remoteReplicaCount"),
            "reason": verdict.get("reason"),
            "inheritedStores": len(backup_control_authority.get_authority_anchor_stores()),
            "inheritedRoots": len(backup_control_authority.get_authority_anchor_roots()),
        }}
        # Recovery must be required; mutations blocked.
        blocked = False
        try:
            backup_control.create_policy({{"policyId": "should-block", "policyRevision": 1, "enabled": True}})
        except Exception as exc:
            blocked = "barrier" in str(exc) or "recovery" in str(exc) or "503" in str(exc)
            out["blockError"] = str(exc)[:200]
        out["mutationBlocked"] = blocked
        # Reconstruct then activate.
        recovered = backup_control_recovery.reconstruct_control_authority(
            recovery_targets=[{str(auth_root)!r}],
            activate=True,
        )
        out["recovered"] = {{
            "status": recovered.get("status"),
            "bootEpoch": recovered.get("bootEpoch"),
            "authorityGeneration": recovered.get("authorityGeneration"),
        }}
        policies = backup_control.list_policies()
        out["policyIds"] = [p.get("policyId") for p in policies]
        # Post-recovery mutation must work.
        backup_control.create_policy({{"policyId": "post-recovery", "policyRevision": 1, "enabled": True}})
        out["postRecoveryPolicy"] = True
        state = backup_control_recovery.get_control_recovery_state()
        out["finalState"] = state.get("recoveryState")
        out["finalBootEpoch"] = state.get("bootEpoch")
        print(json.dumps(out))
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.fail(f"fresh process failed rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["verdict"] == backup_control_recovery.RECOVERY_REQUIRED
    assert payload["allowWorkers"] is False
    assert payload["mutationBlocked"] is True
    assert payload["remoteReplicaCount"] >= 1
    assert "pre-disaster" in payload["policyIds"]
    assert payload["postRecoveryPolicy"] is True
    assert payload["finalState"] == backup_control_recovery.RECOVERY_ACTIVE
    assert int(payload["finalBootEpoch"]) > int(boot_before)
    assert int(payload["recovered"]["authorityGeneration"]) >= g_before


def test_subprocess_crash_after_prepared_recovers_exactly_once(
    control_db: Path, tmp_path: Path
) -> None:
    """PREPARED intent survives kill; second process finishes anchor idempotently."""
    auth_root = tmp_path / "auth"
    auth_root.mkdir()
    control_dir = control_db.parent
    backup_control_authority.configure_authority_anchor_roots([auth_root])
    backup_control.create_policy({"policyId": "base", "policyRevision": 1, "enabled": True})

    # Simulate crash after local commit + PREPARED: insert prepared without completing anchor
    # for a second logical mutation snapshot.
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        prepared = backup_control_authority.prepare_authority_mutation_in_tx(conn, kind="crash-test")
        conn.execute("COMMIT")
    mutation_id = str(prepared["mutationId"])
    checkpoint = prepared["checkpoint"]
    assert isinstance(checkpoint, dict)
    gen = int(checkpoint["authorityGeneration"])

    script = textwrap.dedent(
        f"""
        import json, sys
        from pathlib import Path
        from deepseek_infra.infra.workspace import (
            backup_control,
            backup_control_authority,
            backup_control_recovery,
        )
        control_dir = Path({str(control_dir)!r})
        auth_root = Path({str(auth_root)!r})
        backup_control.CONTROL_DIR = control_dir
        backup_control.CONTROL_DB = control_dir / "control.sqlite3"
        backup_control_authority.configure_authority_anchor_roots([auth_root])
        backup_control_authority.configure_authority_anchor_stores(None)
        # Unresolved prepared must block workers/mutations until drained.
        ready = backup_control.ensure_control_authority_ready()
        verdict = backup_control_recovery.resolve_startup_authority_verdict()
        with backup_control._connect() as conn:
            row = conn.execute(
                "SELECT state FROM control_authority_mutations WHERE mutation_id = ?",
                ({mutation_id!r},),
            ).fetchone()
            state = str(row["state"]) if row else None
            head = conn.execute(
                "SELECT authority_generation FROM control_authority_head WHERE id = 1"
            ).fetchone()
            head_gen = int(head["authority_generation"]) if head else 0
        bundle = backup_control_authority.load_authority_bundle(auth_root)
        print(json.dumps({{
            "ready": ready,
            "verdict": verdict.get("verdict"),
            "mutationState": state,
            "headGen": head_gen,
            "remoteGen": int(bundle["head"]["authorityGeneration"]),
            "expectedGen": {gen},
        }}))
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.fail(f"crash recovery process failed\nstdout={proc.stdout}\nstderr={proc.stderr}")
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["mutationState"] == backup_control_authority.MUTATION_DURABLE
    assert int(payload["headGen"]) == gen
    assert int(payload["remoteGen"]) == gen
    assert int(payload["ready"].get("pending") or 0) == 0
