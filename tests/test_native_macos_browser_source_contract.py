"""Source-admission tests only; no Go process, browser, or compiler is executed."""
from pathlib import Path
import shutil

import pytest

from scripts.check_zero_python_runtime import check_process_tree_isolation

ROOT = Path(__file__).resolve().parents[1]
BROWSER = "go/internal/desktop/browser_darwin.go"
PROFILE = "go/internal/desktop/profile_darwin.go"
MAIN = "go/cmd/deepseek-desktop/main_darwin.go"


def tree(tmp_path: Path, names: list[str]) -> Path:
    for name in names:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return tmp_path


def rewrite(root: Path, name: str, before: str, after: str) -> None:
    file = root / name
    source = file.read_text(encoding="utf8")
    assert before in source
    file.write_text(source.replace(before, after, 1), encoding="utf8")


def test_source_gate_accepts_only_the_bounded_native_mac_browser_hook(tmp_path: Path) -> None:
    root = tree(tmp_path, [BROWSER, MAIN, "go/internal/desktop/browser.go"])
    result = check_process_tree_isolation(root)
    assert result.passed, result.details
    assert result.data is not None and result.data["platform_browser_handlers_checked"] == 1
    assert result.data["scope"] == "source_contract"


@pytest.mark.parametrize("mutation", ["interpreter", "dynamic-path", "url-argv", "extra-env", "no-deadline", "no-wait",
                                     "no-validation", "extra-pipe", "extra-exec", "main-business-mode", "no-native-opener"])
def test_mac_browser_contract_refuses_changed_spawn_or_authority_boundary(tmp_path: Path, mutation: str) -> None:
    root = tree(tmp_path, [BROWSER, MAIN, "go/internal/desktop/browser.go"])
    changes = {
        "interpreter": (BROWSER, '"deepseek-desktop"', '"python3"'),
        "dynamic-path": (BROWSER, 'helper := filepath.Join(filepath.Dir(executable), "deepseek-desktop")', 'helper := os.Getenv("PROGRAM")\n_ = executable'),
        "url-argv": (BROWSER, "exec.CommandContext(ctx, helper)", "exec.CommandContext(ctx, helper, raw)"),
        "extra-env": (BROWSER, "command.Env = macBrowserEnvironment(raw)", "command.Env = os.Environ()"),
        "no-deadline": (BROWSER, "8*time.Second", "time.Hour"),
        "no-wait": (BROWSER, "command.Run()", "command.Start()"),
        "no-validation": (BROWSER, "if err := validateBrowserURL(raw); err != nil", "if err := unrelatedCheck(raw); err != nil"),
        "extra-pipe": (BROWSER, "command.WaitDelay = time.Second", "command.WaitDelay = time.Second\ncommand.ExtraFiles = inheritedFiles"),
        "extra-exec": (BROWSER, "return nil\n}", 'exec.CommandContext(ctx, "node").Run()\nreturn nil\n}'),
        "main-business-mode": (MAIN, 'desktop.OpenMacBrowserOnInitialThread(os.Getenv("DEEPSEEK_BROWSER_URL"))', 'run()'),
        "no-native-opener": (BROWSER, 'macSEL("openURL:")', 'macSEL("unrelated:")'),
    }
    rewrite(root, *changes[mutation])
    assert not check_process_tree_isolation(root).passed


def test_mac_browser_environment_cannot_add_provider_credentials(tmp_path: Path) -> None:
    root = tree(tmp_path, [BROWSER, MAIN, "go/internal/desktop/browser.go"])
    rewrite(root, BROWSER, '"__CF_USER_TEXT_ENCODING"', '"__CF_USER_TEXT_ENCODING", "DEEPSEEK_API_KEY"')
    assert not check_process_tree_isolation(root).passed


def test_mac_hook_does_not_allow_exec_in_another_package(tmp_path: Path) -> None:
    root = tree(tmp_path, [BROWSER, MAIN, "go/internal/desktop/browser.go"])
    target = root / "go/internal/api/browser_darwin.go"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / BROWSER, target)
    assert not check_process_tree_isolation(root).passed


def test_source_gate_accepts_owned_bundle_self_exec_as_a_separate_native_boundary(tmp_path: Path) -> None:
    root = tree(tmp_path, [PROFILE])
    result = check_process_tree_isolation(root)
    assert result.passed, result.details
    assert result.data is not None and result.data["platform_ui_reexecs_checked"] == 1


@pytest.mark.parametrize("invocation", [
    'syscall.Exec("/usr/bin/python3", os.Args, os.Environ())',
    'syscall.Exec(os.Getenv("PROGRAM"), os.Args, os.Environ())',
    'unix.Exec("/bin/sh", os.Args, os.Environ())',
    's.Exec("/usr/bin/python3", os.Args, os.Environ())',
    'Exec("/bin/sh", os.Args, os.Environ())',
])
def test_generic_process_replacement_remains_forbidden(tmp_path: Path, invocation: str) -> None:
    file = tmp_path / "go/internal/api/replace.go"
    file.parent.mkdir(parents=True)
    if invocation.startswith("unix."):
        package = '"golang.org/x/sys/unix"'
    elif invocation.startswith("s."):
        package = 's "syscall"'
    elif invocation.startswith("Exec("):
        package = '. "syscall"'
    else:
        package = '"syscall"'
    file.write_text('package api\nimport ("os"; '+package+')\nfunc replace() { '+invocation+' }\n', encoding="utf8")
    assert not check_process_tree_isolation(tmp_path).passed


@pytest.mark.parametrize("before,after", [
    ('syscall.Exec(result.executable, os.Args, os.Environ())', 'syscall.Exec("/usr/bin/python3", os.Args, os.Environ())'),
    ('os.Open(executable)', 'os.Open(os.Getenv("PROGRAM"))'),
    ('filepath.Join(bin, "deepseek-desktop")', 'filepath.Join(bin, "other-program")'),
    ('source.Stat()', 'os.Stat(executable)'),
    ('info.Mode()&os.ModeSymlink != 0', 'info.Mode()&0 != 0'),
    ('stat.Uid != uint32(os.Geteuid())', 'stat.Uid != stat.Uid'),
])
def test_owned_bundle_exec_requires_unchanged_program_bytes_and_profile_guards(tmp_path: Path, before: str, after: str) -> None:
    root = tree(tmp_path, [PROFILE])
    rewrite(root, PROFILE, before, after)
    assert not check_process_tree_isolation(root).passed
