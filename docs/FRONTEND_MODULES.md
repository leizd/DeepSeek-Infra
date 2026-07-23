# Frontend Boundaries

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


适用版本：v4.3.1。

## Runtime ownership

4.0.8 完成 Legacy Frontend Retirement；4.0.9 将最后一个独立原生界面 Trace Viewer 迁入 React。4.1.0 将 Workspace Provider 下沉到聊天路由，并按需加载 Trace 路由与 Diagnostics 共享详情。4.2.2–4.2.7 建立共享加载边界、Mutation 所有权、并发状态、最新 intent 与精确 blocker；4.2.8 再把这些行为绑定到 exact-merge Evidence 装配。4.3.0 将 Workspace 可选界面改为按意图预取、按打开状态挂载；4.3.1 进一步保证跨 lazy Provider 的 Memory 写屏障、真实且隔离的单次 chunk 恢复，以及当前构建优先的离线升级。以上均不改变聊天关键 Provider、后端协议和冻结 4.0 runtime contract。`/` 与 `/trace/:traceId` 只返回 `frontend/` 的 React + TypeScript + Vite 构建，`/ui/` 作为兼容别名返回同一构建。生成产物位于 gitignored `static/ui/`，不得手工修改。

服务端不再提供旧前端路由或环境变量回滚。`static/ui/index.html` 缺失时，本地启动、Android、PyInstaller、Docker、发布 ZIP、release smoke 与 preflight 都会硬失败，并提示运行 `scripts/build_frontend.py`。

## React source boundaries

| Boundary | Location | Responsibility |
| --- | --- | --- |
| App composition / routes | `frontend/src/app/`, `frontend/src/contexts/` | Provider ownership, React Router routes, mutation keys and top-level workspace composition |
| HTTP / streaming | `frontend/src/api/` | JSON, multipart, NDJSON, auth and abortable request boundaries |
| Chat domain | `frontend/src/domain/chat/`, `frontend/src/domain/conversation/` | Reducers, request building, persisted-history migration and selectors |
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

The Vite build writes `workspace-assets.json` with a build ID plus disjoint `core`, `offlinePrimary`, `recovery` and `routeOptional` lists. The root worker precaches Core; the page requests primary Workspace warmup only at idle and skips it for Save-Data/slow-2g/2g, while the worker limits cache population to three requests. Offline navigation, the manifest and un-hashed metadata use only the current build Cache. The previous Cache is consulted solely for exact hashed asset requests from an unrefreshed old page; search parameters are never ignored. Share Target posts to `/share-target`, then redirects into the root SPA.

## Retained static surface

Legacy retirement does not remove static assets with independent consumers:

| Asset | Reason retained |
| --- | --- |
| `static/icons/` | React favicon, PWA, notification and maskable icons |
| `static/vendor/inter/` | Self-hosted font assets |
| `static/vendor/katex/` | Self-hosted vendor assets kept for compatible document rendering |

`tests/test_frontend_runtime_contract.py` prevents both the retired legacy entry and standalone Trace Viewer files from returning. React component tests, Service Worker behavior tests, the Vite bundle contract and the Chromium evidence gate additionally lock cold-load deferral, preload/query separation, cross-Provider Memory exclusion, truthful and isolated chunk recovery, lazy Mutation continuity, four-layer offline inventory, adaptive warmup, two-build Cache ordering and the existing Trace/Mutation safety contracts.
