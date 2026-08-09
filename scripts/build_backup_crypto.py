"""Build the small age-compatible backup crypto helper for packaging."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_backup_crypto(root: Path = ROOT, *, release: bool = True) -> Path:
    executable = "backup-crypto.exe" if os.name == "nt" else "backup-crypto"
    command = [
        "cargo",
        "build",
        "--locked",
        "--manifest-path",
        str(root / "rust" / "Cargo.toml"),
        "-p",
        "backup-crypto",
        "-p",
        "deepseek-backup",
    ]
    if release:
        command.append("--release")
    subprocess.run(command, cwd=root, check=True)
    source = root / "rust" / "target" / ("release" if release else "debug") / executable
    target = root / "bin" / executable
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    chunk_executable = "deepseek-backup.exe" if os.name == "nt" else "deepseek-backup"
    shutil.copy2(root / "rust" / "target" / ("release" if release else "debug") / chunk_executable, root / "bin" / chunk_executable)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    print(build_backup_crypto(release=not args.debug))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
