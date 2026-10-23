"""Offline package checks; actual app execution is covered by Android instrumentation."""

from __future__ import annotations

import hashlib
import importlib
import json
import struct
import zipfile
import subprocess
from pathlib import Path

import pytest

from scripts.build_android_native import inspect_executable, source_identity, stage_bundle


VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()


def elf(path: Path, *, machine: int = 183, interpreter: str = "/system/bin/linker64", alignment: int = 16384) -> Path:
    interp = interpreter.encode() + b"\0" if interpreter else b""
    ident = b"\x7fELF\x02\x01\x01" + bytes(9)
    header = struct.pack("<HHIQQQIHHHHHH", 3, machine, 1, 0, 64, 0, 0, 64, 56, 2 if interp else 1, 0, 0, 0)
    segment = struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, 256, 256, alignment)
    program = struct.pack("<IIQQQQQQ", 3, 4, 176, 0, 0, len(interp), len(interp), 1) if interp else b""
    path.write_bytes(((ident + header + segment + program).ljust(176, b"\0") + interp).ljust(256, b"\0"))
    return path


def test_native_package_rejects_foreign_loader_architecture_and_small_pages(tmp_path: Path) -> None:
    path = elf(tmp_path / "gateway")
    assert inspect_executable(path, "arm64-v8a", "rust")["interpreter"] == "/system/bin/linker64"
    for params in ({"machine": 62}, {"interpreter": "/lib64/ld-linux-x86-64.so.2"}, {"alignment": 4096}):
        elf(path, **params)
        with pytest.raises(ValueError):
            inspect_executable(path, "arm64-v8a", "rust")
    path.write_bytes(b"not an ELF")
    with pytest.raises(ValueError):
        inspect_executable(path, "arm64-v8a", "rust")


def test_native_bundle_omits_python_and_preserves_input_files(tmp_path: Path) -> None:
    root, output = tmp_path / "repo", tmp_path / "output"
    (root / "static/ui").mkdir(parents=True)
    (root / "static/ui/index.html").write_text("<html>React</html>", encoding="utf-8")
    (root / "static/ui/.vite").mkdir()
    (root / "static/ui/.vite/manifest.json").write_text("{}", encoding="utf-8")
    (root / "skills/builtin").mkdir(parents=True)
    (root / "skills/builtin/example.json").write_text('{"id":"example"}', encoding="utf-8")
    (root / "skills/__pycache__").mkdir()
    (root / "skills/__pycache__/cached.pyc").write_bytes(b"excluded")
    (root / "skills/offline.py").write_text("raise RuntimeError()", encoding="utf-8")
    golden = root / "evals/golden/skills/skill_eval_cases.jsonl"
    golden.parent.mkdir(parents=True)
    golden.write_text('{"caseId":"tutor-golden","skillId":"tutor"}\n', encoding="utf-8")
    (root / "VERSION").write_text(f"{VERSION}\n", encoding="utf-8")
    rust = elf(tmp_path / "rust")
    go = elf(tmp_path / "go")
    manifest = stage_bundle(root, output, {"arm64-v8a": {"rust": rust, "go": go, "worker": rust, "launcher": go}})
    assert manifest["version"] == VERSION
    assert {entry["path"] for entry in manifest["assets"]} == {
        "skills/builtin/example.json", "static/ui/index.html", "static/ui/.vite/manifest.json",
        "evals/golden/skills/skill_eval_cases.jsonl",
    }
    assert (output / "jniLibs/arm64-v8a/libdeepseek_gateway.so").read_bytes() == rust.read_bytes()
    assert (output / "jniLibs/arm64-v8a/libdeepseek_control.so").read_bytes() == go.read_bytes()
    assert (root / "skills/offline.py").exists()
    assert not list(output.rglob("*.py")) and not list(output.rglob("*.pyc"))
    assert (output / "assets/native" / golden.relative_to(root)).read_bytes() == golden.read_bytes()


def test_native_bundle_requires_the_runtime_evaluation_corpus_before_staging(tmp_path: Path) -> None:
    root, output = tmp_path / "repo", tmp_path / "output"
    (root / "static/ui").mkdir(parents=True)
    (root / "static/ui/index.html").write_text("<html>React</html>", encoding="utf-8")
    (root / "skills").mkdir()
    (root / "VERSION").write_text(f"{VERSION}\n", encoding="utf-8")
    rust, go = elf(tmp_path / "rust"), elf(tmp_path / "go")
    with pytest.raises(ValueError, match="evaluation corpus"):
        stage_bundle(root, output, {"arm64-v8a": {"rust": rust, "go": go, "worker": rust, "launcher": go}})
    assert not output.exists()


