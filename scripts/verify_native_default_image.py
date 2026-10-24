"""Cloud-only proof of the native image's fresh profile, bundled data and restart.

Docker owns all scratch volumes. This host-side offline verifier never copies
Rust custody, uploads databases, or retries a possibly completed HTTP mutation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[1]


def command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(list(args), text=True, capture_output=True, timeout=75)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed: {result.stderr[-1200:]}")
    return result


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def post(origin: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(origin + "/api/skills", data=json.dumps(payload).encode(),
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    # Exactly one attempt: a lost mutation response is never proof of absence.
    with urlopen(request, timeout=10) as response:
        raw = response.read(2 * 1024**2 + 1)
        if response.status != 200 or len(raw) > 2 * 1024**2:
            raise RuntimeError("NATIVE_IMAGE_SKILL_RESPONSE_INVALID")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("NATIVE_IMAGE_SKILL_RESPONSE_NOT_AN_OBJECT")
    return value


def wait_ready(container: str, origin: str) -> None:
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        status = command("docker", "inspect", "--format", "{{.State.Status}}", container).stdout.strip()
        if status != "running":
            raise RuntimeError("NATIVE_IMAGE_EXITED_BEFORE_READY")
        try:
            with urlopen(origin + "/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError):
            pass
        time.sleep(0.5)
    raise RuntimeError("NATIVE_IMAGE_READINESS_TIMEOUT")


def copy_file(container: str, relative: str, target: Path, *, required: bool = True) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    result = command("docker", "cp", container + ":/data/" + relative, str(target), check=False)
    if result.returncode and required:
        raise RuntimeError("NATIVE_IMAGE_PUBLIC_STATE_COPY_FAILED: " + relative)
    return result.returncode == 0


def stopped_profile(container: str, target: Path) -> dict[str, Any]:
    command("docker", "stop", "--time", "45", container)
    database = target / "control.sqlite3"
    copy_file(container, "go-control/control.sqlite3", database)
    copy_file(container, "go-control/control.sqlite3-wal", target / "control.sqlite3-wal", required=False)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        db.execute("PRAGMA query_only=ON")
        origin, state, publication = db.execute("SELECT origin,state,publication FROM control_native_origin WHERE singleton=1").fetchone()
        owner, fence = db.execute("SELECT owner_instance_id,fencing_token FROM control_writer WHERE singleton=1").fetchone()
        domains = db.execute("SELECT owner,state FROM control_cutover").fetchall()
    if state != "published" or not origin or not publication or not owner or fence <= 0 or len(domains) != 12:
        raise RuntimeError("NATIVE_IMAGE_ORIGIN_INCOMPLETE")
    if any(owner != "go" or state != "python_disabled" for owner, state in domains):
        raise RuntimeError("NATIVE_IMAGE_DOMAIN_AUTHORITY_INCOMPLETE")
    public: dict[str, str] = {}
    for name in ["native-origin-intent.pb", "native-profile/profile-id", "native-profile/signer-public-pin"]:
        path = target / name
        copy_file(container, "go-control/" + name, path)
        value = path.read_bytes()
        if not value or len(value) > 65536:
            raise RuntimeError("NATIVE_IMAGE_PUBLIC_BINDING_INVALID")
        public[name] = digest(value)
    return dict(originSha256=digest(origin), publicationSha256=digest(publication), publicFiles=public,
                writerOwner=owner, writerFence=fence, authoritativeDomains=12)


class Scratch:
    def __init__(self, image: str, source: str, output: Path, name: str) -> None:
        self.image, self.source, self.output = image, source, output
        self.nonce = uuid.uuid4().hex
        self.name = "deepseek-image-" + name + "-" + self.nonce
        self.volume = self.name + "-data"
        self.created_volume = False

    def start(self, *, legacy_seed: bool = False, missing_bundle: bool = False) -> str:
        self.created_volume = True
        command("docker", "volume", "create", "--label", "deepseek.smoke.owner=" + self.nonce, self.volume)
        observed = json.loads(command("docker", "volume", "inspect", self.volume).stdout)[0]
        if observed["Labels"].get("deepseek.smoke.owner") != self.nonce:
            raise RuntimeError("REFUSE_FOREIGN_VOLUME_USE")
        self.token = uuid.uuid4().hex + uuid.uuid4().hex
        args = ["docker", "run", "-d", "--name", self.name, "--label", "deepseek.smoke.owner=" + self.nonce,
                "--label", "deepseek.smoke.source=" + self.source, "-v", self.volume + ":/data",
                "-p", "127.0.0.1::8000", "-e", "AUTH_TOKEN=" + self.token]
        if missing_bundle:
            args += ["-e", "DEEPSEEK_INFRA_BUNDLE_ROOT=/data"]
        if legacy_seed:
            args += ["--entrypoint", "/bin/sh"]
        args.append(self.image)
        if legacy_seed:
            args += ["-c", "mkdir -p /data/worker; exec /usr/local/bin/deepseek-infra-entrypoint"]
        command(*args)
        inspected = json.loads(command("docker", "inspect", self.name).stdout)[0]
        bindings = inspected["NetworkSettings"]["Ports"].get("8000/tcp")
        if legacy_seed and inspected["State"]["Status"] == "exited" and not bindings:
            return "http://127.0.0.1:1"  # wait_ready checks the exited state before any request.
        if not bindings or len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
            raise RuntimeError("NATIVE_IMAGE_PORT_NOT_ISOLATED")
        port = int(bindings[0]["HostPort"])
        if not 1 <= port <= 65535:
            raise RuntimeError("NATIVE_IMAGE_PORT_INVALID")
        return "http://127.0.0.1:" + str(port)

    def close(self) -> None:
        observed = command("docker", "inspect", self.name, check=False)
        if observed.returncode == 0:
            labels = json.loads(observed.stdout)[0]["Config"]["Labels"]
            if labels.get("deepseek.smoke.owner") != self.nonce or labels.get("deepseek.smoke.source") != self.source:
                raise RuntimeError("REFUSE_FOREIGN_CONTAINER_CLEANUP")
            logs = command("docker", "logs", self.name, check=False)
            (self.output / (self.name + ".log")).write_text(logs.stdout + logs.stderr, encoding="utf-8")
            command("docker", "rm", "--force", self.name)
        if self.created_volume:
            observed = json.loads(command("docker", "volume", "inspect", self.volume).stdout)[0]
            if observed["Labels"].get("deepseek.smoke.owner") != self.nonce:
                raise RuntimeError("REFUSE_FOREIGN_VOLUME_CLEANUP")
            command("docker", "volume", "rm", self.volume)


def expected_catalog() -> tuple[set[str], set[str], list[dict[str, Any]], dict[str, str]]:
    skills = {json.loads(p.read_text())["skillId"] for p in (ROOT / "skills/builtin").glob("*.json")}
    packs = {json.loads(p.read_text())["packId"] for p in (ROOT / "skills/packs").glob("*.json")}
    golden = [json.loads(line) for line in (ROOT / "evals/golden/skills/skill_eval_cases.jsonl").read_text().splitlines() if line.strip()]
    files = [*sorted((ROOT / "skills").rglob("*.json")), ROOT / "evals/golden/skills/skill_eval_cases.jsonl"]
    hashes = {"/app/bundle/" + p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in files}
    if len(skills) != 18 or len(packs) != 4 or not golden:
        raise RuntimeError("NATIVE_IMAGE_SOURCE_CATALOG_INCOMPLETE")
    return skills, packs, golden, hashes


def assert_catalog(origin: str, token: str, expected: tuple[set[str], set[str], list[dict[str, Any]], dict[str, str]]) -> None:
    skills, packs, golden, _ = expected
    actual = post(origin, token, {"action": "list", "includeDisabled": True})["skills"]
    if {s["skillId"] for s in actual if s["builtin"]} != skills:
        raise RuntimeError("NATIVE_IMAGE_BUNDLED_SKILLS_MISSING")
    actual = post(origin, token, {"action": "list_packs"})["packs"]
    if {p["packId"] for p in actual if p["builtin"]} != packs:
        raise RuntimeError("NATIVE_IMAGE_BUNDLED_PACKS_MISSING")
    cases = post(origin, token, {"action": "list_eval_cases"})["cases"]
    if not {c["caseId"] for c in golden} <= {c["caseId"] for c in cases}:
        raise RuntimeError("NATIVE_IMAGE_GOLDEN_CASES_MISSING")


def positive(image: str, source: str, output: Path, scratch: Path) -> dict[str, Any]:
    case = Scratch(image, source, output, "positive")
    expected = expected_catalog()
    try:
        origin = case.start()
        wait_ready(case.name, origin)
        command("docker", "exec", case.name, "/bin/sh", "-c",
                'for binary in python python3 node nodejs; do if command -v "$binary" >/dev/null 2>&1; then exit 1; fi; done')
        rows = command("docker", "top", case.name, "-eo", "args").stdout.splitlines()[1:]
        roles = {Path(shlex.split(row)[0]).name for row in rows if row.strip()}
        required_roles = {"deepseek-launch", "deepseekd", "deepseek-worker", "deepseek-gateway"}
        if not required_roles <= roles:
            raise RuntimeError("NATIVE_IMAGE_REQUIRED_PROCESSES_MISSING")
        with urlopen(origin + "/", timeout=5) as response:
            home = response.read(2 * 1024**2 + 1)
            if response.status != 200 or len(home) > 2 * 1024**2 or b'id="root"' not in home:
                raise RuntimeError("NATIVE_IMAGE_REACT_ENTRY_NOT_SERVED")
        scripts = re.findall(rb'<script[^>]+src="([^"]+)"', home)
        if not scripts:
            raise RuntimeError("NATIVE_IMAGE_FRONTEND_SCRIPT_MISSING")
        for script in scripts:
            path = script.decode("utf-8")
            if not path.startswith("/") or path.startswith("//"):
                raise RuntimeError("NATIVE_IMAGE_FRONTEND_ASSET_NOT_LOCAL")
            with urlopen(origin + path, timeout=5) as response:
                data = response.read(16 * 1024**2 + 1)
                if response.status != 200 or not data or len(data) > 16 * 1024**2:
                    raise RuntimeError("NATIVE_IMAGE_FRONTEND_ASSET_NOT_SERVED")
        try:
            post(origin, "", {"action": "list"})
        except HTTPError as error:
            if error.code not in {401, 403}:
                raise
        else:
            raise RuntimeError("NATIVE_IMAGE_UNAUTHENTICATED_REQUEST_ACCEPTED")
        assert_catalog(origin, case.token, expected)
        config = json.loads((ROOT / "skills/builtin/study_tutor.json").read_text())
        skill_id = "image-resource-" + uuid.uuid4().hex
        config.update(skillId=skill_id, name="Container resource retention fixture", builtin=False)
        created = post(origin, case.token, {"action": "create", "skill": config, "overwrite": False})
        if created["skill"]["skillId"] != skill_id or created["skill"]["builtin"]:
            raise RuntimeError("NATIVE_IMAGE_CUSTOM_SKILL_NOT_CREATED")
        custom_case = dict(expected[2][0], caseId="image-case-" + uuid.uuid4().hex, skillId=skill_id)
        post(origin, case.token, {"action": "create_eval_case", "case": custom_case})
        initial = stopped_profile(case.name, scratch / "first")
        custom_path = scratch / "custom.json"
        copy_file(case.name, ".skills/custom/" + skill_id + ".json", custom_path)
        custom_bytes = custom_path.read_bytes()
        if json.loads(custom_bytes)["skillId"] != skill_id:
            raise RuntimeError("NATIVE_IMAGE_CUSTOM_SKILL_NOT_IN_DATA_VOLUME")
        command("docker", "start", case.name)
        wait_ready(case.name, origin)
        assert_catalog(origin, case.token, expected)
        retained = [s for s in post(origin, case.token, {"action": "list", "includeDisabled": True})["skills"] if s["skillId"] == skill_id]
        if len(retained) != 1 or retained[0]["systemPrompt"] != config["systemPrompt"]:
            raise RuntimeError("NATIVE_IMAGE_CUSTOM_SKILL_LOST_AFTER_RESTART")
        cases = post(origin, case.token, {"action": "list_eval_cases"})["cases"]
        if not any(c["caseId"] == custom_case["caseId"] for c in cases):
            raise RuntimeError("NATIVE_IMAGE_CUSTOM_CASE_LOST_AFTER_RESTART")
        raw = command("docker", "exec", case.name, "sha256sum", *expected[3]).stdout
        actual = {line.split(None, 1)[1].strip(): line.split(None, 1)[0] for line in raw.splitlines()}
        if actual != expected[3]:
            raise RuntimeError("NATIVE_IMAGE_PACKAGED_INPUTS_CHANGED")
        restarted = stopped_profile(case.name, scratch / "second")
        for field in ["originSha256", "publicationSha256", "publicFiles"]:
            if initial[field] != restarted[field]:
                raise RuntimeError("NATIVE_IMAGE_ORIGINAL_IDENTITY_REPLACED")
        if [initial["writerFence"], restarted["writerFence"]] != [1, 2] or initial["writerOwner"] == restarted["writerOwner"]:
            raise RuntimeError("NATIVE_IMAGE_WRITER_FENCE_NOT_ADVANCED")
        return dict(status="PASS", freshVolume=True, bundledSkills=18, bundledPacks=4, goldenCases=len(expected[2]),
                    customSkillRetained=True, customCaseRetained=True, packagedInputsUnchanged=True,
                    originalSignedOriginRetained=True, writerFences=[1, 2], originSha256=initial["originSha256"],
                    retainedCustomSha256=digest(custom_bytes), requiredNativeRoles=sorted(required_roles),
                    PythonAndNodeRuntimeExecutablesAbsent=True, reactEntryAndScriptsServed=True)
    finally:
        case.close()


def negative(image: str, source: str, output: Path, scratch: Path, *, legacy: bool) -> dict[str, Any]:
    case = Scratch(image, source, output, "legacy-seed" if legacy else "missing-bundle")
    try:
        origin = case.start(legacy_seed=legacy, missing_bundle=not legacy)
        if legacy:
            try:
                wait_ready(case.name, origin)
            except RuntimeError as error:
                if str(error) != "NATIVE_IMAGE_EXITED_BEFORE_READY":
                    raise
            else:
                raise RuntimeError("NATIVE_IMAGE_FOREIGN_DIRECTORY_WAS_ADMITTED")
            logs = command("docker", "logs", case.name, check=False)
            if "native managed profile is incomplete, foreign or already active" not in logs.stdout + logs.stderr:
                raise RuntimeError("NATIVE_IMAGE_NEGATIVE_FAILED_AT_UNRELATED_STAGE")
            for relative in ["go-control/native-profile/profile-id", "go-control/control.sqlite3"]:
                if copy_file(case.name, relative, scratch / Path(relative).name, required=False):
                    raise RuntimeError("NATIVE_IMAGE_FOREIGN_ROOT_GAINED_NATIVE_AUTHORITY")
        else:
            wait_ready(case.name, origin)
            try:
                assert_catalog(origin, case.token, expected_catalog())
            except RuntimeError as error:
                if str(error) != "NATIVE_IMAGE_BUNDLED_SKILLS_MISSING":
                    raise
            else:
                raise RuntimeError("NATIVE_IMAGE_MISSING_BUNDLE_REGRESSION_NOT_DETECTED")
        return dict(status="PASS", expectedFailureDetected=True, kind="legacy-worker-directory" if legacy else "missing-bundle")
    finally:
        case.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="deepseek-infra:test")
    parser.add_argument("--report-dir", default="artifacts/native-default-image")
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.name != "posix":
        raise RuntimeError("NATIVE_IMAGE_VERIFICATION_IS_CLOUD_ONLY")
    source = command("git", "rev-parse", "HEAD").stdout.strip()
    if not re.fullmatch(r"[a-f0-9]{40}", source) or source != os.environ.get("GITHUB_SHA"):
        raise RuntimeError("NATIVE_IMAGE_SOURCE_REVISION_MISMATCH")
    inspected = json.loads(command("docker", "image", "inspect", args.image).stdout)[0]
    image = inspected["Id"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image) or inspected["Config"]["Labels"].get("org.opencontainers.image.revision") != source:
        raise RuntimeError("NATIVE_IMAGE_BUILD_REVISION_MISMATCH")
    output = Path(args.report_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = dict(sourceCommit=source, sourceTree=command("git", "rev-parse", "HEAD^{tree}").stdout.strip(),
                                  runId=os.environ.get("GITHUB_RUN_ID"), event=os.environ.get("GITHUB_EVENT_NAME"), imageId=image,
                                  sourceTreeDirty=bool(command("git", "status", "--porcelain").stdout.strip()),
                                  checkedAt=datetime.now(timezone.utc).isoformat(), cases={}, fullMigrationQualified=False,
                                  allProductFeaturesQualified=False, productionProviderQualified=False, readiness="NOT_READY")
    if report["sourceTreeDirty"]:
        raise RuntimeError("NATIVE_IMAGE_SOURCE_TREE_DIRTY")
    with tempfile.TemporaryDirectory(prefix="native-image-proof-") as temp:
        scratch = Path(temp)
        for name in ["positive", "legacy-seed", "missing-bundle"]:
            try:
                if name == "positive":
                    result = positive(image, source, output, scratch / name)
                else:
                    result = negative(image, source, output, scratch / name, legacy=name == "legacy-seed")
                report["cases"][name] = result
            except Exception as error:
                detail = str(error)
                if isinstance(error, HTTPError):
                    detail += ": " + error.read(1200).decode("utf-8", errors="replace")
                report["cases"][name] = dict(status="FAIL", error=detail[:1800])
    report["status"] = "PASS" if all(row["status"] == "PASS" for row in report["cases"].values()) else "FAIL"
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if report["status"] != "PASS":
        raise SystemExit("NATIVE_DEFAULT_IMAGE_QUALIFICATION_FAILED")


if __name__ == "__main__":
    main()
