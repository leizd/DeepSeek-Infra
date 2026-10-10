"""Check that relative markdown links in README.md and docs/ point to existing files.

Offline CI job — no HTTP requests, repo-root-relative path resolution only.
Exits with non-zero if any broken relative links are found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC_GLOB = list(ROOT.glob("docs/*.md")) + [ROOT / "README.md"]
# Relative links: [text](path) or [text](path#anchor) or <path>
LINK_PAT = re.compile(r"\]\(([^)]+)\)")


def link_target(start_dir: Path, raw: str) -> Path | None:
    """Resolve a markdown link target that is relative to the file it appears in."""
    # Strip anchor
    if "#" in raw:
        raw = raw[: raw.index("#")]
    if not raw:
        return None  # pure anchor
    # Absolute URL
    if raw.startswith(("http://", "https://")):
        return None
    # Resolve relative to the file's directory
    candidate = (start_dir / raw).resolve()
    if candidate.exists():
        return candidate
    return None


def check_files() -> list[str]:
    errors: list[str] = []
    for doc in DOC_GLOB:
        directory = doc.parent
        for m in LINK_PAT.finditer(doc.read_text(encoding="utf-8")):
            raw = m.group(1)
            target = link_target(directory, raw)
            if target is None:
                continue  # external URL, anchor-only, or not a file
            if not target.exists():
                errors.append(f"{doc.relative_to(ROOT)}: broken link → {raw}  (resolved: {target})")
    return errors


def main() -> None:
    errors = check_files()
    if errors:
        print(f"Broken doc links ({len(errors)}):")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    print("All doc links OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
