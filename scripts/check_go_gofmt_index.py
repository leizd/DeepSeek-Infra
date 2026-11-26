#!/usr/bin/env python3
"""Fail if any tracked Go file's INDEX content is not gofmt-clean.

Why this exists
---------------
This project is developed on Windows, where the worktree holds CRLF. `gofmt -l`
on a CRLF worktree reports *every* file as unformatted, so it is useless as a
local gate, while the Linux CI runner sees LF and reports only the real defects.

The fix is to check the bytes CI will actually check out: read each file from the
git index (`git show :path`) into a scratch file with LF endings and run gofmt on
that. Scanning every tracked ``.go`` file rather than only the staged ones matters
because a defect committed earlier still fails the gate.

Both child stdout and the scratch directory stay inside the workspace: some
sandboxes deny writes to the system temp area and refuse piped child stdio, and
this check has to work in exactly those sessions.

Usage
-----
    python scripts/check_go_gofmt_index.py

Exits 0 when clean, 1 with the offending paths on stderr otherwise.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".gofmt-index-scratch"


def _capture(args: list[str], into: Path) -> str:
    """Run a command with stdout redirected to a workspace file, and return it."""
    into.parent.mkdir(parents=True, exist_ok=True)
    with into.open("w", encoding="utf-8") as sink:
        result = subprocess.run(args, cwd=ROOT, stdout=sink, stderr=subprocess.STDOUT, text=True)
    text = into.read_text(encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(args)} failed ({result.returncode}): {text.strip()}")
    return text


def tracked_go_files() -> list[str]:
    listing = _capture(["git", "ls-files", "*.go"], SCRATCH / "files.txt")
    return [row for row in listing.splitlines() if row.endswith(".go")]


def main() -> int:
    gofmt = shutil.which("gofmt")
    if gofmt is None:
        print("gofmt not found on PATH", file=sys.stderr)
        return 1

    files = tracked_go_files()
    if not files:
        print("no tracked Go files")
        return 0

    SCRATCH.mkdir(parents=True, exist_ok=True)
    bad: list[str] = []
    try:
        for name in files:
            blob = _capture(["git", "show", f":{name}"], SCRATCH / "blob.txt")
            probe = SCRATCH / Path(name).name
            # newline="\n" keeps the probe LF even on Windows, so gofmt sees the
            # same bytes the Linux runner will.
            with probe.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(blob)
            listing = _capture([gofmt, "-l", str(probe)], SCRATCH / "gofmt.txt")
            if listing.strip():
                bad.append(name)
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)

    if bad:
        print(f"not gofmt-clean in the index ({len(bad)} of {len(files)}):", file=sys.stderr)
        for name in bad:
            print(f"  {name}", file=sys.stderr)
        return 1

    print(f"{len(files)} tracked Go file(s) are gofmt-clean as indexed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
