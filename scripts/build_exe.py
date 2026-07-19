"""Bundle the DeepSeek Infra launcher into a single executable.

Usage:

    python -m pip install -r requirements.txt
    python -m pip install -r requirements-build.txt
    python scripts/build_exe.py

The resulting ``dist/DeepSeekInfra.exe`` (or ``DeepSeekInfra`` on macOS /
Linux) bundles ``launch.py``, the entire ``deepseek_infra`` package, the
``static`` web assets, and KaTeX fonts. The same exe opens the local desktop
app window by default, can launch the legacy GUI with ``--gui``, or run as the
HTTP server with ``--server``.

(The legacy name ``DeepSeekMobile.exe`` is kept as an alias for backward
compatibility; pass ``--name DeepSeekMobile`` to produce it explicitly.)
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENTRY = PROJECT_ROOT / "launch.py"
APP_NAME = "DeepSeekInfra"
LEGACY_NAME = "DeepSeekMobile"
STATIC_DIR = PROJECT_ROOT / "static"
ICON_PATH = PROJECT_ROOT / "static" / "icons" / "favicon.ico"
FRONTEND_BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_frontend.py"


def validate_multipart_build_environment() -> None:
    """Reject the known ``multipart`` / ``python-multipart`` namespace collision."""
    try:
        importlib.metadata.distribution("python-multipart")
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise RuntimeError(
            "python-multipart conflicts with the required multipart package. "
            "Uninstall python-multipart and reinstall requirements.txt before building."
        )

    import multipart

    module_path = Path(str(getattr(multipart, "__file__", ""))).resolve()
    if module_path.name.lower() != "multipart.py":
        raise RuntimeError(f"expected the multipart>=1.3 module, found incompatible import at {module_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--onefile",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Bundle into a single file (default). Use --no-onefile for a folder build.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Pass --clean to PyInstaller and remove build/ first.",
    )
    parser.add_argument(
        "--name",
        default=APP_NAME,
        help="Name of the resulting executable (default: %(default)s).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "PyInstaller is not installed. Run\n"
            "    python -m pip install -r requirements-build.txt\n"
            "then re-run this script.",
            file=sys.stderr,
        )
        return 1

    try:
        validate_multipart_build_environment()
    except RuntimeError as exc:
        print(f"Cannot build executable: {exc}", file=sys.stderr)
        return 1

    if args.clean:
        for sub in ("build", "dist"):
            path = PROJECT_ROOT / sub
            if path.exists():
                shutil.rmtree(path)
        spec = PROJECT_ROOT / f"{args.name}.spec"
        if spec.exists():
            spec.unlink()

    frontend_result = subprocess.call([sys.executable, str(FRONTEND_BUILD_SCRIPT)], cwd=str(PROJECT_ROOT))
    if frontend_result != 0:
        return frontend_result
    if not (STATIC_DIR / "ui" / "index.html").is_file():
        print("Cannot build executable: static/ui/index.html is missing", file=sys.stderr)
        return 1

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--windowed",
        "--name",
        args.name,
        f"--add-data={STATIC_DIR}{os.pathsep}static",
        "--collect-data=customtkinter",
        "--collect-all=webview",
        "--collect-all=pythonnet",
        "--collect-all=clr_loader",
        "--hidden-import=multipart",
    ]
    if args.onefile:
        cmd.append("--onefile")
    if args.clean:
        cmd.append("--clean")
    if ICON_PATH.exists():
        cmd.extend(["--icon", str(ICON_PATH)])
    cmd.append(str(ENTRY))

    print("Running:", " ".join(cmd))
    result = subprocess.call(cmd, cwd=str(PROJECT_ROOT))

    # Create legacy-name alias for backward compatibility if default name used
    if result == 0 and (not args.name or args.name == APP_NAME):
        for ext in ("", ".exe"):
            new_path = (PROJECT_ROOT / "dist" / (APP_NAME + ext))
            legacy_path = (PROJECT_ROOT / "dist" / (LEGACY_NAME + ext))
            if new_path.exists() and not legacy_path.exists():
                if new_path.is_dir():
                    shutil.copytree(new_path, legacy_path)
                else:
                    shutil.copy2(new_path, legacy_path)
                print(f"Legacy alias created: {legacy_path}")

    return result


if __name__ == "__main__":
    raise SystemExit(main())