def test_native_bundle_rebuild_replaces_stale_assets_and_preserves_siblings(tmp_path: Path) -> None:
    root, output = tmp_path / "repo", tmp_path / "output"
    (root / "static/ui").mkdir(parents=True)
    (root / "static/ui/index.html").write_text("<html>first</html>", encoding="utf-8")
    old_asset = root / "static/ui/old-build.js"
    old_asset.write_text("old build", encoding="utf-8")
    (root / "skills").mkdir()
    golden = root / "evals/golden/skills/skill_eval_cases.jsonl"
    golden.parent.mkdir(parents=True)
    golden.write_text('{"caseId":"one"}\n', encoding="utf-8")
    (root / "VERSION").write_text(f"{VERSION}\n", encoding="utf-8")
    rust, go = elf(tmp_path / "rust"), elf(tmp_path / "go")
    binaries = {"arm64-v8a": {"rust": rust, "go": go, "worker": rust, "launcher": go}}
    stage_bundle(root, output, binaries)
    sibling = output / "assets/retained.txt"
    sibling.write_bytes(b"unrelated output")
    old_asset.unlink()
    (root / "static/ui/current-build.js").write_text("current build", encoding="utf-8")
    manifest = stage_bundle(root, output, binaries)
    assets = output / "assets/native"
    actual = {path.relative_to(assets).as_posix() for path in assets.rglob("*") if path.is_file()}
    assert actual == {entry["path"] for entry in manifest["assets"]} | {"bundle.json"}
    assert not (assets / "static/ui/old-build.js").exists()
    assert sibling.read_bytes() == b"unrelated output"


def publication_lock(winerror: int | None) -> PermissionError:
    error = PermissionError(13, "asset publication is locked")
    if winerror is not None:
        setattr(error, "winerror", winerror)
    return error


@pytest.mark.parametrize("winerror", [5, 32])
def test_native_bundle_recovers_a_transient_windows_publication_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int,
) -> None:
    original_rename = Path.rename
    injected = False
    waits: list[float] = []

    def locked_once(source: Path, destination: Path | str) -> Path:
        nonlocal injected
        if source.name == "native" and source.parent.name.startswith(".native-stage-") and not injected:
            injected = True
            raise publication_lock(winerror)
        return original_rename(source, destination)

    monkeypatch.setattr(Path, "rename", locked_once)
    monkeypatch.setattr("scripts.build_android_native.time.sleep", waits.append)
    # Verify the existing first-build/rebuild manifest, stale asset and sibling
    # behavior through a refused move, rather than merely counting retries.
    test_native_bundle_rebuild_replaces_stale_assets_and_preserves_siblings(tmp_path)
    assert injected and waits[0] == 0.05


