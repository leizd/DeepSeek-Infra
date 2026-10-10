"""Offline package checks; actual app execution is covered by Android instrumentation."""

from __future__ import annotations

import struct
import subprocess
from pathlib import Path

import pytest

from scripts.build_android_native import inspect_executable, source_identity, stage_bundle


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
    (root / "VERSION").write_text("4.8.0\n", encoding="utf-8")
    rust = elf(tmp_path / "rust")
    go = elf(tmp_path / "go")
    manifest = stage_bundle(root, output, {"arm64-v8a": {"rust": rust, "go": go}})
    assert manifest["version"] == "4.8.0"
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
    (root / "VERSION").write_text("4.8.0\n", encoding="utf-8")
    rust, go = elf(tmp_path / "rust"), elf(tmp_path / "go")
    with pytest.raises(ValueError, match="evaluation corpus"):
        stage_bundle(root, output, {"arm64-v8a": {"rust": rust, "go": go}})
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
    (root / "VERSION").write_text("4.8.0\n", encoding="utf-8")
    rust, go = elf(tmp_path / "rust"), elf(tmp_path / "go")
    binaries = {"arm64-v8a": {"rust": rust, "go": go}}
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


def test_native_source_identity_rejects_a_parent_repository(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Native fixture", "-c", "user.email=fixture@localhost",
                    "commit", "--quiet", "--allow-empty", "-m", "Fixture"], check=True)
    copied_source = tmp_path / "copied-source"
    copied_source.mkdir()
    (copied_source / "VERSION").write_text("4.8.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Git working tree root"):
        source_identity(copied_source)


def test_native_source_identity_binds_runtime_contracts_and_evaluation_inputs(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    fixtures = {
        "VERSION": "4.8.0\n", "rust/src.rs": "fn main() {}\n",
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
