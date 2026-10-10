"""Maintain bilingual entry links across human-maintained Markdown docs."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START_MARKER = "<!-- docs-language-switcher:start -->"
END_MARKER = "<!-- docs-language-switcher:end -->"

SKIP_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
    "dist",
    "node_modules",
    "output",
    "target",
}
SKIP_FILES = {Path("AGENTS.md"), Path("content_brief.md"), Path("plan.md")}
SKIP_PREFIXES = {
    ("docs", "evidence"),
    ("evals", "reports"),
    ("deepseek-infra-deck",),
}


def _is_skipped(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if relative in SKIP_FILES:
        return True
    if any(part in SKIP_DIR_NAMES or part.startswith(".tmp-") for part in relative.parts):
        return True
    return any(relative.parts[: len(prefix)] == prefix for prefix in SKIP_PREFIXES)


def iter_managed_markdown() -> list[Path]:
    """Return human-maintained Markdown files governed by the language nav."""

    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--", "*.md"],
        check=True,
        capture_output=True,
    )
    paths = [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
    return sorted(path for path in paths if path.is_file() and not _is_skipped(path))


def _relative_link(from_path: Path, target: Path) -> str:
    return os.path.relpath(target, from_path.parent).replace(os.sep, "/")


def language_targets(path: Path) -> tuple[Path, Path]:
    """Return the Chinese and English landing targets for a document."""

    relative = path.relative_to(ROOT)
    if relative.name in {"ROADMAP.md", "ROADMAP.en.md"} and len(relative.parts) == 1:
        return ROOT / "ROADMAP.md", ROOT / "ROADMAP.en.md"
    return ROOT / "README.md", ROOT / "README.en.md"


def language_switcher(path: Path) -> str:
    chinese, english = language_targets(path)
    return "\n".join(
        [
            START_MARKER,
            f"[中文]({_relative_link(path, chinese)}) / [English]({_relative_link(path, english)})",
            END_MARKER,
        ]
    )


def _replace_or_insert_switcher(text: str, switcher: str) -> str:
    pattern = re.compile(rf"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}", re.DOTALL)
    if pattern.search(text):
        return pattern.sub(switcher, text, count=1)

    lines = text.splitlines()
    heading_index = next((index for index, line in enumerate(lines) if line.startswith("# ")), None)
    insert_at = heading_index + 1 if heading_index is not None else 0
    lines[insert_at:insert_at] = ["", switcher, ""]
    return "\n".join(lines).rstrip() + "\n"


def extract_readme_roadmap() -> bool:
    """Move the legacy inline README roadmap into the standalone ROADMAP.md."""

    readme_path = ROOT / "README.md"
    roadmap_path = ROOT / "ROADMAP.md"
    readme = readme_path.read_text(encoding="utf-8")
    start = readme.find("\n## Roadmap\n")
    end = readme.find("\n## 文档\n", start + 1)
    if start < 0 or end < 0:
        raise RuntimeError("README Roadmap or 文档 boundary was not found")

    section = readme[start + len("\n## Roadmap\n") : end].strip()
    if "### " not in section:
        if not roadmap_path.exists():
            raise RuntimeError("README already has a Roadmap stub but ROADMAP.md is missing")
        return False
    if roadmap_path.exists():
        raise RuntimeError("Refusing to overwrite existing ROADMAP.md")

    roadmap_path.write_text(f"# DeepSeek Infra Roadmap\n\n{section}\n", encoding="utf-8", newline="\n")
    replacement = (
        "\n## Roadmap\n\n"
        "路线图已迁移为独立文档：[ROADMAP.md](ROADMAP.md)。\n\n"
        "The roadmap now lives in a standalone document: [ROADMAP.en.md](ROADMAP.en.md).\n"
    )
    readme_path.write_text(readme[:start] + replacement + readme[end:], encoding="utf-8", newline="\n")
    return True


def update_all() -> list[Path]:
    changed: list[Path] = []
    for path in iter_managed_markdown():
        original = path.read_text(encoding="utf-8")
        updated = _replace_or_insert_switcher(original, language_switcher(path))
        if updated != original:
            path.write_text(updated, encoding="utf-8", newline="\n")
            changed.append(path)
    return changed


def check_all() -> list[Path]:
    invalid: list[Path] = []
    for path in iter_managed_markdown():
        text = path.read_text(encoding="utf-8")
        if text.count(START_MARKER) != 1 or text.count(END_MARKER) != 1:
            invalid.append(path)
            continue
        if language_switcher(path) not in text:
            invalid.append(path)
    return invalid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail when a managed Markdown file has stale or missing language links.")
    parser.add_argument("--extract-roadmap", action="store_true", help="Move the inline README Roadmap into ROADMAP.md before updating links.")
    args = parser.parse_args()

    if args.extract_roadmap:
        extract_readme_roadmap()
    if args.check:
        invalid = check_all()
        if invalid:
            for path in invalid:
                print(path.relative_to(ROOT).as_posix())
            return 1
        print(f"Markdown language navigation: PASS ({len(iter_managed_markdown())} files)")
        return 0

    changed = update_all()
    print(f"Updated language navigation in {len(changed)} Markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
