# Frontend Boundaries

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


适用版本：v4.0.8。

## Runtime ownership

4.0.8 完成 Legacy Frontend Retirement。`/` 只返回 `frontend/` 的 React + TypeScript + Vite 构建，`/ui/` 作为兼容别名返回同一构建。生成产物位于 gitignored `static/ui/`，不得手工修改。

服务端不再提供旧前端路由或环境变量回滚。`static/ui/index.html` 缺失时，本地启动、Android、PyInstaller、Docker、发布 ZIP、release smoke 与 preflight 都会硬失败，并提示运行 `scripts/build_frontend.py`。

## React source boundaries

| Boundary | Location | Responsibility |
| --- | --- | --- |
| App composition | `frontend/src/app/`, `frontend/src/contexts/` | Provider ownership and top-level workspace composition |
| HTTP / streaming | `frontend/src/api/` | JSON, multipart, NDJSON, auth and abortable request boundaries |
| Chat domain | `frontend/src/domain/chat/`, `frontend/src/domain/conversation/` | Reducers, request building, persisted-history migration and selectors |
| Chat UI | `frontend/src/features/chat/`, `frontend/src/features/composer/` | Message flow, generation controls, editing, quoting and composer actions |
| Agent / Activity | `frontend/src/features/agent-run/`, `frontend/src/features/activity/` | Durable runs, plan confirmation, timeline and diagnostics presentation |
| Workspace | `frontend/src/features/projects/`, `frontend/src/features/skills/`, `frontend/src/features/memory/` | Projects, Skill management/binding and memory workflows |
| Platform features | `frontend/src/features/attachments/`, `frontend/src/features/file-reader/`, `frontend/src/features/reminders/`, `frontend/src/features/speech/` | Uploads, previews, Share Target, reminders, speech and selection quote |
| Styling | `frontend/src/shared/styles/app.css` | The single application stylesheet owned by the React build |

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
| `static/trace_viewer.html` | Independent read-only Trace Viewer page |
| `static/modules/trace_viewer.js` | Trace Viewer request and rendering entry |
| `static/modules/trace_waterfall.js` | Trace tree, waterfall and duration aggregation |
| `static/icons/` | React favicon, PWA, notification and maskable icons |
| `static/vendor/inter/` | Self-hosted font assets |
| `static/vendor/katex/` | Self-hosted vendor assets kept for compatible document rendering |

`tests/test_frontend_runtime_contract.py` prevents the removed entry files and rollback switch from returning while also locking the independent Trace Viewer contract.
