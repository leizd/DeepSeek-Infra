"""The mobile platform hook cannot become a general process execution bridge."""
from pathlib import Path

import pytest

from scripts.check_zero_python_runtime import check_process_tree_isolation

SOURCE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("damage", [None, "helper", "guard", "extra-command", "arguments", "environment", "other-package"])
def test_native_browser_hook_is_bounded(tmp_path: Path, damage: str | None) -> None:
    directory = tmp_path / "go/internal/desktop"
    directory.mkdir(parents=True)
    (directory / "browser.go").write_bytes((SOURCE / "go/internal/desktop/browser.go").read_bytes())
    text = (SOURCE / "go/internal/desktop/browser_termux.go").read_text(encoding="utf-8")
    if damage == "helper":
        text = text.replace('exec.LookPath("termux-open-url")', 'exec.LookPath("python")')
    elif damage == "guard":
        text = text.replace("if err := validateBrowserURL(raw); err != nil", "if false")
    elif damage == "extra-command":
        text += '\nfunc unsafeRun() { exec.Command("python") }\n'
    elif damage == "arguments":
        text = text.replace("exec.CommandContext(ctx, path, raw)", 'exec.CommandContext(ctx, path, "-c", raw)')
    elif damage == "environment":
        text = text.replace('"TERMUX_VERSION",', '"AUTH_TOKEN",')
    path = directory / "browser_termux.go"
    if damage == "other-package":
        path = tmp_path / "go/internal/control/browser_termux.go"
        path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    result = check_process_tree_isolation(tmp_path)
    assert result.passed is (damage is None)
    if damage is None:
        assert result.data is not None
        assert result.data["platform_browser_handlers_checked"] == 1
        assert result.data["native_supervisors_checked"] == 0
