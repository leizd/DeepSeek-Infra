"""The source gate admits only the fenced, bounded Rust custody initializer."""
from pathlib import Path

import pytest

from scripts.check_zero_python_runtime import check_process_tree_isolation

SOURCE = Path(__file__).resolve().parents[1]
MUTATIONS = {
    "target-role": ('process.Name != "deepseek-worker"', 'process.Name != "deepseekd"'),
    "absolute-target": ('!filepath.IsAbs(process.Path)', 'false'),
    "interpreter-guard": ('legacyCommand(process.Path)', 'false'),
    "deadline": ('context.WithTimeout(ctx, 45*time.Second)', 'context.WithTimeout(ctx, time.Hour)'),
    "fence-factory": ('return newProcessFence()', 'return nil, nil'),
    "fence-prepare": ('fence.prepare(process, environment)', 'unsafePrepare(process, environment)'),
    "fence-admit": ('fence.admit(cmd.Process.Pid)', 'unsafeAdmit(cmd.Process.Pid)'),
    "fence-close": ('defer fence.close()', 'defer unsafeClose()'),
    "fence-attributes": ('cmd.SysProcAttr = fence.attributes()', 'cmd.SysProcAttr = nil'),
    "child-environment": ('cmd.Env = prepared.env', 'cmd.Env = os.Environ()'),
    "guardian-files": ('cmd.ExtraFiles = prepared.files', 'cmd.ExtraFiles = nil'),
    "guardian-cancel": ('cmd.Cancel = prepared.cancel', 'cmd.Cancel = nil'),
    "target-substitution": ('exec.CommandContext(ctx, prepared.path, prepared.args[1:]...)', 'exec.CommandContext(ctx, process.Path)'),
    "failure-settlement": ('_ = cmd.Wait()', '_ = unsafeWait()'),
    "initializer-status": ('if err = cmd.Wait(); err != nil', 'if err = unsafeWait(); err != nil'),
}


@pytest.mark.parametrize("damage", [None, *MUTATIONS, "extra-command", "other-package", "launcher-guard"])
def test_managed_custody_source_requires_fenced_native_initializer(tmp_path: Path, damage: str | None) -> None:
    destination = tmp_path / "go/internal/launch"
    destination.mkdir(parents=True)
    (destination / "plan.go").write_bytes((SOURCE / "go/internal/launch/plan.go").read_bytes())
    text = (SOURCE / "go/internal/launch/managed_profile.go").read_text(encoding="utf-8")
    path = destination / "managed_profile.go"
    if damage in MUTATIONS:
        before, after = MUTATIONS[damage]
        assert before in text
        text = text.replace(before, after)
    elif damage == "extra-command":
        text += '\nfunc unboundedInitializer() { exec.Command("python") }\n'
    elif damage == "other-package":
        path = tmp_path / "go/internal/control/managed_profile.go"
        path.parent.mkdir(parents=True)
    elif damage == "launcher-guard":
        plan = destination / "plan.go"
        plan.write_text(plan.read_text(encoding="utf-8").replace("if legacyCommand(process.Path)", "if false"), encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    result = check_process_tree_isolation(tmp_path)
    assert result.passed is (damage is None)
    if damage is None:
        assert result.data is not None
        assert result.data["native_supervisors_checked"] == 2
        assert result.data["scope"] == "source_contract"
