# Frontend Boundaries

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


适用版本：v4.2.0。

## Runtime ownership

4.0.8 完成 Legacy Frontend Retirement；4.0.9 进一步将最后一个独立原生界面 Trace Viewer 迁入 React。4.1.0 将 Workspace Provider 下沉到聊天路由，并按需加载 Trace 路由与 Diagnostics 共享详情。4.2.0 为共享 Trace 加载边界增加原地重试和陈旧响应保护。`/` 与 `/trace/:traceId` 只返回 `frontend/` 的 React + TypeScript + Vite 构建，`/ui/` 作为兼容别名返回同一构建。生成产物位于 gitignored `static/ui/`，不得手工修改。

服务端不再提供旧前端路由或环境变量回滚。`static/ui/index.html` 缺失时，本地启动、Android、PyInstaller、Docker、发布 ZIP、release smoke 与 preflight 都会硬失败，并提示运行 `scripts/build_frontend.py`。

## React source boundaries

| Boundary | Location | Responsibility |
| --- | --- | --- |
| App composition / routes | `frontend/src/app/`, `frontend/src/contexts/` | Provider ownership, React Router routes and top-level workspace composition |
| HTTP / streaming | `frontend/src/api/` | JSON, multipart, NDJSON, auth and abortable request boundaries |
| Chat domain | `frontend/src/domain/chat/`, `frontend/src/domain/conversation/` | Reducers, request building, persisted-history migration and selectors |
| Chat UI | `frontend/src/features/chat/`, `frontend/src/features/composer/` | Message flow, generation controls, editing, quoting and composer actions |
| Agent / Activity | `frontend/src/features/agent-run/`, `frontend/src/features/activity/` | Durable runs, plan confirmation, timeline and diagnostics presentation |
| Trace | `frontend/src/features/trace/`, `frontend/src/features/diagnostics/` | Routed Trace detail, shared summary/tree/waterfall/category/error views and drawer integration |
| Workspace | `frontend/src/features/projects/`, `frontend/src/features/skills/`, `frontend/src/features/memory/` | Projects, Skill management/binding and memory workflows |
| Platform features | `frontend/src/features/attachments/`, `frontend/src/features/file-reader/`, `frontend/src/features/reminders/`, `frontend/src/features/speech/` | Uploads, previews, Share Target, reminders, speech and selection quote |
| Styling | `frontend/src/shared/styles/app.css`, `frontend/src/features/trace/trace.css` | Shared application styles plus the first feature-scoped stylesheet |

## Route runtime ownership

- `main.tsx` owns only `BrowserRouter`; it does not initialize application domain state.
- `/` and `/ui/` mount `AppProviders` around `ChatPage`.
- `/trace/:traceId` mounts no workspace Context and lazy-loads `TracePage`.
- Diagnostics lazy-loads the same `TraceDetailView` chunk, so shared Trace rendering remains outside the initial chat bundle until requested.
- `RouteErrorBoundary` contains route render and dynamic-import failures. Trace effects abort in-flight HTTP requests when the route changes, retries, or unmounts; late resolutions from clients that ignore cancellation cannot replace current state.
- `scripts/check_frontend_bundle.py` reads the Vite manifest and blocks releases if Trace JavaScript or CSS leaks into the initial entry.

## PWA ownership

- `/sw.js` maps to generated `static/ui/sw-root.js`.
- `/manifest.webmanifest` maps to generated `static/ui/manifest-root.webmanifest`.
- `/ui/sw.js` and `/ui/manifest.webmanifest` remain build-local aliases for `/ui/` clients.
- Source files live under `frontend/public/`; root files under `static/` are not allowed.

The root worker owns navigation recovery, offline refresh, cache replacement and reminder notifications. Share Target posts to `/share-target`, then redirects into the root SPA.

## Retained static surface

Legacy retirement does not remove static assets with independent consumers:

| Asset | Reason retained |
| --- | --- |
| `static/icons/` | React favicon, PWA, notification and maskable icons |
| `static/vendor/inter/` | Self-hosted font assets |
| `static/vendor/katex/` | Self-hosted vendor assets kept for compatible document rendering |

`tests/test_frontend_runtime_contract.py` prevents both the retired legacy entry and standalone Trace Viewer files from returning. React component tests, the Vite bundle contract, and the Chromium evidence gate lock `/trace/:traceId`, request cancellation, retry recovery, stale-response suppression, provider isolation, deferred chunks, API failure rendering, deep-link refresh, and shared Diagnostics ownership.