@pytest.mark.parametrize("winerror", [5, None])
def test_native_bundle_persistent_publication_refusal_restores_previous_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int | None,
) -> None:
    test_native_bundle_omits_python_and_preserves_input_files(tmp_path)
    root, output = tmp_path / "repo", tmp_path / "output"
    destination = output / "assets/native"
    prior = {path.relative_to(destination).as_posix(): path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    sibling = output / "assets/retained.txt"
    sibling.write_bytes(b"unrelated output")
    (root / "static/ui/index.html").write_text("<html>replacement</html>", encoding="utf-8")
    original_rename = Path.rename
    refused = 0
    waits: list[float] = []

    def deny_publication(source: Path, target: Path | str) -> Path:
        nonlocal refused
        if source.name == "native" and source.parent.name.startswith(".native-stage-"):
            refused += 1
            raise publication_lock(winerror)
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", deny_publication)
    monkeypatch.setattr("scripts.build_android_native.time.sleep", waits.append)
    with pytest.raises(PermissionError):
        stage_bundle(root, output, {"arm64-v8a": {"rust": tmp_path / "rust", "go": tmp_path / "go", "worker": tmp_path / "rust", "launcher": tmp_path / "go"}})
    assert refused == (4 if winerror is not None else 1)
    assert len(waits) == (3 if winerror is not None else 0) and sum(waits) <= 0.301
    assert {path.relative_to(destination).as_posix(): path.read_bytes() for path in destination.rglob("*") if path.is_file()} == prior
    assert sibling.read_bytes() == b"unrelated output"
    assert not list(output.glob(".native-stage-*"))


def test_native_bundle_failed_restore_keeps_previous_assets_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_native_bundle_omits_python_and_preserves_input_files(tmp_path)
    root, output = tmp_path / "repo", tmp_path / "output"
    destination = output / "assets/native"
    prior = {path.relative_to(destination).as_posix(): path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    sibling = output / "assets/retained.txt"
    sibling.write_bytes(b"unrelated output")
    original_rename = Path.rename

    def deny_publish_and_restore(source: Path, target: Path | str) -> Path:
        if source.parent.name.startswith(".native-stage-") and source.name in {"native", "previous"}:
            raise publication_lock(5)
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", deny_publish_and_restore)
    monkeypatch.setattr("scripts.build_android_native.time.sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="prior assets remain for recovery"):
        stage_bundle(root, output, {"arm64-v8a": {"rust": tmp_path / "rust", "go": tmp_path / "go", "worker": tmp_path / "rust", "launcher": tmp_path / "go"}})
    retired = list(output.glob(".native-stage-*/previous"))
    assert len(retired) == 1 and not destination.exists()
    assert {path.relative_to(retired[0]).as_posix(): path.read_bytes() for path in retired[0].rglob("*") if path.is_file()} == prior
    assert sibling.read_bytes() == b"unrelated output"


@pytest.mark.parametrize("winerror", [5, 32, None])
def test_native_bundle_refused_retirement_does_not_leak_staged_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int | None,
) -> None:
    test_native_bundle_omits_python_and_preserves_input_files(tmp_path)
    root, output = tmp_path / "repo", tmp_path / "output"
    destination = output / "assets/native"
    prior = {path.relative_to(destination).as_posix(): path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    sibling = output / "assets/retained.txt"
    sibling.write_bytes(b"unrelated output")
    (root / "static/ui/index.html").write_text("<html>replacement</html>", encoding="utf-8")
    original_rename = Path.rename
    refused = 0
    waits: list[float] = []

    def deny_retirement(source: Path, target: Path | str) -> Path:
        nonlocal refused
        if source == destination:
            refused += 1
            raise publication_lock(winerror)
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", deny_retirement)
    monkeypatch.setattr("scripts.build_android_native.time.sleep", waits.append)
    with pytest.raises(PermissionError):
        stage_bundle(root, output, {"arm64-v8a": {"rust": tmp_path / "rust", "go": tmp_path / "go", "worker": tmp_path / "rust", "launcher": tmp_path / "go"}})
    assert refused == (4 if winerror is not None else 1)
    assert len(waits) == (3 if winerror is not None else 0) and sum(waits) <= 0.301
    assert {path.relative_to(destination).as_posix(): path.read_bytes() for path in destination.rglob("*") if path.is_file()} == prior
    assert sibling.read_bytes() == b"unrelated output"
    assert not list(output.glob(".native-stage-*"))


def test_native_source_identity_rejects_a_parent_repository(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Native fixture", "-c", "user.email=fixture@localhost",
                    "commit", "--quiet", "--allow-empty", "-m", "Fixture"], check=True)
    copied_source = tmp_path / "copied-source"
    copied_source.mkdir()
    (copied_source / "VERSION").write_text(f"{VERSION}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Git working tree root"):
        source_identity(copied_source)


def test_native_source_identity_binds_runtime_contracts_and_evaluation_inputs(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    fixtures = {
        "VERSION": f"{VERSION}\n", "rust/src.rs": "fn main() {}\n",
        "release/native_runtime_ownership_v1.json": "{}\n", "compat/frozen.json": "{}\n",
        "evals/golden/skills/skill_eval_cases.jsonl": '{"caseId":"one","skillId":"tutor"}\n',
    }
    for name, data in fixtures.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--all"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Native fixture", "-c", "user.email=fixture@localhost",
                    "commit", "--quiet", "-m", "Fixture"], check=True)
    before = source_identity(tmp_path)
    assert before["sourceFiles"] == len(fixtures)
    assert before["dirty"] is False and before["releaseQualified"] is False
    for name in ("release/native_runtime_ownership_v1.json", "compat/frozen.json", "evals/golden/skills/skill_eval_cases.jsonl"):
        path = tmp_path / name
        old = path.read_bytes()
        path.write_bytes(old + b"\n")
        after = source_identity(tmp_path)
        assert after["commit"] == before["commit"] and after["dirty"] is True
        assert after["sourceSha256"] != before["sourceSha256"]
        path.write_bytes(old)


@pytest.mark.parametrize("missing", ["rust", "go", "worker", "launcher"])
def test_native_bundle_refuses_an_incomplete_managed_process_tree(tmp_path: Path, missing: str) -> None:
    root, output = managed_bundle_fixture(tmp_path)
    binaries = {role: elf(tmp_path / role) for role in ("rust", "go", "worker", "launcher")}
    del binaries[missing]
    with pytest.raises(ValueError, match="complete native process tree"):
        stage_bundle(root, output, {"arm64-v8a": binaries})
    assert not output.exists(), "incomplete process tree published staged output"


def managed_bundle_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root, output = tmp_path / "repo", tmp_path / "output"
    (root / "static/ui").mkdir(parents=True)
    (root / "static/ui/index.html").write_text("<html>native</html>", encoding="utf-8")
    (root / "skills").mkdir()
    golden = root / "evals/golden/skills/skill_eval_cases.jsonl"
    golden.parent.mkdir(parents=True)
    golden.write_text('{"caseId":"native-managed"}\n', encoding="utf-8")
    (root / "VERSION").write_text(f"{VERSION}\n", encoding="utf-8")
    return root, output


def test_native_bundle_packages_every_managed_role_with_its_original_bytes(tmp_path: Path) -> None:
    root, output = managed_bundle_fixture(tmp_path)
    binaries = {role: elf(tmp_path / role) for role in ("rust", "go", "worker", "launcher")}
    for role, path in binaries.items():
        path.write_bytes(path.read_bytes() + role.encode())
    manifest = stage_bundle(root, output, {"arm64-v8a": binaries})
    assert set(manifest["binaries"]["arm64-v8a"]) == set(binaries)
    filenames = {"rust": "gateway", "go": "control", "worker": "worker", "launcher": "launch"}
    for role, label in filenames.items():
        assert (output / f"jniLibs/arm64-v8a/libdeepseek_{label}.so").read_bytes() == binaries[role].read_bytes()


def test_native_bundle_checks_worker_loader_before_publishing_any_role(tmp_path: Path) -> None:
    root, output = managed_bundle_fixture(tmp_path)
    binaries = {role: elf(tmp_path / role) for role in ("rust", "go", "worker", "launcher")}
    elf(binaries["worker"], interpreter="/lib64/ld-linux-x86-64.so.2")
    with pytest.raises(ValueError, match="Android PIE loader"):
        stage_bundle(root, output, {"arm64-v8a": binaries})
    assert not output.exists(), "unloadable worker partially published other roles"


def managed_apk_entries(tmp_path: Path) -> dict[str, bytes]:
    root, output = managed_bundle_fixture(tmp_path)
    roles = ("rust", "go", "worker", "launcher")
    binaries = {abi: {role: elf(tmp_path / f"{abi}-{role}", machine=machine) for role in roles}
                for abi, machine in (("arm64-v8a", 183), ("x86_64", 62))}
    manifest = stage_bundle(root, output, binaries)
    manifest.update(toolchains={}, sourceIdentity={"commit": "a" * 40, "sourceSha256": "b" * 64})
    entries = {}
    for path in output.rglob("*"):
        if path.is_file():
            relative = path.relative_to(output).as_posix()
            entries[relative.replace("jniLibs/", "lib/", 1)] = path.read_bytes()
    entries["assets/native/bundle.json"] = json.dumps(manifest).encode()
    return entries


def write_apk_fixture(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def test_built_apk_inspection_requires_all_four_original_managed_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    inspector = importlib.import_module("check_native_android_package")
    entries = managed_apk_entries(tmp_path)
    apk = tmp_path / "managed.apk"
    write_apk_fixture(apk, entries)
    result = inspector.inspect_package(apk)
    for abi in ("arm64-v8a", "x86_64"):
        assert set(result["binaries"][abi]) == {"rust", "go", "worker", "launcher"}
    assert not result["releaseQualified"] and not result["deviceExecutionVerified"]


@pytest.mark.parametrize("fault", ["missing-role", "changed-worker-bytes", "foreign-worker-loader"])
def test_built_apk_inspection_refuses_a_missing_or_changed_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    inspector = importlib.import_module("check_native_android_package")
    entries = managed_apk_entries(tmp_path)
    manifest = json.loads(entries["assets/native/bundle.json"])
    key = "lib/arm64-v8a/libdeepseek_worker.so"
    if fault == "missing-role":
        del manifest["binaries"]["arm64-v8a"]["worker"]
    elif fault == "changed-worker-bytes":
        entries[key] += b"changed after manifest signing"
    else:
        path = elf(tmp_path / "foreign-worker", interpreter="/lib64/ld-linux-x86-64.so.2")
        entries[key] = path.read_bytes()
        manifest["binaries"]["arm64-v8a"]["worker"]["sha256"] = hashlib.sha256(entries[key]).hexdigest()
    entries["assets/native/bundle.json"] = json.dumps(manifest).encode()
    apk = tmp_path / "refused.apk"
    write_apk_fixture(apk, entries)
    with pytest.raises(ValueError):
        inspector.inspect_package(apk)
