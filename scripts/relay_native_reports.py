"""Read existing, digest-verified native CI reports; never execute a workload."""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import re
import stat
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPO = "leizd/DeepSeek-Infra"
SOURCE = "3853c19c78e3bd01e83943f19afba60e6b8f5acd"
TREE = "73accc76195bd5481ca5d4d0d4e8064fec31ad79"
DEFAULT_SPECS = [
    dict(kind="go-media", source=SOURCE, tree=TREE, run=40858309954, event="workflow_dispatch"),
    dict(kind="windows-custody", source=SOURCE, tree=TREE, run=40858426517, event="workflow_dispatch"),
    dict(kind="rust-coverage", source=SOURCE, tree=TREE, run=40858290634, event="workflow_dispatch"),
]
KINDS = {
    "go-media": ("native-media-development.yml", "native-media-development-", ["result.json"]),
    "windows-custody": ("native-mcp-windows-custody.yml", "native-mcp-windows-custody-", ["process-custody.json"]),
    "rust-coverage": ("rust-cloud.yml", "rust-cloud-coverage-", ["source-context.json", "coverage-evidence.json", "coverage.lcov", "coverage.log"]),
}
MAX_JSON = 1024 * 1024
MAX_ENVELOPE = 128 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def checked_address(url: str) -> str:
    address = urllib.parse.urlsplit(url)
    host = address.hostname or ""
    assert address.scheme == "https" and address.username is None and address.port in (None, 443)
    assert host == "api.github.com" or host.endswith((".blob.core.windows.net", ".actions.githubusercontent.com", ".githubusercontent.com"))
    return host


def fetch(url: str, limit: int, authenticated: bool = False) -> bytes:
    opener = urllib.request.build_opener(NoRedirect())
    for hop in range(5):
        host = checked_address(url)
        headers = {"User-Agent": "DeepSeek-native-report-collector"}
        if authenticated and hop == 0:
            assert host == "api.github.com"
            headers.update(Authorization="Bearer " + os.environ["GH_TOKEN"], Accept="application/vnd.github+json")
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
                data = response.read(limit + 1)
                assert len(data) <= limit
                return data
        except urllib.error.HTTPError as error:
            assert error.code in (301, 302, 303, 307, 308) and hop < 4
            url = urllib.parse.urljoin(url, error.headers["Location"])
    raise AssertionError("report redirect bound exceeded")


def api(path: str) -> Any:
    assert not path.startswith("/") and ".." not in path and "://" not in path
    return json.loads(fetch("https://api.github.com/repos/" + REPO + "/" + path, 2 * 1024**2, True))


def validate_specs(specs: Any) -> list[dict[str, Any]]:
    assert isinstance(specs, list) and 1 <= len(specs) <= 8
    identities = set()
    for spec in specs:
        assert isinstance(spec, dict) and set(spec) == {"kind", "source", "tree", "run", "event"}
        assert spec["kind"] in KINDS and spec["event"] in {"push", "workflow_dispatch"}
        assert type(spec["run"]) is int and spec["run"] > 0
        assert re.fullmatch(r"[0-9a-f]{40}", spec["source"]) and re.fullmatch(r"[0-9a-f]{40}", spec["tree"])
        identity = (spec["kind"], spec["run"])
        assert identity not in identities
        identities.add(identity)
    return specs


def sanitize(value: Any, path: str = "", removed: list[str] | None = None) -> Any:
    removed = [] if removed is None else removed
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            child = path + "/" + key
            if key.lower() in {"argv", "args", "command", "cmdline", "commandline", "environment", "env", "token", "password", "apikey", "privatekey", "private_key", "authorization"}:
                result[key] = "[redacted diagnostic]"
                removed.append(child)
            elif key.endswith("Processes"):
                assert isinstance(item, list)
                result[key] = [{field: row[field] for field in ("executable", "pid", "startTicks")} for row in item]
                removed.append(child + "/diagnostic-fields")
            else:
                result[key] = sanitize(item, child, removed)
        return result
    if isinstance(value, list):
        return [sanitize(item, path + "/" + str(index), removed) for index, item in enumerate(value)]
    return value


def archive_members(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    assert len(infos) <= 5000 and sum(item.file_size for item in infos) <= 128 * 1024**2
    assert len({item.filename for item in infos}) == len(infos)
    for item in infos:
        assert item.orig_filename == item.filename
        name = PurePosixPath(item.filename)
        assert not name.is_absolute() and not stat.S_ISLNK(item.external_attr >> 16)
        assert all(part not in {"", ".", ".."} and ":" not in part and "\\" not in part for part in name.parts)


def project(path: str, raw: bytes) -> tuple[str, Any]:
    if path == "coverage.lcov":
        counts = {"LF": 0, "LH": 0}
        records = 0
        for line in raw.decode().splitlines():
            if line == "end_of_record":
                records += 1
            for key in counts:
                if line.startswith(key + ":"):
                    counts[key] += int(line[3:])
        assert records > 0 and 0 <= counts["LH"] <= counts["LF"] and counts["LF"] > 0
        return "LCOV_LINE_TOTALS_ONLY", dict(records=records, totalLines=counts["LF"], coveredLines=counts["LH"])
    if path == "coverage.log":
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw.decode())
        test_counts = [list(map(int, v)) for v in re.findall(r"test result: ok\. (\d+) passed; (\d+) failed; (\d+) ignored", text)]
        assert test_counts and "test result: FAILED" not in text and not any(v[1] for v in test_counts)
        ignored = [line for line in text.splitlines() if line.startswith("test ") and " ... ignored" in line]
        return "CARGO_TEST_TOTALS_ONLY", dict(cargoResults=test_counts, passed=sum(v[0] for v in test_counts),
            ignored=sum(v[2] for v in test_counts), originalIgnoreLines=ignored)
    assert path in {"result.json", "process-custody.json", "source-context.json", "coverage-evidence.json"} and len(raw) <= MAX_JSON
    return "SANITIZED_JSON", json.loads(raw.decode("utf-8-sig"))


