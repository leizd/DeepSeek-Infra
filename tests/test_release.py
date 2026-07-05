from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


class ReleaseScriptTests(unittest.TestCase):
    def test_release_zip_excludes_runtime_private_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()
            (workspace / "README.md").write_text("ok", encoding="utf-8")
            (workspace / "static").mkdir()
            (workspace / "static" / "app.js").write_text("console.log('ok');", encoding="utf-8")
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
                ".gradle",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                "__pycache__",
                ".venv",
                ".idea",
                "pytest-cache-files-demo",
            ]
            for directory in excluded_dirs:
                path = workspace / directory
                path.mkdir()
                (path / "private.txt").write_text("secret", encoding="utf-8")
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
                [sys.executable, str(script), "--root", str(workspace), "--output-dir", str(output_dir), "--version", "1.2.2"],
                check=True,
                capture_output=True,
                text=True,
            )

            archive_path = Path(result.stdout.strip())
            self.assertTrue(archive_path.is_file())
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())

            self.assertIn("README.md", names)
            self.assertIn("static/app.js", names)
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


