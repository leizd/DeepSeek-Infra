from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from deepseek_infra.infra.native_runtime.authority import (
    PythonRuntimeDisabledError,
    PythonWriterMechanicallyDeniedError,
    RuntimeMode,
    assert_production_python_allowed,
    assert_python_writer_allowed,
    get_runtime_mode,
)


def test_python_writer_allowed_in_legacy_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_GO_CONTROL", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUNTIME_MODE", raising=False)
    assert get_runtime_mode() == RuntimeMode.PYTHON_AUTHORITATIVE
    assert_python_writer_allowed("scheduler")
    assert_python_writer_allowed("policy")


def test_python_writer_mechanically_denied_when_go_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_GO_CONTROL", "1")
    assert get_runtime_mode() == RuntimeMode.GO_AUTHORITATIVE

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'scheduler' write mutation is mechanically denied"):
        assert_python_writer_allowed("scheduler")

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'policy' write mutation is mechanically denied"):
        assert_python_writer_allowed("policy")


def test_python_production_disabled_without_legacy_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    monkeypatch.delenv("DEEPSEEK_LEGACY_PYTHON", raising=False)
    assert get_runtime_mode() == RuntimeMode.PYTHON_DISABLED

    with pytest.raises(PythonRuntimeDisabledError, match="Python production server is de-authoritized"):
        assert_production_python_allowed()


def test_python_production_rollback_allowed_with_legacy_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    monkeypatch.setenv("DEEPSEEK_LEGACY_PYTHON", "1")
    assert_production_python_allowed()


def test_real_policy_writer_mechanically_denied_when_go_authoritative(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: None,
) -> None:
    from deepseek_infra.infra.workspace.backup_policies import create_policy

    monkeypatch.setenv("DEEPSEEK_GO_CONTROL", "1")
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'policy' write mutation is mechanically denied"):
        create_policy({"policyId": "pol-test-deny", "schedule": "0 0 * * *"})


def test_real_scheduler_worker_mechanically_denied_when_go_authoritative(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: None,
) -> None:
    from deepseek_infra.infra.workspace.backup_scheduler import claim_due_drill_slots, worker_tick

    monkeypatch.setenv("DEEPSEEK_GO_CONTROL", "1")
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'scheduler' write mutation is mechanically denied"):
        claim_due_drill_slots([], instance_id="inst-1")

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'scheduler' write mutation is mechanically denied"):
        worker_tick(instance_id="inst-1", executor=lambda run: None)


def test_real_server_startup_denied_when_python_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deepseek_infra.web.server import create_app

    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    monkeypatch.delenv("DEEPSEEK_LEGACY_PYTHON", raising=False)

    with pytest.raises(PythonRuntimeDisabledError, match="Python production server is de-authoritized"):
        create_app()


#: A syntactically valid age recipient; the mirror's gate fires before any crypto runs,
#: so nothing here is ever sealed.
_TEST_RECIPIENT = "age1fu59d59ghmr8x2t5dyzjs9xdcjgnakujp7mjy7cz2v7fq6vjqypskh4e62"


def test_the_mirror_store_is_a_declared_rust_data_domain() -> None:
    from deepseek_infra.infra.native_runtime.authority import RUST_DATA_DOMAINS

    assert "frontend_mirror_store" in RUST_DATA_DOMAINS


def test_real_mirror_writer_mechanically_denied_when_python_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: Path,
) -> None:
    """The mirror has one Python writer, and this is it.

    `.backup-mirror/` is the sealed frontend replica: immutable generations plus the
    `HEAD.json` pointer. `put_frontend_mirror` is the only path that creates a
    generation, moves HEAD, drops the legacy 4.4.4 files or prunes an old generation, so
    gating there is what makes the handover mechanical. The denial has to happen before
    the directory is even created — a refused upload that still left a generation behind
    would be worse than no gate at all.
    """
    from deepseek_infra.infra.workspace import backup_mirror

    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    monkeypatch.delenv("DEEPSEEK_LEGACY_PYTHON", raising=False)

    paths_before = sorted(path.relative_to(tmp_settings).as_posix() for path in tmp_settings.rglob("*"))
    with pytest.raises(
        PythonWriterMechanicallyDeniedError,
        match="Domain 'frontend_mirror_store' write mutation is mechanically denied",
    ):
        backup_mirror.put_frontend_mirror(
            "mirror_denied",
            {"schemaVersion": 1, "conversations": [], "conflicts": []},
            source_epoch="epoch-1",
            recipients=[_TEST_RECIPIENT],
        )
    assert not (tmp_settings / ".backup-mirror" / "mirror_denied").exists()
    assert sorted(path.relative_to(tmp_settings).as_posix() for path in tmp_settings.rglob("*")) == paths_before

    # Reads stay available: the Rust store writes the same format, so the Python
    # scheduler and restore paths keep working against a Rust-owned mirror. That is the
    # handback direction, and denying it would turn the gate into a data outage.
    assert backup_mirror.list_mirrors() == []
    assert backup_mirror.mirror_status("mirror_denied") == {"status": "missing", "profileId": "mirror_denied"}


def test_the_mirror_writer_is_allowed_while_python_still_owns_the_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: Path,
) -> None:
    """The gate is conditional, not a permanent refusal.

    A gate that refused in every mode would pass the denial test above while making the
    feature unusable, so the permitted mode is exercised too: the same call reaches the
    real writer and publishes a verified generation.
    """
    import json

    from deepseek_infra.infra.workspace import backup_crypto, backup_mirror

    monkeypatch.delenv("DEEPSEEK_RUNTIME_MODE", raising=False)
    monkeypatch.delenv("DEEPSEEK_GO_CONTROL", raising=False)
    assert get_runtime_mode() == RuntimeMode.PYTHON_AUTHORITATIVE
    if backup_crypto.helper_path() is None:
        pytest.skip("the pinned backup-crypto helper is not built")

    identity = backup_crypto.generate_identity()
    body = {"schemaVersion": 1, "conversations": [{"id": "c-1"}], "conflicts": []}
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    envelope = {**body, "digest": digest}
    metadata = backup_mirror.put_frontend_mirror(
        "mirror_allowed",
        envelope,
        source_epoch="epoch-1",
        recipients=[str(identity["recipient"])],
        client_sequence=1,
    )
    assert metadata["creationVerified"] is True
    head = json.loads((tmp_settings / ".backup-mirror" / "mirror_allowed" / "HEAD.json").read_text(encoding="utf-8"))
    assert head["generationId"] == metadata["generationId"]

