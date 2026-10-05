"""The static process gate permits only the bounded native supervisor."""

from pathlib import Path

import pytest

from scripts.check_zero_python_runtime import check_process_tree_isolation


SOURCE = Path(__file__).resolve().parents[1] / "go/internal/launch/plan.go"


@pytest.mark.parametrize("damage", [None, "extra-command", "native-names", "guard", "other-package"])
def test_native_launcher_exception_refuses_arbitrary_exec(tmp_path: Path, damage: str | None) -> None:
    source = SOURCE.read_text(encoding="utf-8")
    path = tmp_path / "go/internal/launch/plan.go"
    if damage == "extra-command":
        source += '\nfunc unsafeRun() { exec.Command("python") }\n'
    elif damage == "native-names":
        source = source.replace('names := []string{"deepseekd",', 'names := []string{"python",')
    elif damage == "guard":
        source = source.replace("if legacyCommand(process.Path)", "if false")
    elif damage == "other-package":
        path = tmp_path / "go/internal/control/plan.go"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    result = check_process_tree_isolation(tmp_path)
    assert result.passed is (damage is None)
    if damage is None:
        assert result.data is not None
        assert result.data["native_supervisors_checked"] == 1
        assert result.data["scope"] == "source_contract"
