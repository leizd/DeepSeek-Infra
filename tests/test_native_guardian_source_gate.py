"""Adversarial checks for the bounded Linux native process guardian."""
from pathlib import Path
import shutil

import pytest

from scripts.check_zero_python_runtime import check_process_tree_isolation

SOURCE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("damage", [None, "extra-command", "guard", "native-names", "custody", "other-package"])
def test_linux_guardian_refuses_unbounded_launch(tmp_path: Path, damage: str | None) -> None:
    destination = tmp_path / "go/internal/launch"
    destination.mkdir(parents=True)
    for name in ["plan.go", "process_fence_linux.go", "guardian_linux.go"]:
        shutil.copyfile(SOURCE / "go/internal/launch" / name, destination / name)
    guardian = destination / "guardian_linux.go"
    text = guardian.read_text()
    if damage == "extra-command":
        text += '\nfunc unsafeRun() { exec.Command("python") }\n'
    elif damage == "guard":
        text = text.replace("if !guardianProcessAllowed(process)", "if false")
    elif damage == "native-names":
        fence = destination / "process_fence_linux.go"
        fence.write_text(fence.read_text().replace('case "deepseekd",', 'case "python",'))
    elif damage == "custody":
        text = text.replace("if guardianChild(pid)", "if true")
    elif damage == "other-package":
        guardian = tmp_path / "go/internal/control/guardian_linux.go"
        guardian.parent.mkdir(parents=True)
        (destination / "guardian_linux.go").unlink()
    guardian.write_text(text)
    result = check_process_tree_isolation(tmp_path)
    assert result.passed is (damage is None)
    if damage is None:
        assert result.data is not None
        assert result.data["native_supervisors_checked"] == 2
