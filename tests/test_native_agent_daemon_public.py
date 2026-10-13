from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts import verify_native_agent_daemon_public as daemon


def daemon_fixture() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result: dict[str, Any] = dict(status="PASS", runStatus="done", nextIndex=9, durablePhases=3,
        publicEventCount=9, publicNDJSONEventCount=9, resourcesReleased=True, finalAnswerVisible=True,
        inputBodySeededByOfflineOracle=False, productionRuntimeProcesses=["Go", "Rust"],
        publicCreationAndDAGDriverQualified=True, publicPlanConfirmationQualified=True,
        noTasksBeforePublicConfirmation=True, productionProviderQualified=False, fullMigrationQualified=False,
        actualDaemonCLIQualified=True, actualDaemonProcessRestartQualified=True, builtFrontendAssetsServed=True,
        independentlyValidatedByGoStore=True, writerChangedAcrossRestart=True,
        bootstrapUIIsOfflineFixture=False, browserInteractionQualified=False, offlineFixtureScheduledAgent=False,
        daemonProcessStarts=2, daemonRestartMode="graceful-awaiting-plan", writerFenceBeforeRestart=2,
        writerFenceAfterRestart=3, providerCallsBeforeRestart=1, providerCallsAfterRestartBeforeConfirmation=1,
        publicReplayPreserved=True, publicEventsEqualNDJSON=True, frontendIndexSha256="a" * 64, frontendAssetCount=2)
    calls = [dict(actionId=action, operationId=f"operation-{i}", executionEpoch="1", stream=False,
                  selectedTaskInPrompt=True, dependentTaskInPrompt=True, predecessorSummaryInPrompt=True,
                  privateReasoningInPrompt=False, synthesisCoderInPrompt=True, synthesisCriticInPrompt=True)
             for i, action in enumerate(["plan", "tasks", "tasks", "synthesis"])]
    return result, calls


def test_accepts_actual_daemon_restart_and_assets_without_browser_claim() -> None:
    daemon.validate_daemon_result(*daemon_fixture())


@pytest.mark.parametrize(("key", "value"), [
    ("actualDaemonCLIQualified", False), ("actualDaemonProcessRestartQualified", False),
    ("builtFrontendAssetsServed", False), ("independentlyValidatedByGoStore", False),
    ("writerChangedAcrossRestart", False), ("bootstrapUIIsOfflineFixture", True),
    ("browserInteractionQualified", True), ("offlineFixtureScheduledAgent", True),
    ("daemonProcessStarts", 1), ("daemonRestartMode", "controller-loop"),
    ("writerFenceAfterRestart", 2), ("providerCallsBeforeRestart", 2),
    ("providerCallsAfterRestartBeforeConfirmation", 2), ("publicReplayPreserved", False),
    ("publicNDJSONEventCount", 8), ("publicEventsEqualNDJSON", False), ("frontendIndexSha256", "fixture"),
    ("frontendAssetCount", 0), ("productionProviderQualified", True),
])
def test_refuses_incomplete_daemon_evidence(key: str, value: Any) -> None:
    result, calls = daemon_fixture()
    result[key] = value
    with pytest.raises(ValueError):
        daemon.validate_daemon_result(result, calls)


def test_repeated_provider_effect_is_not_qualified() -> None:
    result, calls = daemon_fixture()
    calls.append(deepcopy(calls[-1]))
    with pytest.raises(ValueError):
        daemon.validate_daemon_result(result, calls)


def test_daemon_receives_only_public_worker_configuration(tmp_path: Path) -> None:
    seed = dict(controlPath=str(tmp_path / "go-control"), internalBearer="b" * 48,
                promotionPublicKey="public-only", fleetId="fleet-a", environment="production")
    source = dict(PATH="tools", DEEPSEEK_API_KEY="ambient-provider-secret",
                  DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE="private-file",
                  DEEPSEEK_WORKER_AUTHORITY_NOW="fake-clock", GH_TOKEN="ambient-github-token",
                  DEEPSEEKD_SHADOW_STORE="foreign-shadow-store", DEEPSEEKD_MUTATION_SIGNER_KEY="foreign-public-key")
    environment = daemon.daemon_environment(source, seed, "127.0.0.1:8090", "127.0.0.1:50052",
        tmp_path / "ca.pem", "b" * 48, "2030-01-01T00:00:00Z", "public-worker-identity")
    assert environment["PATH"] == "tools"
    assert environment["DEEPSEEKD_MODE"] == "authoritative"
    assert environment["DEEPSEEK_WORKER_TARGET"] == "127.0.0.1:50052"
    assert not daemon.signing.PRIVATE_ENV.intersection(environment)
    assert "DEEPSEEK_API_KEY" not in environment
    assert "GH_TOKEN" not in environment
    assert "DEEPSEEKD_SHADOW_STORE" not in environment
    assert "DEEPSEEKD_MUTATION_SIGNER_KEY" not in environment
