"""Cloud-only: compile an offline producer against the exact retained Go v21 source."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

LEGACY_SOURCE = "ab09a93b6584d4ff50a1cc351ddf4a5c65e59302"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("native fixture compilation is cloud-only")
    root = Path(__file__).resolve().parents[1]
    current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if current != os.environ.get("GITHUB_SHA"):
        parser.error("the requested current source is not checked out")
    report = args.report.resolve()
    if not report.is_relative_to(root / "artifacts"):
        parser.error("only task reports may be returned")
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve()
    work = temporary / "native-v21-target-producer"
    if work.exists():
        parser.error("the fixture build directory already exists; do not reuse another build")
    work.mkdir(mode=0o700)
    subprocess.run(["git", "fetch", "--depth=1", "--no-write-fetch-head", "--no-tags", "--no-auto-maintenance", "origin", LEGACY_SOURCE], cwd=root, check=True, timeout=180)
    archive = work / "source.tar"
    subprocess.run(["git", "archive", "--format=tar", "--output=" + str(archive), LEGACY_SOURCE], cwd=root, check=True, timeout=120)
    snapshot = work / "source"
    snapshot.mkdir(mode=0o700)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if len(members) > 10000 or sum(m.size for m in members) > 256 * 1024 * 1024:
            raise ValueError("unexpected retained source archive size")
        for member in members:
            if not (member.isfile() or member.isdir()) or not (snapshot / member.name).resolve().is_relative_to(snapshot):
                raise ValueError("unsafe source archive member")
        for member in members:
            target = snapshot / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            body = source.extractfile(member)
            if body is None:
                raise ValueError("retained source file has no body")
            with body, target.open("xb") as output:
                shutil.copyfileobj(body, output)
    archive.unlink()
    entries = subprocess.check_output(["git", "ls-tree", "-rz", LEGACY_SOURCE], cwd=root).split(b"\0")
    verified = 0
    production = {}
    for entry in entries:
        if not entry:
            continue
        header, name_bytes = entry.split(b"\t", 1)
        mode, kind, digest = header.split()
        relative = name_bytes.decode("utf-8")
        path = snapshot / relative
        if kind != b"blob" or mode not in (b"100644", b"100755") or not path.is_file():
            raise ValueError("unexpected retained source object")
        data = path.read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if actual != digest.decode():
            raise ValueError("retained source bytes differ from the pinned Git object")
        verified += 1
        if relative.startswith("go/") and relative.endswith(".go") and not relative.endswith("_test.go"):
            production[relative] = hashlib.sha256(data).hexdigest()
    driver = root / "tests/native/native_v21_target_producer.go.txt"
    destination = snapshot / "go/internal/store/native_v21_fixture_producer_test.go"
    if destination.exists():
        raise ValueError("driver would replace retained source")
    destination.write_bytes(driver.read_bytes())
    binary = destination.parent / "native-v21-target-producer.test"
    environment = dict(os.environ, GOTOOLCHAIN="local", CGO_ENABLED="1")
    version = subprocess.check_output(["go", "env", "GOVERSION"], env=environment, text=True).strip()
    if version != "go1.27.1":
        raise ValueError("fixture uses the same checksum-pinned Go")
    subprocess.run(["go", "test", "-c", "-race", "-o", str(binary), "./internal/store"], cwd=snapshot / "go", env=environment, check=True, timeout=600)
    for relative_name, expected in production.items():
        if hashlib.sha256((snapshot / relative_name).read_bytes()).hexdigest() != expected:
            raise ValueError("fixture compilation modified retained production source")
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as stream:
        stream.write("DEEPSEEK_TEST_LEGACY_V21_BINARY=" + str(binary) + "\n")
        stream.write("DEEPSEEK_TEST_V21_SOURCE_COMMIT=" + LEGACY_SOURCE + "\n")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(dict(status="PASS_EXACT_SOURCE_OFFLINE_LEGACY_PRODUCER_BUILD", currentSource=current,
        legacySource=LEGACY_SOURCE, sourceFilesVerified=verified, productionGoFilesVerified=len(production),
        driverSha256=hashlib.sha256(driver.read_bytes()).hexdigest(), go=version, schemaVersion=21,
        productionSourceChanged=False, fixtureSignaturesOnly=True, productionPromotionQualified=False,
        compilerCachesOrBinariesReturned=False, fullMigrationQualified=False), indent=2) + "\n", encoding="utf-8")
    print(json.dumps(dict(status="LEGACY_V21_PRODUCER_BUILT_IN_CLOUD", legacySource=LEGACY_SOURCE,
        sourceFilesVerified=verified, productionGoFilesVerified=len(production))), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
