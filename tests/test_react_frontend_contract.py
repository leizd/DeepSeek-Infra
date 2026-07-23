from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_react_frontend_is_an_isolated_versioned_build() -> None:
    package = json.loads(read("frontend/package.json"))
    assert package["version"] == "4.3.1"
    assert package["engines"]["node"] == ">=22.12.0"
    assert package["scripts"]["build"] == "tsc --noEmit && vite build"
    assert package["scripts"]["test"] == "vitest run"
    assert package["scripts"]["check:bundle"] == "python ../scripts/check_frontend_bundle.py"
    assert package["dependencies"] == {
        "@tanstack/react-query": "5.101.2",
        "react": "19.2.7",
        "react-dom": "19.2.7",
        "react-router-dom": "7.18.1",
    }
    assert package["devDependencies"]["@testing-library/react"] == "16.3.0"
    assert package["devDependencies"]["@testing-library/user-event"] == "14.6.1"
    assert package["devDependencies"]["jsdom"] == "27.0.1"
    assert (ROOT / "frontend/package-lock.json").is_file()
    assert "registry=https://registry.npmjs.org/" in read("frontend/.npmrc")
    assert "registry.npmmirror.com" not in read("frontend/package-lock.json")

    vite = read("frontend/vite.config.ts")
    assert 'base: "/ui/"' in vite
    assert 'new URL("../static/ui", import.meta.url)' in vite
    assert "manifest: true" in vite
    assert '"/api": "http://127.0.0.1:8000"' in vite
    assert "static/ui/" in read(".gitignore")


def test_react_migration_starts_with_typed_protocol_boundaries() -> None:
    required = (
        "frontend/src/domain/chat/types.ts",
        "frontend/src/domain/chat/streamReducer.ts",
        "frontend/src/domain/conversation/types.ts",
        "frontend/src/api/httpClient.ts",
        "frontend/src/api/chatStream.ts",
    )
    for path in required:
        assert (ROOT / path).is_file(), path

    reducer = read("frontend/src/domain/chat/streamReducer.ts")
    stream = read("frontend/src/api/chatStream.ts")
    client = read("frontend/src/api/httpClient.ts")
    assert "export function applyStreamEvent" in reducer
    assert "document." not in reducer and "fetch(" not in reducer
    assert "export async function* streamChat" in stream
    assert "AsyncGenerator<ChatStreamEvent>" in stream
    assert "localStorage" not in client and "sessionStorage" not in client


def test_react_chat_vertical_slice_has_separated_state_and_ui_boundaries() -> None:
    required = (
        "frontend/src/api/chatApi.ts",
        "frontend/src/api/titleApi.ts",
        "frontend/src/contexts/ChatContext.tsx",
        "frontend/src/contexts/SettingsContext.tsx",
        "frontend/src/contexts/OverlayContext.tsx",
        "frontend/src/domain/chat/chatReducer.ts",
        "frontend/src/domain/chat/requestBuilder.ts",
        "frontend/src/domain/chat/selectors.ts",
        "frontend/src/domain/conversation/migration.ts",
        "frontend/src/domain/conversation/persistence.ts",
        "frontend/src/features/chat/ChatPage.tsx",
        "frontend/src/features/chat/useChatController.ts",
        "frontend/src/features/composer/Composer.tsx",
        "frontend/src/features/history/HistoryDrawer.tsx",
        "frontend/src/shared/markdown/MarkdownContent.tsx",
    )
    for path in required:
        assert (ROOT / path).is_file(), path

    settings = read("frontend/src/contexts/SettingsContext.tsx")
    stream = read("frontend/src/api/chatStream.ts")
    assert 'useState("")' in settings
    assert "localStorage.setItem" not in settings.split("function setModel", 1)[0]
    assert "reader.cancel()" in stream
    assert "reader.releaseLock()" in stream


def test_build_packaging_and_ci_include_the_react_frontend() -> None:
    workflow = read(".github/workflows/ci.yml")
    dockerfile = read("Dockerfile")
    release = read("scripts/release.py")
    exe = read("scripts/build_exe.py")
    android = read("android/app/build.gradle")
    smoke_release = read("scripts/smoke_release.py")
    preflight = read("scripts/preflight_release.py")
    agents = read("AGENTS.md")

    assert "  frontend:" in workflow
    for command in (
        "npm ci --prefix frontend",
        "npm run typecheck --prefix frontend",
        "npm test --prefix frontend",
        "npm run build --prefix frontend",
    ):
        assert command in workflow
    assert "RC_CI_FRONTEND" in workflow
    assert "FROM node:24-bookworm-slim AS frontend-builder" in dockerfile
    assert "test -f /build/static/ui/index.html" in dockerfile
    assert "COPY --from=frontend-builder /build/static/ui ./static/ui" in dockerfile
    assert "test -f /app/static/ui/index.html" in dockerfile
    assert "build_frontend(root)" in release
    assert "require_frontend_build(root)" in release
    assert "FRONTEND_BUILD_SCRIPT" in exe
    assert 'STATIC_DIR / "ui" / "index.html"' in exe
    assert '"--hidden-import=multipart"' in exe
    assert "validate_multipart_build_environment()" in exe
    assert "ignoreExitValue false" in android
    assert "ignoreExitValue true" not in android
    assert "build_frontend.py" in smoke_release
    assert "check_react_frontend_build" in preflight
    assert "React + TypeScript + Vite build" in agents


