from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts import release


class ReleaseScriptTests(unittest.TestCase):
    def test_collect_files_excludes_unowned_root_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("tracked", encoding="utf-8")
            (root / "notes.md").write_text("private", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "existing.md").write_text("tracked", encoding="utf-8")
            (root / "docs" / "private-notes.md").write_text("private", encoding="utf-8")
            (root / "docs" / "releases").mkdir()
            (root / "docs" / "releases" / "new-release.md").write_text("generated", encoding="utf-8")
            (root / "static" / "ui").mkdir(parents=True)
            (root / "static" / "ui" / "index.html").write_text("generated", encoding="utf-8")
            (root / "private-deck").mkdir()
            (root / "private-deck" / "slide.png").write_bytes(b"private")
            tracked = "README.md\0docs/existing.md\0static/favicon.ico\0"
            completed = subprocess.CompletedProcess(["git", "ls-files"], 0, stdout=tracked, stderr="")

            with mock.patch.object(release.subprocess, "run", return_value=completed):
                names = {path.relative_to(root).as_posix() for path in release.collect_files(root)}

            self.assertEqual(names, {"README.md", "docs/existing.md", "docs/releases/new-release.md", "static/ui/index.html"})

    def test_release_zip_excludes_runtime_private_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()
            (workspace / "README.md").write_text("ok", encoding="utf-8")
            (workspace / "static" / "ui").mkdir(parents=True)
            (workspace / "static" / "ui" / "index.html").write_text("<main>react</main>", encoding="utf-8")
            excluded_dirs = [
                ".file-cache",
                ".agent-runs",
                ".memory",
                ".projects",
                ".reminders",
                ".search-cache",
                ".budget",
                ".tool-audit",
                ".browser-audit",
                ".browser-downloads",
                ".browser-profiles",
                ".automation",
                ".scheduler",
                ".skills",
                ".local-rag",
                ".traces",
                ".semantic-cache",
                ".request-queue",
                ".generated",
                "artifacts",
                ".gradle",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                "__pycache__",
                ".venv",
                ".idea",
                "target",
                "pytest-cache-files-demo",
                ".tmp-pytest-release-gate",
            ]
            for directory in excluded_dirs:
                path = workspace / directory
                path.mkdir()
                (path / "private.txt").write_text("secret", encoding="utf-8")
            (workspace / "artifacts" / "benchmark-sensitive-payload.json").write_text("secret", encoding="utf-8")
            (workspace / "server.8010.err.log").write_text("secret", encoding="utf-8")
            (workspace / ".server.err.log").write_text("secret", encoding="utf-8")
            (workspace / ".coverage").write_text("secret", encoding="utf-8")
            (workspace / ".auth-token").write_text("secret", encoding="utf-8")
            # Deployment secrets must never ship; the committed template must.
            (workspace / ".env").write_text("DEEPSEEK_API_KEY=secret", encoding="utf-8")
            (workspace / ".env.example").write_text("DEEPSEEK_API_KEY=", encoding="utf-8")
            (workspace / "release.jks").write_text("secret", encoding="utf-8")
            (workspace / "release.keystore").write_text("secret", encoding="utf-8")
            (workspace / "signing.properties").write_text("secret", encoding="utf-8")
            (workspace / "keystore.properties").write_text("secret", encoding="utf-8")
            # VCS / tooling metadata and the encrypted launcher credential store must never ship.
            (workspace / ".git").mkdir()
            (workspace / ".git" / "config").write_text("secret", encoding="utf-8")
            (workspace / ".claude").mkdir()
            (workspace / ".claude" / "settings.local.json").write_text("secret", encoding="utf-8")
            (workspace / ".launcher-config.json").write_text("secret", encoding="utf-8")

            output_dir = Path(tmp) / "out"
            script = Path.cwd() / "scripts" / "release.py"
            result = subprocess.run(
                [sys.executable, str(script), "--root", str(workspace), "--output-dir", str(output_dir), "--version", "1.2.2", "--skip-frontend-build"],
                check=True,
                capture_output=True,
                text=True,
            )

            archive_path = Path(result.stdout.strip())
            self.assertTrue(archive_path.is_file())
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())

            self.assertIn("README.md", names)
            self.assertIn("static/ui/index.html", names)
            self.assertNotIn("server.8010.err.log", names)
            self.assertNotIn(".server.err.log", names)
            self.assertFalse(any(name.startswith(tuple(f"{directory}/" for directory in excluded_dirs)) for name in names))
            self.assertNotIn(".coverage", names)
            self.assertNotIn(".auth-token", names)
            self.assertNotIn(".env", names)
            self.assertIn(".env.example", names)
            self.assertNotIn("release.jks", names)
            self.assertNotIn("release.keystore", names)
            self.assertNotIn("signing.properties", names)
            self.assertNotIn("keystore.properties", names)
            self.assertFalse(any(name.startswith((".git/", ".claude/")) for name in names))
            self.assertNotIn(".launcher-config.json", names)


if __name__ == "__main__":
    unittest.main()


