# Frontend Boundaries

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


适用版本：v4.7.4。

> 4.5.0 的 Gate A-D 先完成后端 Transport、Cache、Pipeline 与 Safety；在这些 Gate 通过前不新增漂亮但不可依赖的 Recovery UI。

> 4.4.13 的 Restore Projection（`selectionDigest`、Dependency Closure、Support/Output 分离）、Pack Index/Range、Bloom、Rust Worker Pool、Adaptive Cost 与 Index 迁移全部属于后端/本地索引边界。Frontend 只提交临时 Secret Slot 引用并消费 Federated Restore 状态，从 `from-target` 创建请求携带 `selection`，按返回的 `requiresFrontendApply` / `requiresExternalMcp` 决定是否弹 Frontend 确认或触发 external 准备；不持有 File/Chunk Hash、Pack Offset、投影闭包、Bloom、Index DB 或 Multipart 状态，也不采集、不回显、不持久化 Access Key。

## 前端模块拆分与 Agent timeline 文件位置

前端模块按 `app`、`api`、`domain`、`features` 与 `shared` 边界拆分；Agent timeline 的规范化实现位于 `frontend/src/domain/chat/agentTimeline.ts`。

## Runtime ownership

4.0.8 完成 Legacy Frontend Retirement；4.0.9 将最后一个独立原生界面 Trace Viewer 迁入 React。4.1.0 将 Workspace Provider 下沉到聊天路由，并按需加载 Trace 路由与 Diagnostics 共享详情。4.2.2–4.2.7 建立共享加载边界、Mutation 所有权、并发状态、最新 intent 与精确 blocker；4.2.8 再把这些行为绑定到 exact-merge Evidence 装配。4.3.0–4.3.6 依次完成按需加载、升级连续性、不可变构建身份、更新激活事务、可诊断生命周期检查点与 V3 跨标签页分片。4.3.7 将失败副本永久隔离进多分支 Ledger，以事务化 copy/discard 解决冲突，拆分 tab continuity 与 UUID writer identity，通过不可变 Proposal 收敛无锁同胞写入，自愈损坏 Head，并以完整性校验的 Recovery Capsule V2 防止崩溃与过期标签页破坏历史。4.4.1 再把所有副本状态放进 Workspace Epoch 命名空间：Restore Envelope 先由 Persistence Adapter 写入未激活 Epoch 并逐键回读，随后只切换一个 Active Epoch；旧标签页先冻结、取消自动保存和请求，再把脏内容写入 previous-Epoch Recovery Capsule，绝不向恢复后的共享 Head Flush。Frontend Restore Journal 会与服务端状态机对账，服务端不可达时保留 Fence，不能靠猜测清理。以上均不改变聊天关键 Provider、冻结 4.0 runtime contract 或 Bundle 预算。`/` 与 `/trace/:traceId` 只返回 `frontend/` 的 React + TypeScript + Vite 构建，`/ui/` 作为兼容别名返回同一构建。生成产物位于 gitignored `static/ui/`，不得手工修改。

4.4.2 的密码与 Recovery Identity 只保存在 Backup/Restore feature 的组件局部状态，关闭面板、完成或失败时清空；Context、localStorage、URL 和持久 Journal 均不持有 Secret。恢复 UI 在 age 认证成功前只显示 locked 状态和保护类型，不渲染 Manifest 或 Contributor 细节。

4.4.5 的 Frontend Replica Mirror 上传由 `backupMirror.ts` 做标签页 Leader Election：`localStorage` 租约 + `BroadcastChannel` 心跳，只有 Leader 收集 Envelope 并携带 `clientReplicaId` / 单调 `clientSequence` / `expectedHeadGenerationId`；Restore Fence 冻结上传，离线指数退避，成功后广播 `generationId`。既有 Bundle 预算不提高。

服务端不再提供旧前端路由或环境变量回滚。`static/ui/index.html` 缺失时，本地启动、Android、PyInstaller、Docker、发布 ZIP、release smoke 与 preflight 都会硬失败，并提示运行 `scripts/build_frontend.py`。

## React source boundaries

| Boundary | Location | Responsibility |
| --- | --- | --- |
| App composition / routes | `frontend/src/app/`, `frontend/src/contexts/` | Provider ownership, React Router routes, mutation keys and top-level workspace composition |
| HTTP / streaming | `frontend/src/api/` | JSON, multipart, NDJSON, auth and abortable request boundaries |
| Chat domain | `frontend/src/domain/chat/`, `frontend/src/domain/conversation/` | Reducers, request building, persisted-history migration and selectors; Agent timeline: `frontend/src/domain/chat/agentTimeline.ts` |
| Agent timeline | `frontend/src/domain/chat/agentTimeline.ts` | Agent timeline normalization, stable step identities and legacy-history deduplication |
| Chat UI | `frontend/src/features/chat/`, `frontend/src/features/composer/` | Message flow, generation controls, editing, quoting and composer actions |
| Agent / Activity | `frontend/src/features/agent-run/`, `frontend/src/features/activity/` | Durable runs, plan confirmation, timeline and diagnostics presentation |
| Trace | `frontend/src/features/trace/`, `frontend/src/features/diagnostics/` | Routed Trace detail, shared summary/tree/waterfall/category/error views and drawer integration |
| Workspace | `frontend/src/features/workspace/`, `frontend/src/features/projects/`, `frontend/src/features/skills/`, `frontend/src/features/memory/`, `frontend/src/shared/runUiAction.ts`, `frontend/src/shared/useActionLocks.ts` | Typed feature registry, intent preload, active surface host, Projects/Skills/Memory workflows and contained retry/dispatch |
| Platform features | `frontend/src/features/attachments/`, `frontend/src/features/file-reader/`, `frontend/src/features/reminders/`, `frontend/src/features/speech/` | Uploads, previews, Share Target, reminders, speech and selection quote |
| Styling | `frontend/src/shared/styles/app.css`, `frontend/src/shared/styles/workspace-drawer-frame.css`, feature-owned `*.css` | Initial chat/frame styles plus deferred Workspace and Trace stylesheets |

