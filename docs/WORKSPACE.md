# Workspace Core

适用版本：v2.7.0。

v2.5.0 引入 **Workspace Core**；本文适用于 v2.7.0：把项目空间、保存项、生成产物、对话和导出统一成 DeepSeek Infra 的本地 AI 工作台对象模型。它不是新的协议门禁，也不是 Skill / 浏览器控制 / 自动化系统，而是 3.0.0 前的第一块产品地基。

## 对象模型

### Project 2.0

Project 是一级工作台对象：

```json
{
  "projectId": "proj-abc123",
  "name": "考研408复习",
  "description": "复习计划和资料沉淀",
  "createdAt": "2026-06-28T00:00:00Z",
  "updatedAt": "2026-06-28T00:00:00Z",
  "stats": {
    "files": 12,
    "savedItems": 35,
    "artifacts": 8,
    "conversations": 4,
    "memories": 2
  }
}
```

项目详情会聚合：

- `files` / `documents`：项目文档库，仍复用 `.projects/{projectId}/project.json` 与 project files。
- `conversations`：项目内对话快照，支持后续导出。
- `memories`：`scope=project:{projectId}` 的项目记忆。
- `savedItems`：保存项。
- `artifacts`：产物中心索引。

删除项目只移除 `.projects/{projectId}/` 及该项目的 RAG 文件索引，不会删除全局 `.generated/`、`.memory/`、其它项目或 `.traces/`。

### Saved Items

保存项用于把分散上下文变成可复用资料：

```json
{
  "savedId": "save_abc123",
  "projectId": "proj-abc123",
  "type": "chat_snippet",
  "title": "OS 调度总结",
  "content": "RR 适合分时系统...",
  "sourceRef": {
    "conversationId": "conv-1",
    "messageId": "msg-2",
    "fileId": "file-1",
    "artifactId": "art_abc123"
  },
  "tags": ["408", "OS"],
  "purpose": "export_fragment",
  "createdAt": "2026-06-28T00:00:00Z"
}
```

支持类型：`chat_snippet`、`assistant_answer`、`file_quote`、`rag_citation`、`artifact`、`webpage`、`media`、`trace`、`eval_result`。

`purpose` 支持 `reference`、`memory_candidate`、`export_fragment`。v2.6.0 只做保存项，不做复杂 Memory Graph；后续记忆升级可以从保存项筛选。

### Artifact Hub

产物对象把原先的下载链接升级成项目资产：

```json
{
  "artifactId": "art_abc123",
  "projectId": "proj-abc123",
  "type": "markdown",
  "title": "复习提纲",
  "path": ".generated/summary.md",
  "source": {
    "conversationId": "conv-1",
    "messageId": "msg-3",
    "skillId": null
  },
  "version": 1,
  "versions": [
    {"version": 1, "path": ".generated/summary.md"}
  ],
  "downloadUrl": "/api/workspace/artifacts/art_abc123/download?projectId=proj-abc123",
  "createdAt": "2026-06-28T00:00:00Z"
}
```

支持类型：`pptx`、`docx`、`pdf`、`svg`、`markdown`、`csv`、`json`、`html`、`txt`。文本类产物预览会脱敏 API key、Bearer token、query token 等敏感片段。

## API

所有 `/api/workspace/*` 端点使用普通本地 API 鉴权。