def collect(spec: dict[str, Any]) -> dict[str, Any]:
    workflow, prefix, paths = KINDS[spec["kind"]]
    run = api("actions/runs/" + str(spec["run"]))
    assert run["head_sha"] == spec["source"] and run["path"] == ".github/workflows/" + workflow
    assert run["head_branch"].startswith("codex/")
    assert run["event"] == spec["event"] and run["run_attempt"] == 1 and run["status"] == "completed"
    if spec["kind"] == "rust-coverage":
        jobs = api("actions/runs/" + str(spec["run"]) + "/jobs?per_page=100")["jobs"]
        selected = [job for job in jobs if job["name"] == "Rust coverage"]
        assert len(selected) == 1 and selected[0]["head_sha"] == spec["source"] and selected[0]["conclusion"] == "success"
    else:
        assert run["conclusion"] == "success"
    assert api("git/commits/" + spec["source"])["tree"]["sha"] == spec["tree"]
    listed = api("actions/runs/" + str(spec["run"]) + "/artifacts?per_page=100")
    assert listed["total_count"] <= 100
    artifacts = [row for row in listed["artifacts"] if row["name"] == prefix + spec["source"]]
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert not artifact["expired"] and artifact["workflow_run"]["head_sha"] == spec["source"]
    assert artifact["workflow_run"]["id"] == spec["run"] and 0 < artifact["size_in_bytes"] <= 8 * 1024**2
    payload = fetch("https://api.github.com/repos/" + REPO + "/actions/artifacts/" + str(artifact["id"]) + "/zip", artifact["size_in_bytes"], True)
    assert len(payload) == artifact["size_in_bytes"] and "sha256:" + hashlib.sha256(payload).hexdigest() == artifact["digest"]
    records = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive_members(archive)
        for path in paths:
            info = archive.getinfo(path)
            assert 0 < info.file_size <= 16 * 1024**2
            raw = archive.read(info)
            projection, value = project(path, raw)
            if path in {"result.json", "process-custody.json"}:
                assert value["sourceCommit"] == spec["source"] and value["sourceTree"] == spec["tree"]
                assert str(value["runId"]) == str(spec["run"]) and value["event"] == spec["event"]
            elif path == "source-context.json":
                assert value["testedRevision"] == spec["source"] and value["sourceTreeDirty"] is False
                assert str(value["workflowRunId"]) == str(spec["run"]) and value["eventName"] == spec["event"]
            removed: list[str] = []
            cleaned = sanitize(value, removed=removed)
            records.append(dict(path=path, bytes=len(raw), rawSha256=hashlib.sha256(raw).hexdigest(),
                projection=projection, report=cleaned, redactedDiagnosticPaths=removed))
    if spec["kind"] == "rust-coverage":
        selected_reports = {row["path"]: row["report"] for row in records}
        assert selected_reports["coverage-evidence.json"]["sourceContext"] == selected_reports["source-context.json"]
    return dict(originSpec=spec, originRunConclusion=run["conclusion"], artifactId=artifact["id"],
        artifactName=artifact["name"], artifactDigest=artifact["digest"], originalArchiveVerifiedInCloud=True, records=records)


def notice_chunks(envelope: dict[str, Any]) -> list[str]:
    data = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    assert len(data) < MAX_ENVELOPE
    encoded = base64.b64encode(gzip.compress(data, mtime=0)).decode()
    pieces = [encoded[start:start + 3000] for start in range(0, len(encoded), 3000)]
    assert 1 <= len(pieces) <= 64
    return pieces


def main() -> int:
    assert os.environ["GITHUB_REPOSITORY"] == REPO
    encoded = os.environ.get("REPORT_SPECS", "")
    assert len(encoded.encode()) <= 16 * 1024
    specs = validate_specs(json.loads(encoded) if encoded else DEFAULT_SPECS)
    envelope = dict(schemaVersion=1, kind="native-qualified-report-transport-v1",
        carrierSourceCommit=os.environ["GITHUB_SHA"], carrierRunId=os.environ["GITHUB_RUN_ID"],
        runtimeReexecuted=False, compilerCachesOrImagesRelayed=False, qualificationClaimed=False,
        reports=[collect(spec) for spec in specs])
    pieces = notice_chunks(envelope)
    for index, piece in enumerate(pieces, 1):
        print("::notice file=.github/workflows/native-report-collector.yml,line=1,title=native-qualified-reports-v1-part-" + str(index) + "-of-" + str(len(pieces)) + "::" + piece, flush=True)
    print(json.dumps(dict(status="REPORT_TRANSPORT_ONLY", reportGroups=len(specs), chunks=len(pieces), runtimeReexecuted=False)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
