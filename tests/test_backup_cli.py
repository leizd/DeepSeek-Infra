from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import backup_crypto
from scripts import backup_catalog as catalog_cli
from scripts import backup_policy as policy_cli
from scripts import backup_target as target_cli


RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"


@pytest.fixture
def stub_crypto(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prefix = b"age-encryption.org/v1\n"

    def encrypt_stream(target: Path, write_plaintext: object, *, mode: str, secret: object = None, recipients: tuple[str, ...] = (), cancel_event: object = None) -> None:
        import io

        buffer = io.BytesIO()
        write_plaintext(buffer)  # type: ignore[operator]
        target.write_bytes(prefix + bytes(buffer.getbuffer())[::-1])

    def decrypt_file(source: Path, target: Path, *, kind: str, secret: bytearray, cancel_event: object = None) -> None:
        raw = source.read_bytes()
        assert raw.startswith(prefix)
        target.write_bytes(raw[len(prefix):][::-1])

    monkeypatch.setattr(backup_crypto, "encrypt_stream", encrypt_stream)
    monkeypatch.setattr(backup_crypto, "decrypt_file", decrypt_file)
    monkeypatch.setattr(backup_crypto, "generate_identity", lambda: {"identity": "AGE-SECRET-KEY-1EPH", "recipient": "age1eph"})
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: {"age": True})
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def _run(cli: object, argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr("sys.argv", ["cli", *argv])
    code = cli.main()  # type: ignore[attr-defined]
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def test_target_cli_lifecycle(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))
    directory = tmp_path / "usb"
    directory.mkdir()
    code, created = _run(target_cli, ["init", str(directory), "--label", "USB"], monkeypatch, capsys)
    assert code == 0
    target_id = str(created["targetId"])
    code, probe = _run(target_cli, ["probe", target_id], monkeypatch, capsys)
    assert code == 0 and probe["ready"] is True
    code, listing = _run(target_cli, ["list"], monkeypatch, capsys)
    targets = listing["targets"]
    assert code == 0 and isinstance(targets, list)
    assert any(item["targetId"] == target_id for item in targets)
    code, deleted = _run(target_cli, ["delete", target_id], monkeypatch, capsys)
    assert code == 0 and deleted["deleted"] is True


def test_policy_cli_create_run_and_runs(tmp_settings: Path, stub_crypto: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")
    code, policy = _run(
        policy_cli,
        ["create", "--cron", "0 3 * * *", "--timezone", "UTC", "--recipient", RECIPIENT_A, "--coverage-policy", "best-effort", "--no-external-state", "--mirror-mode", "excluded"],
        monkeypatch,
        capsys,
    )
    assert code == 0
    policy_id = str(policy["policyId"])
    code, listing = _run(policy_cli, ["list"], monkeypatch, capsys)
    assert code == 0 and len(listing["policies"]) == 1  # type: ignore[arg-type]
    code, outcome = _run(policy_cli, ["run", policy_id], monkeypatch, capsys)
    assert code == 0
    assert outcome["phase"] == "complete"
    code, runs = _run(policy_cli, ["list-runs", "--policy-id", policy_id], monkeypatch, capsys)
    assert code == 0
    assert runs["runs"][0]["phase"] == "complete"  # type: ignore[index]
    code, catalog = _run(catalog_cli, ["list"], monkeypatch, capsys)
    assert code == 0
    assert catalog["chainValid"] is True
    backup_id = str(outcome["backupId"])
    code, scrubbed = _run(catalog_cli, ["scrub", backup_id], monkeypatch, capsys)
    assert code == 0 and scrubbed["ok"] is True
    code, pinned = _run(catalog_cli, ["pin", backup_id], monkeypatch, capsys)
    assert code == 0 and pinned["pinned"] is True
    code, unpinned = _run(catalog_cli, ["pin", backup_id, "--unpin"], monkeypatch, capsys)
    assert code == 0 and unpinned["pinned"] is False
    code, preview = _run(catalog_cli, ["retention-preview", policy_id], monkeypatch, capsys)
    assert code == 0
    assert "keep" in preview


def test_policy_cli_rejects_invalid(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.argv", ["cli", "create", "--cron", "bad", "--timezone", "UTC", "--recipient", RECIPIENT_A])
    assert policy_cli.main() == 1
    capsys.readouterr()
    monkeypatch.setattr("sys.argv", ["cli", "run", "policy_missing"])
    assert policy_cli.main() == 1


def test_catalog_cli_rebuild(tmp_settings: Path, stub_crypto: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from deepseek_infra.infra.workspace import backup_publish, backups

    root = backups.BACKUP_DIR
    for name in backup_publish.LAYOUT_DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "receipts" / "b1.receipt.json").write_text(
        json.dumps({"schemaVersion": 1, "backupId": "b1", "filename": "b1.age", "createdAt": "2026-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    code, result = _run(catalog_cli, ["rebuild"], monkeypatch, capsys)
    assert code == 0
    assert result["rebuilt"] == 1
    assert result["chainValid"] is True