| Method | Path | 用途 |
| --- | --- | --- |
| `GET` | `/api/workspace/projects` | 项目列表，含 stats。 |
| `POST` | `/api/workspace/projects` | 创建项目，字段：`name`、可选 `description`。 |
| `GET` | `/api/workspace/projects/{projectId}` | 项目详情，含 files、conversations、memories、savedItems、artifacts。 |
| `PATCH` | `/api/workspace/projects/{projectId}` | 重命名或更新 description。 |
| `DELETE` | `/api/workspace/projects/{projectId}` | 删除单个项目。 |
| `GET` | `/api/workspace/projects/{projectId}/conversations` | 项目对话列表。 |
| `POST` | `/api/workspace/projects/{projectId}/conversations` | 新增或覆盖对话快照。 |
| `GET` | `/api/workspace/projects/{projectId}/saved-items` | 保存项列表，可用 `type` 与 `tags=a,b` 过滤。 |
| `POST` | `/api/workspace/projects/{projectId}/saved-items` | 创建保存项。 |
| `PATCH` | `/api/workspace/projects/{projectId}/saved-items/{savedId}` | 更新保存项标题、内容、标签、用途或 sourceRef。 |
| `DELETE` | `/api/workspace/projects/{projectId}/saved-items/{savedId}` | 删除保存项。 |
| `GET` | `/api/workspace/projects/{projectId}/artifacts` | 产物列表。 |
| `POST` | `/api/workspace/projects/{projectId}/artifacts` | 注册产物到项目。 |
| `PATCH` | `/api/workspace/projects/{projectId}/artifacts/{artifactId}` | 重命名、更新 source，或传 `path` 新增版本。 |
| `DELETE` | `/api/workspace/projects/{projectId}/artifacts/{artifactId}` | 移除产物索引，不删除原始文件。 |
| `GET` | `/api/workspace/artifacts/{artifactId}/preview?projectId=...` | 文本类产物预览。 |
| `GET` | `/api/workspace/artifacts/{artifactId}/download?projectId=...` | 下载产物文件。 |
| `POST` | `/api/workspace/exports` | 创建导出。 |
| `GET` | `/api/workspace/exports/{exportId}/download?projectId=...` | 下载导出文件。 |

兼容旧前端的 `POST /api/projects` 仍保留，并新增 `get` / `rename` action；新开发优先使用 `/api/workspace/*`。

## 导出

`POST /api/workspace/exports` 请求：

```json
{
  "kind": "project",
  "projectId": "proj-abc123",
  "format": "zip"
}
```

`kind` 支持：

- `conversation`
- `project`
- `saved_items`
- `artifacts`
- `evidence`

`format` 支持 `markdown`、`html`、`json`、`zip`。对话导出默认 Markdown，项目和产物包默认 ZIP。

项目 ZIP 包结构：

```text
project-export.zip
  metadata.json
  project.md
  conversations/
    conversation-xxx.md
  saved-items/
    saved-items.json
  artifacts/
    xxx.docx
    xxx.pptx
  files/
    source-files/
      notes.txt
  traces/
    trace-save_xxx.json
```

导出规则：

- `metadata.json` 总是存在。
- Markdown 导出保留保存项 sourceRef、产物相对链接和对话消息。
- ZIP 导出包含项目产物文件；文本类产物和疑似含密钥的文件会写入脱敏文本。
- API key、Authorization / Bearer token、query token、password / secret 字段会脱敏。
- `files/source-files/` 使用已解析的缓存文本，不直接打包原始二进制上传文件。

## Evidence

Workspace Core smoke 是离线的，不需要 API key 或网络：

```bash
python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v2.7.0.json
```

证据文件包含统一 metadata：

```json
{
  "version": "2.6.8",
  "commit": "abc1234",
  "generatedAt": "2026-06-28T00:00:00Z",
  "environment": {"os": "Windows", "python": "3.12", "ci": false},
  "status": "PASS",
  "checks": {
    "projectCreate": "PASS",
    "savedItemCreate": "PASS",
    "artifactList": "PASS",
    "conversationExport": "PASS",
    "projectExportZip": "PASS",
    "secretRedaction": "PASS"
  }
}
```

`scripts/preflight_release.py --version 2.6.8` 会把 `workspace_core_evidence` 作为硬检查。`scripts/smoke_release.py --offline` 默认会先跑 Workspace Core smoke 与 Skill System smoke，再跑 eval / security / agent / baseline compare。
