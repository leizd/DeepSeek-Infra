"""Refuse unbounded process creation or concurrent reaping in the Android guardian."""
from pathlib import Path
import shutil

import pytest

from scripts.check_zero_python_runtime import check_process_tree_isolation

SOURCE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("damage", [None, "extra-command", "extra-spaced-command", "guard", "custody", "reaper", "wait-call", "native-names", "other-package"])
def test_android_guardian_refuses_unbounded_launch(tmp_path: Path, damage: str | None) -> None:
    destination = tmp_path / "go/internal/launch"
    destination.mkdir(parents=True)
    for name in ["plan.go", "process_fence_linux.go", "guardian_android_compat.go"]:
        shutil.copyfile(SOURCE / "go/internal/launch" / name, destination / name)
    guardian = destination / "guardian_android_compat.go"
    text = guardian.read_text()
    if damage == "extra-command":
        text += '\nfunc unsafeRun() { os.StartProcess("python", nil, nil) }\n'
    elif damage == "extra-spaced-command":
        text += '\nfunc unsafeRun() { os.StartProcess ("python", nil, nil) }\n'
    elif damage == "guard":
        text = text.replace("if !guardianProcessAllowed(process)", "if false")
    elif damage == "custody":
        text = text.replace("if err != nil || !guardianChild(pid)", "if err != nil")
    elif damage == "reaper":
        text += '\nfunc unsafeReaper(p *os.Process) { p.Wait(); unix.Wait4(-1, nil, 0, nil) }\n'
    elif damage == "wait-call":
        text += '\nfunc unsafeReaper(p *os.Process) { p.Wait() }\n'
    elif damage == "native-names":
        fence = destination / "process_fence_linux.go"
        fence.write_text(fence.read_text().replace('case "deepseekd",', 'case "python",'))
    elif damage == "other-package":
        guardian = tmp_path / "go/internal/control/guardian_android_compat.go"
        guardian.parent.mkdir(parents=True)
        (destination / "guardian_android_compat.go").unlink()
    guardian.write_text(text)
    result = check_process_tree_isolation(tmp_path)
    assert result.passed is (damage is None)
    if damage is None:
        assert result.data is not None
        assert result.data["native_supervisors_checked"] == 2
