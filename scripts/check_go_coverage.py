#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=str(cwd), check=False, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return result


_GENERATED_GO_MARKER = re.compile(r"^// Code generated .* DO NOT EDIT\.$", re.MULTILINE)


def is_generated_only_package(package_dir: Path, go_files: list[str]) -> bool:
    """Return true only when every build input is machine-generated Go source."""

    if not go_files:
        return False
    for filename in go_files:
        if not filename.endswith(".pb.go"):
            return False
        path = package_dir / filename
        try:
            prefix = path.read_text(encoding="utf-8")[:4096]
        except (OSError, UnicodeDecodeError):
            return False
        if _GENERATED_GO_MARKER.search(prefix) is None:
            return False
    return True


def collect_profiles(go_dir: Path, dest: Path) -> None:
    template = '{{.ImportPath}}\t{{.Dir}}\t{{join .GoFiles ","}}'
    listed = _run(["go", "list", "-f", template, "./internal/...", "./pkg/..."], go_dir).stdout.splitlines()
    packages: list[str] = []
    for row in listed:
        try:
            import_path, directory, filenames = row.split("\t", 2)
        except ValueError as exc:
            raise RuntimeError(f"unexpected go list output: {row!r}") from exc
        go_files = [filename for filename in filenames.split(",") if filename]
        if not is_generated_only_package(Path(directory), go_files):
            packages.append(import_path)
    if not packages:
        raise RuntimeError("go coverage package inventory is empty after generated-source filtering")
    parts: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for index, pkg in enumerate(packages):
            pkg_profile = tmp_path / f"{index}.out"
            _run(
                ["go", "test", pkg, "-count=1", "-covermode=set", f"-coverprofile={pkg_profile}"],
                go_dir,
            )
            if pkg_profile.is_file() and pkg_profile.stat().st_size:
                parts.append(pkg_profile.read_text(encoding="utf-8"))
        blocks: dict[str, tuple[str, str]] = {}
        for text in parts:
            for line in text.splitlines():
                if not line or line.startswith("mode:"):
                    continue
                loc, rest = line.split(" ", 1)
                nstmts, count = rest.split()
                prev = blocks.get(loc)
                if prev is None or int(count) > int(prev[1]):
                    blocks[loc] = (nstmts, count)
        lines = ["mode: set"]
        for loc in sorted(blocks):
            nstmts, count = blocks[loc]
            lines.append(f"{loc} {nstmts} {count}")
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def profile_statement_counts(profile: Path) -> tuple[int, int]:
    """Count covered/total statements without rounding Go's display percentage.

    A block repeated by merged runs counts once and is covered if any run hit it.
    The Go tool still validates the source locations before this is used by the gate.
    """
    lines = profile.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] not in {"mode: set", "mode: count", "mode: atomic"}:
        raise ValueError("invalid Go coverage profile mode")
    blocks: dict[str, tuple[int, bool]] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        location, statement_count, hit_count = line.rsplit(None, 2)
        statements, hits = int(statement_count), int(hit_count)
        if statements < 0 or hits < 0:
            raise ValueError("negative Go coverage count")
        previous = blocks.get(location)
        if previous is not None and previous[0] != statements:
            raise ValueError(f"inconsistent statement count at {location}")
        blocks[location] = (statements, hits > 0 or (previous is not None and previous[1]))
    total = sum(statements for statements, _ in blocks.values())
    if total == 0:
        raise ValueError("Go coverage profile contains no statements")
    covered = sum(statements for statements, hit in blocks.values() if hit)
    return covered, total


def coverage_percent(profile: Path, cwd: Path | None = None) -> float:
    result = subprocess.run(
        ["go", "tool", "cover", f"-func={profile}"],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    print(result.stdout)
    covered, total = profile_statement_counts(profile)
    percent = 100.0 * covered / total
    print(f"Exact Go statement coverage: {percent:.6f}% ({covered}/{total})")
    return percent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail unless Go coverage meets the minimum.")
    parser.add_argument("--profile")
    parser.add_argument("--dir")
    parser.add_argument("--min", type=float, default=95.0)
    args = parser.parse_args(argv)
    profile = Path(args.profile) if args.profile else Path(tempfile.mkdtemp()) / "coverage.out"
    if args.dir:
        collect_profiles(Path(args.dir), profile)
    if not profile.is_file():
        print(f"missing coverage profile: {profile}", file=sys.stderr)
        return 1
    percent = coverage_percent(profile, Path(args.dir) if args.dir else None)
    if percent + 1e-9 < args.min:
        print(f"Go coverage {percent:.6f}% is below {args.min:.1f}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
