from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import native_action_lifecycle_oracle as oracle
from scripts.native_action_lifecycle_oracle import capture_baseline


def test_action_lifecycle_oracle_executes_pinned_source_in_isolation(tmp_settings: Path) -> None:
    result = capture_baseline()
    assert result["source_commit"] == "a37735c68398fc8f795babaa269e2de6a5acd567"
    assert result["execution"] == "isolated-source-snapshot-real-sqlite"
    assert result["current_worktree_imported"] is False
    cases = {case["id"]: case for case in result["cases"]}
    assert cases["fresh-claim"]["observations"][-1]["state"] == "CLAIMED"
    for state in ("CLAIMED", "EXECUTING", "RECONCILING", "VERIFYING", "ASSESSING_EFFECT"):
        observed = cases[f"takeover-{state}"]["observations"]
        assert observed[-1]["state"] == "RECONCILING"
        assert observed[-1]["epoch"] == 2
        assert observed[-1]["token_replaced"] is True
        assert observed[-1]["locks"][0]["owner"] == "successor"
    assert cases["takeover-at-deadline"]["observations"][-1]["admitted"] is False
    assert cases["renew-expired-current-token"]["observations"][-1]["renewed"] is True
    assert cases["unknown-is-terminal"]["observations"][-1]["locks"] == []
    assert cases["unknown-is-terminal"]["observations"][-1]["admitted"] is False
    assert cases["unknown-is-terminal"]["observations"][-1]["renewed"] is False
    repeated = cases["repeated-takeover"]["observations"]
    assert [row["epoch"] for row in repeated] == [1, 2, 3]
    assert [row["state"] for row in repeated] == ["CLAIMED", "RECONCILING", "RECONCILING"]
    assert cases["stale-token-after-takeover"]["observations"][-1]["renewed"] is False
    encoded = json.dumps(result)
    assert "claimToken" not in encoded
    assert str(tmp_settings) not in encoded


def test_action_lifecycle_fixture_is_reproducible_with_poisoned_runtime_environment(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unexpected_root = tmp_settings / "must-not-be-created"
    monkeypatch.setenv("DEEPSEEK_INFRA_ROOT", str(unexpected_root))
    monkeypatch.setenv("DEEPSEEK_MOBILE_ROOT", str(unexpected_root))
    monkeypatch.setenv("PYTHONPATH", str(unexpected_root))
    expected = json.loads(oracle.CORPUS.read_text(encoding="utf-8"))
    assert capture_baseline() == expected
    assert not unexpected_root.exists()


def test_action_lifecycle_check_rejects_edited_expected_result(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corrupted = json.loads(oracle.CORPUS.read_text(encoding="utf-8"))
    corrupted["cases"][0]["observations"][-1]["state"] = "EFFECT_UNKNOWN"
    fixture = tmp_settings / "tampered.json"
    fixture.write_text(json.dumps(corrupted), encoding="utf-8")
    monkeypatch.setattr(oracle, "CORPUS", fixture)
    with pytest.raises(SystemExit, match="differs from actual baseline"):
        oracle.main(["--check"])
    before = fixture.read_bytes()
    with pytest.raises(SystemExit, match="will not replace"):
        oracle.main(["--write"])
    assert fixture.read_bytes() == before