def test_browser_gate_covers_react_chat_trace_history_stop_and_spa_fallback() -> None:
    smoke = read("scripts/smoke_frontend_browser.py")
    preflight = read("scripts/preflight_release.py")
    assert 'checks["reactOnlyRoot"] = "PASS"' in smoke
    assert 'checks["legacyRouteRetired"] = "PASS"' in smoke
    assert 'checks["rootSpaDeepLink"] = "PASS"' in smoke
    assert 'checks["reactTraceRouteRefresh"] = "PASS"' in smoke
    assert 'checks["traceChunkDeferred"] = "PASS"' in smoke
    assert 'checks["traceRouteProviderIsolation"] = "PASS"' in smoke
    assert 'checks["reactChatVerticalSlice"] = "PASS"' in smoke
    assert 'checks["reactHistoryPersistence"] = "PASS"' in smoke
    assert 'checks["reactStopGeneration"] = "PASS"' in smoke
    assert "projects/example" in smoke
    assert 'get_by_role("heading", name="Waterfall")' in smoke
    for check in ("reactOnlyRoot", "legacyRouteRetired", "rootSpaDeepLink", "reactTraceRouteRefresh", "traceChunkDeferred", "traceRouteProviderIsolation", "reactChatVerticalSlice", "reactHistoryPersistence", "reactStopGeneration"):
        assert f'"{check}"' in preflight


def test_workspace_demand_loading_has_one_registry_and_deferred_providers() -> None:
    registry = read("frontend/src/features/workspace/workspaceFeatureRegistry.ts")
    hosts = read("frontend/src/features/workspace/WorkspaceFeatureHosts.tsx")
    providers = read("frontend/src/app/AppProviders.tsx")
    chat = read("frontend/src/features/chat/ChatPage.tsx")
    memory = read("frontend/src/contexts/MemoryContext.tsx")
    memory_list = read("frontend/src/contexts/MemoryListContext.tsx")
    main = read("frontend/src/main.tsx")
    vite = read("frontend/vite.config.ts")

    for feature in (
        "settings",
        "projects",
        "skills",
        "memory",
        "reminders",
        "diagnostics",
        "file-preview",
        "image-lightbox",
        "activity",
    ):
        assert f'"{feature}"' in registry
    assert "preloadWorkspaceFeature" in registry
    assert "retryWorkspaceFeature" in registry
    assert "WorkspaceFeatureBoundary" in hosts
    assert "SkillsRuntimeBoundary" in hosts
    assert "MemoryListProvider" not in memory
    assert "MemoryListProvider" in memory_list
    assert "SkillsProvider" not in providers
    assert "WorkspaceOverlayHost" in chat
    assert "ContextualFeatureHost" in chat
    assert "scheduleWorkspaceOfflineWarmup" in main
    warmup = read("frontend/src/app/workspaceOfflineWarmup.ts")
    assert 'postMessage({ type: "cache_workspace_primary" })' in warmup
    assert '"slow-2g", "2g"' in warmup
    assert "connection?.saveData" in warmup
    assert "requestIdleCallback" in warmup
    assert '"workspace-assets.json"' in vite


def test_workspace_release_gates_cover_demand_loading_budgets_and_browser_behavior() -> None:
    bundle = read("scripts/check_frontend_bundle.py")
    browser = read("scripts/smoke_frontend_browser.py")
    preflight = read("scripts/preflight_release.py")

    for contract in (
        "BASELINE_428_INITIAL_BYTES = 425_914",
        "MAX_INITIAL_BYTES = 390_000",
        "MAX_INITIAL_CSS_BYTES = 28_000",
        "MAX_OPTIONAL_CHUNK_BYTES = 90_000",
        "MINIMUM_REDUCTION_PERCENT = 8",
    ):
        assert contract in bundle
    for check in (
        "workspaceOptionalChunksDeferred",
        "workspaceFeatureLoadsOnDemand",
        "workspaceFeaturePreloadsOnIntent",
        "preloadDoesNotStartQueries",
        "skillsQueryDeferred",
        "memoryListQueryDeferred",
        "latestOverlayWinsDuringLoad",
        "lazyMutationSurvivesClose",
        "workspaceChunkFailureContained",
        "offlineUnopenedFeatureAvailable",
    ):
        assert f'checks["{check}"] = "PASS"' in browser
        assert f'"{check}"' in preflight
    for check in (
        "memoryBarrierCrossProvider",
        "memoryBarrierSurvivesLazyRemount",
        "chunkRetryProducesNewRequest",
        "chunkRetryExhaustionTruthful",
        "featureRuntimeRecoveryIsolated",
        "currentBuildShellWinsOffline",
        "previousBuildChunkStillAvailable",
        "recoveryChunksDeferred",
    ):
        assert f'checks["{check}"] = "PASS"' in browser
        assert f'"{check}"' in preflight
    for check in ("optionalWarmRespectsSaveData", "optionalWarmRespects2G"):
        assert f'"{check}"' in browser
        assert f'"{check}"' in preflight
    for check in (
        "workspacePrimaryWarmLayer",
        "workspaceRecoveryChunksDeferred",
        "routeOptionalChunksSeparated",
    ):
        assert f'"{check}": "PASS"' in bundle
        assert f'"{check}"' in preflight
