from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


class DoctorScriptTests(unittest.TestCase):
    def test_doctor_json_reports_core_checks_and_exits_zero(self) -> None:
        script = Path.cwd() / "scripts" / "doctor.py"
        result = subprocess.run(
            [sys.executable, str(script), "--json"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            # Windows 控制台默认 GBK：钉住子进程 stdout 编码，跨平台稳定解码
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        names = " | ".join(check["name"] for check in report["checks"])
        for expected in ("Python", "核心依赖", "static 目录", "数据目录可写", "安全配置"):
            with self.subTest(expected=expected):
                self.assertIn(expected, names)
        # doctor 是诊断工具：只读环境，绝不应有 FAIL 之外的非零路径
        statuses = {check["status"] for check in report["checks"]}
        self.assertTrue(statuses.issubset({"OK", "WARN", "FAIL"}))


if __name__ == "__main__":
    unittest.main()
