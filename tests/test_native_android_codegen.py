"""Binding and checksum denials for the Android platform code generator."""
import json
from pathlib import Path

import pytest

from scripts import native_android_codegen as codegen


def binding_fixture(root: Path) -> None:
    lock = root / codegen.LOCK
    lock.parent.mkdir(parents=True)
    lock.write_bytes((codegen.ROOT / codegen.LOCK).read_bytes())
    source = root / codegen.SOURCE
    source.parent.mkdir(parents=True)
    source.write_bytes((codegen.ROOT / codegen.SOURCE).read_bytes())
    for path in codegen.OUTPUTS:
        output = root / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes((codegen.ROOT / path).read_bytes())
    manifest = root / codegen.MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(codegen.manifest_for(root, root)), encoding="utf-8")


def test_binding_denies_changed_source_generated_code_and_extra_files(tmp_path: Path) -> None:
    binding_fixture(tmp_path)
    codegen.check_existing(tmp_path)
    for relative in (codegen.SOURCE, codegen.OUTPUTS[0]):
        path = tmp_path / relative
        original = path.read_bytes()
        path.write_bytes(original + b"// unverified change\n")
        with pytest.raises(ValueError, match="bindings drifted"):
            codegen.check_existing(tmp_path)
        path.write_bytes(original)
    (tmp_path / codegen.JAVA_DIR / "Unexpected.java").write_text("class Unexpected {}", encoding="utf-8")
    with pytest.raises(ValueError, match="file set drifted"):
        codegen.check_existing(tmp_path)


def test_generator_rejects_wrong_bytes_before_executing_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binding_fixture(tmp_path)
    plugin = tmp_path / "untrusted-generator.exe"
    plugin.write_bytes(b"not the approved generator")
    monkeypatch.setattr(codegen.subprocess, "check_output", lambda *args, **kwargs: "libprotoc 36.1\n")
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Unverified generator was executed")
    monkeypatch.setattr(codegen.subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="checksum mismatch"):
        codegen.generate(root=tmp_path, protoc=tmp_path / "protoc", plugin=plugin, write=True)


def test_lock_cannot_replace_runtime_or_generator_identity(tmp_path: Path) -> None:
    binding_fixture(tmp_path)
    path = tmp_path / codegen.LOCK
    lock = json.loads(path.read_text(encoding="utf-8"))
    lock["generatorArtifacts"]["windows_amd64"]["sha256"] = "0" * 64
    path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact identities"):
        codegen.load_lock(tmp_path)