## Route runtime ownership

- `main.tsx` owns only `BrowserRouter`; it does not initialize application domain state.
- `/` and `/ui/` mount `AppProviders` around `ChatPage`.
- `/trace/:traceId` mounts no workspace Context and lazy-loads `TracePage`.
- Diagnostics lazy-loads the same `TraceDetailView` chunk, so shared Trace rendering remains outside the initial chat bundle until requested.
- `workspaceFeatureRegistry.ts` is the only loader inventory for drawers and contextual Workspace features. Pointer, focus and touch intent share a deduplicated preload promise; preload never opens UI or starts a feature-owned list query.
- `WorkspaceOverlayHost` mounts only the current drawer. Settings, Projects, Skills, Memory and Reminders do not remain hidden in the tree; a later selection wins even when an earlier chunk resolves late.
- `SkillsProvider` is demand-mounted around Projects/Skills only. Root `MemoryContext` owns write continuity for Chat, while `MemoryListContext` and its list query exist only inside the Memory feature.
- `WorkspaceFeatureBoundary` contains optional import failures locally. A chunk failure may consume exactly one fresh retry module identity; if it fails too, the boundary offers refresh/close. Render and business errors never consume module recovery. Feature recovery and the Skills Runtime recovery have independent state.
- Root `MemoryProvider` and lazy `MemoryListProvider` share one barrier per `QueryClient`; save/remove and clear exclude one another before Mutation metadata exists, and lazy remounts retain the original lifecycle blocker.
- `RouteErrorBoundary` contains route render and dynamic-import failures. Trace effects abort in-flight HTTP requests when the route changes, retries, or unmounts; late resolutions from clients that ignore cancellation cannot replace current state.
- `scripts/check_frontend_bundle.py` reads the Vite manifest and blocks releases unless all Workspace features remain dynamic, the initial entry is at most 390,000 bytes and at least 8% below the 4.2.8 baseline, initial CSS is at most 28,000 bytes, each optional JavaScript chunk is at most 90,000 bytes, and the offline inventory resolves to real build outputs.

## PWA ownership

- `/sw.js` maps to generated `static/ui/sw-root.js`.
- `/manifest.webmanifest` maps to generated `static/ui/manifest-root.webmanifest`.
- `/ui/sw.js` and `/ui/manifest.webmanifest` remain build-local aliases for `/ui/` clients.
- Source files live under `frontend/public/`; root files under `static/` are not allowed.

Before bundling, Vite derives an immutable `buildId` from the version, exact source revision and build-configuration version. Formal CI uses `GITHUB_SHA`; dirty local builds include a deterministic frontend source digest. The build then emits matching index metadata, `workspace-assets-<buildId>.json`, `sw-<buildId>.js` and `sw-root-<buildId>.js`, plus a stable `workspace-assets.json` latest pointer. `assetSetDigest` separately covers the emitted page/assets, canonical manifest and Worker templates.

The page registers the build-scoped Worker with `updateViaCache: "none"` and uses MessageChannel against `navigator.serviceWorker.controller`; only a matching, cache-ready controller receives `offlinePrimary` warmup. Warmup remains disabled for Save-Data/slow-2g/2g, is deduplicated across tabs, skips exact hits, retries only missing files after partial failure and never includes Recovery/route-optional chunks.

Each controlled page reports its own build on controller changes, visible-state return and a 60-second heartbeat. The Worker retains the current build, the previous build and every active/recent Client Build Lease. Activation claims and requests leases before pruning; old exact hash assets remain available through repeated deployments, while stable metadata and query-altered requests never cross Cache boundaries. Share Target posts to `/share-target`, then redirects into the root SPA.

## Retained static surface

Legacy retirement does not remove static assets with independent consumers:

| Asset | Reason retained |
| --- | --- |
| `static/icons/` | React favicon, PWA, notification and maskable icons |
| `static/vendor/inter/` | Self-hosted font assets |
| `static/vendor/katex/` | Self-hosted vendor assets kept for compatible document rendering |

`tests/test_frontend_runtime_contract.py` prevents both the retired legacy entry and standalone Trace Viewer files from returning. React component tests, Service Worker behavior tests, the Vite bundle contract and the Chromium evidence gate additionally lock cold-load deferral, preload/query separation, cross-Provider Memory exclusion, truthful and isolated chunk recovery, immutable identity matching, controller-only warmup, resumable multi-tab caching, A→B→C Lease retention/pruning and the existing Trace/Mutation safety contracts.
