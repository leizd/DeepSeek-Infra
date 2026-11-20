"""Execute the real workflow consumer with inert file fixtures, never native programs."""
import hashlib
from pathlib import Path
import re
import shutil
import struct
import subprocess
from typing import Any, BinaryIO

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def fixture_transfer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, dict[str, Any]]:
    import json

    workflow = yaml.safe_load((ROOT / ".github/workflows/native-media-recovery.yml").read_bytes())
    upload = next(step for step in workflow["jobs"]["media-recovery"]["steps"]
                  if step.get("with", {}).get("name") == "native-media-fixture-binaries-${{ github.sha }}")
    source = next(step["run"] for step in workflow["jobs"]["media-process-recovery"]["steps"]
                  if "shutil.copytree(root/'ui',destination)" in step.get("run", ""))
    programs = re.findall(r"python - <<'PY'\n(.*?)\nPY", source, re.DOTALL)
    assert len(programs) == 1
    staging = tmp_path / "producer"
    staging.mkdir()
    # These are inert ABI-header fixtures, not built or executed Rust binaries.
    header = bytearray(20)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, 62)
    for name in ["deepseek-worker", "deepseek-gateway"]:
        (staging / name).write_bytes(header + name.encode())
    (staging / "ui/.vite").mkdir(parents=True)
    (staging / "ui/index.html").write_text("<html>source-built UI fixture</html>", encoding="utf8")
    (staging / "ui/.vite/manifest.json").write_text('{"source":"controlled-vite-fixture"}', encoding="utf8")
    files = [{"path": path.relative_to(staging).as_posix(), "bytes": path.stat().st_size,
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(staging.rglob("*")) if path.is_file()]
    receipt = dict(sourceCommit="a" * 40, sourceTree="b" * 40, runId="42", attempt="1",
                   requiredElfDependenciesResolved=True, files=files,
                   binaries=[dict(name=name, **{key: row[key] for key in ["bytes", "sha256"]})
                             for name in ["deepseek-worker", "deepseek-gateway"] for row in files if row["path"] == name])
    (staging / "native-binaries.json").write_text(json.dumps(receipt), encoding="utf8")
    runner = tmp_path / "runner"
    target = runner / "native-media-fixture-bin"
    target.mkdir(parents=True)
    for path in staging.rglob("*"):
        relative = path.relative_to(staging)
        # actions/upload-artifact excludes hidden files by default. This models
        # its declared transfer policy before exercising the actual consumer.
        if not upload["with"].get("include-hidden-files", False) and any(part.startswith(".") for part in relative.parts):
            continue
        if path.is_file():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    work = tmp_path / "work"
    (work / "artifacts/native-media-process").mkdir(parents=True)
    monkeypatch.chdir(work)
    for key, value in {"RUNNER_TEMP": str(runner), "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1"}.items():
        monkeypatch.setenv(key, value)

    def git_tree(args: list[str], **kwargs: Any) -> str | bytes:
        assert args == ["git", "rev-parse", "HEAD^{tree}"]
        value = "b" * 40 + "\n"
        return value if kwargs.get("text") else value.encode()

    def file_digest(stream: BinaryIO, algorithm: str) -> Any:
        # The embedded workflow explicitly uses Python3.12. Keep its digest
        # operation testable under the repository's separate Python3.10 oracle.
        assert algorithm == "sha256"
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest

    monkeypatch.setattr(subprocess, "check_output", git_tree)
    monkeypatch.setattr(hashlib, "file_digest", file_digest, raising=False)
    return programs[0], target, receipt


def test_cloud_transfer_preserves_the_required_hidden_vite_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    program, _, _ = fixture_transfer(tmp_path, monkeypatch)
    exec(compile(program, "actual-workflow-consumer.py", "exec"), {})
    assert Path("static/ui/.vite/manifest.json").read_text() == '{"source":"controlled-vite-fixture"}'
    assert Path("artifacts/native-media-process/binary-context.json").is_file()


@pytest.mark.parametrize("tamper", ["missing-hidden", "extra-file", "modified-ui", "mixed-source", "duplicate-member"])
def test_fixture_consumer_still_refuses_incomplete_extra_modified_or_mixed_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str,
) -> None:
    import json

    program, target, receipt = fixture_transfer(tmp_path, monkeypatch)
    if tamper == "missing-hidden":
        (target / "ui/.vite/manifest.json").unlink(missing_ok=True)
    elif tamper == "extra-file":
        (target / "unexpected.txt").write_text("unlisted fixture", encoding="utf8")
    elif tamper == "modified-ui":
        (target / "ui/index.html").write_text("modified", encoding="utf8")
    elif tamper == "mixed-source":
        receipt["sourceCommit"] = "c" * 40
    else:
        receipt["files"].append(dict(receipt["files"][0]))
    (target / "native-binaries.json").write_text(json.dumps(receipt), encoding="utf8")
    with pytest.raises(AssertionError):
        exec(compile(program, "actual-workflow-consumer.py", "exec"), {})
    assert not Path("static/ui").exists()
