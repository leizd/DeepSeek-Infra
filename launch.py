"""Double-clickable entry point.

Without arguments this opens the local desktop app window. ``--gui`` keeps the
legacy launcher available, and ``--server`` lets the bundled PyInstaller exe
run the HTTP server internally without opening another window.
"""

from __future__ import annotations

import sys


def _without_launcher_flags(args: list[str], *flags: str) -> list[str]:
    return [arg for arg in args if arg not in set(flags)]


def main() -> None:
    args = sys.argv[1:]
    if "--server" in args:
        from deepseek_infra.app import main as run_server

        run_server()
        return

    if "--mobile" in args:
        from deepseek_infra.launcher.mobile import main as run_mobile

        raise SystemExit(run_mobile(_without_launcher_flags(args, "--mobile", "--gui")))

    if "--gui" in args:
        from deepseek_infra.launcher.gui import main as run_gui

        sys.argv = [sys.argv[0], *_without_launcher_flags(args, "--gui", "--app")]
        run_gui()
        return

    if "--app" not in args:
        from deepseek_infra.launcher.mobile import is_mobile_environment

        if is_mobile_environment():
            from deepseek_infra.launcher.mobile import main as run_mobile

            raise SystemExit(run_mobile(_without_launcher_flags(args, "--app")))

    for idx, arg in enumerate(args):
        if arg == "--edge" and idx + 1 < len(args):
            import os

            os.environ["DEEPSEEK_EDGE_URL"] = args[idx + 1]
            break
        if arg.startswith("--edge="):
            import os

            os.environ["DEEPSEEK_EDGE_URL"] = arg.split("=", 1)[1]
            break

    from deepseek_infra.desktop_app import main as run_desktop_app

    raise SystemExit(run_desktop_app())


if __name__ == "__main__":
    main()
