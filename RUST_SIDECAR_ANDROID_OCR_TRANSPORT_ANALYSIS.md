# Rust Sidecar 与 Android OCR / 多模态输入的传输协同分析

<!-- docs-language-switcher:start -->
[中文](README.md) / [English](README.en.md)
<!-- docs-language-switcher:end -->


> 分析基准：工作区 `D:\deepseek` 当前工作树（`VERSION` 主线 4.8.0，4.4.15 基础切片已在树内）。
> 方法：以源码与 `docs/ARCHITECTURE.md`、`docs/adr/ADR-0040-*` 为准，逐文件核对，不引用规划中的能力。
> 结论口径：**只描述已落地并可验证的行为**；未落地部分单独列在「缺口」一节。

---

## 0. 首要结论（与提问前提的偏差）

提问假设存在一条「Rust Sidecar ↔ Android OCR ↔ 多模态输入」的协同传输链路。

**当前代码库中不存在这条链路。** 这三者属于三个互相独立、无直接连接的平面：

| 平面 | 载体 | 协议 | 是否参与 OCR |
| --- | --- | --- | --- |
| Android 设备平面 | Chaquopy 嵌入式 CPython + JNI | Chaquopy 桥 / `jclass` | **是**（ML Kit 引擎） |
| Rust Sidecar 平面 | `rust/` 独立二进制 | 回环 HTTP/JSON | **否**（显式排除） |
| 多模态数据平面 | `POST /api/media` → media 管线 | HTTP multipart/JSON，进程内调用 | 是（作为 segments 生产者） |

两处硬证据：

1. `docs/ARCHITECTURE.md:220` 明确列出 Sidecar **不实现**：「网关流式、上游 HTTP、MCP 传输、真实工具执行、文件读取、**OCR**、embeddings、SQLite 或索引持久化。这些能力都留在 Python 路径。」
2. `proto/` 下只有 `action / agent / common / control / evidence / federation / storage` 七个 v1 契约，**没有 media / vision / ocr**；`rust/crates/*` 全量检索 `\bocr\b` 为空命中。即不存在把 OCR 语义送进 Rust 的 wire 契约。

因此下面先分别还原三条真实链路，再讨论「它们之间已经存在的协同点」与「要真正协同需要补什么」。

---

## 1. 组件与落点清单

### 1.1 Android 侧

| 文件 | 职责 | 关键常量 |
| --- | --- | --- |
| `android/app/src/main/java/com/deepseek/mobile/MainActivity.java` | 原生 WebView 壳 + Python 启动 | `SERVER_PORT = 8000`、`FILE_CHOOSER_REQUEST = 5010` |
| `deepseek_infra/android_entry.py` | Chaquopy 桥（唯一被 Java 调用的 Python 模块） | `DEFAULT_ANDROID_PORT = 8000` |
| `android/.../AndroidOcrBridge.java` | ML Kit OCR 桥 | `OCR_TIMEOUT_SECONDS = 60`、`PDF_RENDER_SCALE = 3`、`MAX_PDF_BITMAP_PIXELS = 6_000_000` |
| `deepseek_infra/infra/tool_runtime/ocr.py` | OCR 引擎抽象与候选链 | `OCR_MODES = {fast, balanced, quality}`、`OCR_UPSCALE_MAX = 3.0`、`DEEPSEEK_OCR_MODEL = "deepseek-v4-pro"` |

### 1.2 Rust Sidecar 侧

| 文件 | 职责 | 关键常量 |
| --- | --- | --- |
| `deepseek_infra/infra/rust_core/config.py` | feature flag 与 URL | `DEFAULT_RUST_GATEWAY_URL = http://127.0.0.1:8787` |
| `deepseek_infra/infra/rust_core/transport.py` | 持久连接池传输层 | `DEFAULT_MAX_CONNECTIONS = 32`、`DEFAULT_MAX_RESPONSE_BYTES = 16 MiB` |
| `deepseek_infra/infra/rust_core/gateway_client.py` | Gateway 委托客户端 | `DEFAULT_TIMEOUT_MS = 3000` |
| `deepseek_infra/infra/rust_core/{mcp,policy,rag}_client.py` | 其余三个委托客户端 | — |
| `rust/crates/deepseek-{gateway,mcp,policy,rag}` | 四个委托实现 | `Cargo.toml` workspace，edition 2024 |
| `docker-compose.rust.yml` | 独立可选部署 | 容器内 `0.0.0.0:8787`，宿主发布为 `127.0.0.1:8787:8787` |

### 1.3 多模态数据侧

| 文件 | 职责 |
| --- | --- |
| `deepseek_infra/web/routes/media.py` | `POST /api/media` 等路由 |
| `deepseek_infra/infra/media/ingestion.py` | 摄入 + 处理编排、状态机 |
| `deepseek_infra/infra/media/processors.py` | 按类型产出 segments（图片/PDF 走 OCR） |
| `deepseek_infra/infra/media/{library,indexer,schema,citations}.py` | 落盘、索引、校验、引用 |
| `deepseek_infra/infra/media/schema.py` | `MAX_MEDIA_UPLOAD_BYTES = 50 MiB`、`MAX_MEDIA_UPLOADS_PER_REQUEST = 20` |

### 1.4 第三个相关平面：原生权威（Go/Rust worker）

`deepseek_infra/infra/native_runtime/authority.py` 定义 `GO_CONTROL_DOMAINS`（policy / target / scheduler / action / risk / wave / capacity / forecast / maintenance / federation_* / agent_run / dr_*）与 `RuntimeMode`，`assert_python_writer_allowed()` 在 `go_authoritative` / `python_disabled` 下对上述域抛出 `PythonWriterMechanicallyDeniedError`。`native_runtime/process_tree.py` 另有 `assert_zero_python_process_tree()`，禁止进程树中出现 `python/python3/cpython/pypy`。

**注意这个不变量与 Android 拓扑互斥**：`process_tree` 的零 Python 约束服务于服务器生产拓扑，而 Android 端 Python 是唯一运行时。这直接决定了「把 Android OCR 迁到 Rust sidecar」在架构上不成立——只能做服务器侧双实现，必然分叉。

---

## 2. 数据流转路径

### 2.1 Android 启动路径（控制面，一次性）

```
MainActivity.onCreate
  → buildLayout()          FrameLayout + WebView + ProgressBar
  → configureWebView()     JS/DOM/Database 使能；禁止 file access
  → startPythonServer()    新线程 "deepseek-python-start"
      → Python.start(new AndroidPlatform(this))            [Chaquopy 初始化]
      → AndroidOcrBridge.initialize(getApplicationContext()) [OCR 桥握手，唯一注入点]
      → module = getModule("deepseek_infra.android_entry")
      → module.dependency_versions()  → fastapi/pydantic/uvicorn 版本探针
      → module.start_json(filesDir, 8000, "", "", false)  → 返回 JSON 字符串
      → JSONObject 解析 {url, phoneUrl, host, port}
      → runOnUiThread { progressBar.GONE; webView.loadUrl(url) }
```

失败分支：`catch (Exception)` → `showStartupError()`，在 UI 上以红色文本展示版本号 + `formatException`（最多 6 层 `Caused by`）+ 依赖探针结果。`onDestroy` 且 `isFinishing()` 时调用 `module.stop()`。

导航边界：`isLocalAppUrl()` 只允许 `http://127.0.0.1|localhost` 留在 WebView 内，其余一律 `Intent.ACTION_VIEW` 外跳。文件选择走 `onShowFileChooser` → `startActivityForResult(5010)` → `FileChooserParams.parseResult`。

### 2.2 Android OCR 路径（数据面，逐跳）

```
1. WebView 前端 → POST /api/media?process=true  (multipart/form-data)
     fields: projectId / process / ocrEnabled / apiKey / title
     校验: 单文件 ≤ 50 MiB；每请求 ≤ 20 个 upload；JSON 分支 body ≤ 16 MB

2. media.py: api_media_create  → ingestion.ingest_upload(...)
      → schema.validate_media_upload_size / validate_media_mime_type
      → normalize_media_type → library.save_source_bytes → register_media
      → process=true 时进入 process_media(media_id, ocr_enabled, ocr_api_key)

3. ingestion.process_media
      → library.set_status("processing")
      → processors.extract_segments(media, ...)          ← OCR 在此发生
      → schema.normalize_segment + citations.citation_for_segment（逐段）
      → library.save_segments → indexer.index_media_segments
      → library.update_media(status="ready", metadata{segmentCount, indexedChunkCount, pageCount, durationSec})

4. processors.extract_segments 分派
      media_type == image → image_segments   （先读 metadata["ocrText"]/["text"]，为空且 ocr_enabled 才真跑 OCR）
      media_type == pdf   → pdf_segments     （rag_files.extract_pdf_text(..., ocr_enabled=True)）
      产出片段形如 {"type": "ocr_text", "text": ..., "page": 1, "confidence": ...}

5. ocr.py::_ocr_engine_candidates 候选链（顺序即优先级）
      deepseek-api         需要 API Key，走云端多模态，模型 deepseek-v4-pro
      ── 若 os.environ["DEEPSEEK_ANDROID_APP"] == "1" ──
      android-mlkit        构造成功即 **提前 return**，后续候选不再加入
      ── 否则 ──
      formula-command      env 配置的外部 CLI
      tesseract            OCR_MODE 决定增强档位
      windows-ocr          仅 os.name == "nt"

6. AndroidMlKitEngine.__init__
      from java import jclass                     ← Chaquopy 的 java 模块（pyjnius）
      jclass("com.deepseek.mobile.AndroidOcrBridge")
      bridge.isAvailable()  ← 校验 appContext 非空

7. 跨语言调用（设备内唯一的跨语言边界）
      extract_image(bytes)  → bridge.recognizeImage(jbyteArray)
      extract(pdf_bytes)    → bridge.recognizePdf(jbyteArray)

8. AndroidOcrBridge 内部
      图片: BitmapFactory.decodeByteArray → recognizeBitmap → finally bitmap.recycle()
      PDF : 写 cacheDir 临时文件 → ParcelFileDescriptor + PdfRenderer
            → 逐页 renderPage()（3x 缩放，超 6M px 用 sqrt 等比压回）
            → recognizeBitmap() → "[PDF 第 N 页 (OCR)]\n<text>"
            → finally 删除临时文件（失败则 deleteOnExit）
      ML Kit: InputImage.fromBitmap(bitmap, 0) → Task<Text>
            → CountDownLatch(1) + AtomicReference(result/error) 把异步 Task 转同步
            → latch.await(60, SECONDS)，超时抛 IllegalStateException

9. 回流
      normalize_ocr_text(文本) → segments → 引用 → indexer → status="ready"
```

关键结构性事实：**Android 端 `AndroidMlKitEngine` 没有实现 `extract_page_image`**，而 `extract`/`extract_image` 由 Java 侧自己完成 PDF 渲染。因此 Python 的 `_extract_pdf_with_page_fallback`（逐页、逐引擎打分择优）在 Android 上不生效——桌面端的「Python 渲染 + Tesseract/Windows OCR」与移动端的「Java 渲染 + ML Kit」是两套完全不同的失败恢复结构。

### 2.3 Rust Sidecar 路径

```
调用方（如 gateway 请求准备）
  → _rust_gateway_enabled() 查 DEEPSEEK_RUST_GATEWAY（默认 False，入口直接返回 disabled）
  → transport.new_correlation_id() = uuid4().hex
  → 头: Accept: application/json, X-DeepSeek-Request-ID: <corr>
        payload 时额外 Content-Type: application/json
        `del headers` ← 本地认证与 provider 凭据永不转发给 Rust
  → json.dumps(payload).encode("utf-8")，记录 serialization_us
  → transport.urlopen(req, timeout)  ← 走持久连接池
  → 响应：先卡 16 MiB 上限，再 json.loads，再要求 isinstance(dict)
  → 读取 X-DeepSeek-Rust-Processing-Us 头填入 rust_processing_us
  → 返回 GatewayProxyResult（含 OK/失败分类与四段计时）
```

监听器端点（单一 8787 监听器）：`GET /healthz`（`service == "deepseek-gateway-rs"`）、`GET /v1/models`、`POST /v1/chat/completions`、`POST /gateway/request/prepare`、`POST /mcp/request/prepare`、`POST /policy/{url,path,capability}`、`POST /rag/vectors/rank`、`POST /rag/vectors/rank-binary`、`POST /rag/documents/prepare`。

---

## 3. 通信协议与接口定义

这套系统里实际存在 **四种互不相同的接口契约**，这是理解「协同」的关键：

| # | 边界 | 协议形态 | 接口定义位置 | 序列化 |
| --- | --- | --- | --- | --- |
| 1 | Java → Python | Chaquopy 反射调用 | `android_entry.start_json / stop / dependency_versions` | 单个 JSON 字符串（`ensure_ascii=False`） |
| 2 | Python → Java | `jclass` 静态方法调用 | `AndroidOcrBridge.recognizeImage / recognizePdf / isAvailable / initialize` | JNI `byte[]` 按值拷贝 |
| 3 | Python → Rust | HTTP/1.1 + JSON | 9 个端点，见上 | `json.dumps` UTF-8；向量另有紧凑二进制 |
| 4 | 前端 → Python | HTTP multipart / JSON | `POST /api/media`、`/api/media/{id}/process` 等 | multipart 表单 + JSON |

第 1 条契约面积极小——`start_json(...) -> str` 是 Java 与 Python 之间**唯一**的结构化返回，`_handle_payload` 只回 `{url, phoneUrl, host, port}`。这意味着启动期的任何信息不足都只能靠 `dependency_versions()` 探针 + 日志补齐。

第 3 条契约有明确纪律，值得作为「协同」的范式：

- **凭据零转发**：`transport.urlopen` 直接拒收带 userinfo 的 URL（`parsed.username is not None` → `URLError`），且 `_request` 显式 `del headers`。
- **关联 ID**：`X-DeepSeek-Request-ID`，仅由 `uuid4().hex` 生成，日志安全。
- **形状门**：响应必须是 JSON `dict`，否则 `rust_invalid_shape`；空体是 `rust_empty_response`（注意此处 `ok=True`）。
- **错误分类**：`rust_gateway_disabled / rust_backend_unavailable / rust_backend_timeout / rust_http_error / rust_malformed_json / rust_invalid_shape / rust_empty_response`。
- **只做确定性准备**：Python 先本地算一份，仅当 Rust 结果契约等价时才采纳。MCP 委托更进一步——「总是先计算 Python 结果，任何 Rust 分歧都使用 Python 结果继续执行」。

---

## 4. 序列化与传输方式

| 通道 | 编码 | 边界开销 | 约束 |
| --- | --- | --- | --- |
| Java→Python 启动 | `json.dumps(..., ensure_ascii=False)` | 一次字符串转换 | 仅 4 个字段 |
| Python→Java OCR | JNI `byte[]`，按值拷贝 | **整份文件一次内存拷贝** | 文件已在 multipart 解析时进过内存，峰值放大明显 |
| Python→Java 结果 | `str` 返回，`normalize_ocr_text` 归一 | — | — |
| Python→Rust | JSON UTF-8 | `serialization_us` 单独计时 | 16 MiB 响应上限（env 可调到 64 MiB） |
| Python→Rust 向量 | `f64le-v1` 小端定长 | 免 JSON decode 与 list-of-lists 重建 | 固定 24 字节响应；`DSVRNK01` 魔数；严格校验长度与边界；无 `auto` 模式 |
| 跨进程控制面 | Protobuf（`proto/*/v1`） | — | 仅 Go/Rust 控制面使用，不含 media/vision |
| Python→云端 OCR | 图片字节上传，模型 `deepseek-v4-pro` | 网络 | 需要 API Key，是链路上唯一的「真多模态」模型调用 |

向量二进制路径的设计值得单独指出：`DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT` 非法值**失败关闭到 JSON**，二进制失败**直接回落 Python**、不再发第二次 JSON 请求；Python 侧权威扫描始终保留。`f64le-v1` BLOB 与 JSON 列双写，旧行逐行回落，JSON 列永不删除以保回滚可读。

---

## 5. 异步处理

这是当前实现里最集中的问题区。

**已有的正确做法：**

- `MainActivity.startPythonServer()` 在专门 Java 线程 `deepseek-python-start` 上执行，不阻塞 UI；就绪后 `runOnUiThread` 才更新视图。
- `process_media` 有显式状态机：`processing → ready | failed`，失败时 `metadata_patch={"error": str(exc)[:500]}`。
- Rust sidecar 客户端本身是同步阻塞的 `urlopen`，但被放在 Python 的调用点，且连接池 `acquire()` 用 `Condition.wait(remaining)` 带 deadline 等待，不是忙等。
- 传输层有 fork 安全：`os.register_at_fork(after_in_child=...)` 在子进程重建 manager 与锁，避免继承其他线程的锁；`_current_manager()` 在 PID 变化时惰性重建。

**问题：**

1. **事件循环阻塞（高优先级）**。`media.py:api_media_create` 是 `async def`，但第 45–55 行**同步直接调用** `ingestion.ingest_upload(...)`，未经 threadpool 卸载。该调用可能包含最长 60 秒的 ML Kit OCR。Android 上 uvicorn 是单进程单事件循环，结果是：一次大文件 OCR 会让 WebView 的**所有** API 请求一起挂起。`MAX_MEDIA_UPLOADS_PER_REQUEST = 20` 使单次请求可叠加最多 20 次串行 OCR。
2. **异步被强制转同步**。ML Kit 原生是 `Task` 异步模型，`AndroidOcrBridge.recognizeBitmap` 用 `CountDownLatch.await(60s)` 把它拉平成同步，调用线程（即 Python/JNI 线程）全程占用。60 秒是一个很长的单点停顿。
3. **启动无超时**。`start_json(...)` 在 Java 线程上无限期阻塞，没有超时或取消。若 `prepare_and_start` 挂起，UI 会永久停在 `ProgressBar`——`showStartupError` 只覆盖抛异常的情况。
4. **无背压与限流**。OCR 链路上没有 semaphore、没有任务队列、没有并发上限。与之对比，sidecar 传输层明确有 `max_connections`（默认 32，env 夹在 1..128）作为并发闸门。

---

## 6. 错误恢复

**分层恢复链（已实现）：**

| 层 | 机制 |
| --- | --- |
| 引擎选择 | `_ocr_engine_candidates` 逐个 try/except 构造，失败的进 `errors` 列表，最终通过 `_with_error_details` 截取前 4 条附在报错信息里 |
| 逐页逐引擎 | `_extract_pdf_with_page_fallback` 对每页遍历所有具备 `extract_page_image` 的引擎，用 `_ocr_text_score` 打分取最优；`OCR_EMPTY` 置 `saw_empty_result` 标记；`mode == "fast"` 时一旦得分 > 0 立即返回 |
| 文本归一 | `normalize_ocr_text` 统一各引擎输出 |
| 媒体状态 | `process_media` 捕获后置 `failed` 并截断错误；`AppError` 原样透传，其余包成 `AppError(code=INTERNAL, status=500)` |
| 传输层 | 连接在 `OSError / HTTPException / timeout` 时以 `reusable=False` 释放；`response.will_close` 决定是否复用 |
| 容量保护 | 响应超过 `_response_limit()` 抛 `ResponseTooLargeError`（继承 `OSError`，天然落入不可复用分支） |
| 组件级 | Gateway 失败 → Python 请求准备；MCP → 始终先用 Python 结果；Policy → `fallback`（默认）/ `deny` / `error` 三档；RAG → 回 Python 扫描；文档准备 → 丢弃畸形/分歧输出后仍以 Python chunks 持久化 |

**缺口：**

- **JNI 异常未归一化**。`AndroidOcrBridge` 抛的是 `IllegalArgumentException("Image bytes cannot be decoded.")` / `IllegalStateException("Android OCR bridge is not initialized." / "Android OCR timed out.")`，而 `AndroidMlKitEngine.extract/extract_image` **没有**把它们包装成 `AppError`。对比其他引擎（Tesseract/Windows/公式）都统一抛 `AppError(code=OCR_UNAVAILABLE|OCR_EMPTY)`。后果：Android 上的 OCR 失败会退化成通用 500，丢失 `OCR_UNAVAILABLE` / `OCR_EMPTY` 语义，前端无法区分「没有引擎」与「识别为空」。
- **无重试/退避**。JNI 侧失败不重试；sidecar 侧同样没有重试逻辑（重试/退避按 ADR-0040 属 Python 保留职责，但 OCR 链路也没有）。
- **`TextRecognizer` 从不释放**。`getRecognizer()` 是 `static` 且全生命周期只创建一次，`TextRecognizer.close()` 在 `onDestroy` 中从未被调用，Activity 重建会累积 native 资源。
- **`rust_empty_response` 语义可疑**。HTTP 200 + 空体被判为 `ok=True`，调用方若不额外检查 `body` 可能误判成功。

---

## 7. 资源调度

| 资源 | 现状 | 观察 |
| --- | --- | --- |
| 网络连接 | 每 `(scheme, host, port)` 一个池，默认 32，env `DEEPSEEK_RUST_SIDECAR_MAX_CONNECTIONS` 夹在 1..128；空闲用 `deque`，`release()` 时 `notify()` | 只有 sidecar 有真正意义上的调度器 |
| 进程/线程 | Android：单 Python 进程、单 uvicorn 事件循环；`MainActivity` 仅一个启动线程 | OCR 实质并发度为 1 |
| ML Kit 计算 | 单例 `TextRecognizer`，`ChineseTextRecognizerOptions` | 无并行度；并发上传会在 ML Kit 内部串行排队 |
| 内存（图片） | `MAX_PDF_BITMAP_PIXELS = 6_000_000`，`ARGB_8888` 即每页峰值约 24 MB；`recycle()` 在 finally 中严格执行 | 内存纪律好，但峰值偏高 |
| 磁盘 | PDF 临时文件落在 `appContext.getCacheDir()`，finally 删除，失败退 `deleteOnExit()` | 相对安全 |
| 上传 | 单文件 50 MiB、每请求 20 个、JSON 分支 16 MB | 与事件循环阻塞问题叠加 |
| 进程树纪律 | `assert_zero_python_process_tree()` + `FORBIDDEN_RUNTIME_NAMES` | 只作用于服务器拓扑，不作用于 APK |

调度层面的核心判断：**这套系统里唯一被认真调度的资源是 sidecar 的 HTTP 连接池**。OCR 链路没有任何准入控制、并发限制或队列，靠调用方自觉串行。

---

## 8. 性能优化

**已落地的优化（可验证）：**

- **持久连接替代每调用一次握手**。`transport.py` 模块 docstring 明确定位：只替换 per-call 连接生命周期，保持既有 urllib 风格失败契约。`BufferedResponse` 用 `io.BytesIO` + 自建 `Message` 头模拟 urllib 子集，使客户端不必改写。
- **分段计时**。`GatewayProxyResult` 把耗时拆成 `serialization_us / transport_us / rust_processing_us / total_us`，其中 transport 由 `BufferedResponse.transport_us` 提供，Rust 计算时间由响应头 `X-DeepSeek-Rust-Processing-Us` 回传。这是跨语言性能归因的干净做法——Python 侧不需要猜测 Rust 内部花了多久。
- **紧凑二进制向量传输**。跳过 JSON decode 与候选 list-of-lists 重建；`f64le-v1` BLOB 与 JSON 双写，合法 BLOB 直接拷贝进单次二进制请求；一个 lookup 最多一次 Rust 二进制请求。
- **OCR 侧**：`OCR_UPSCALE_MAX = 3.0` 限制放大倍数避免爆内存；`_ocr_text_score` 择优；`OCR_MODE=fast` 命中即返回；`normalize_ocr_text` 统一后处理。
- **启动期探针**：`dependency_versions()` 先于 `start_json` 执行，失败时把版本信息打进错误视图。
- **Android 内存**：`bitmap.recycle()` 在 finally 中硬保证；PDF 渲染按像素预算等比压回。

**尚缺的优化：**

- OCR 链路**没有任何分段计时**（multipart 解析 / 引擎构造 / JNI 拷贝 / ML Kit 推理 / 后处理），也没有 correlation id。这是与 sidecar 链路最大的观测能力落差：sidecar 一条请求能给出四段耗时加关联 ID，OCR 一条请求只能靠日志拼接。
- Java↔Python 之间的 `byte[]` 全量拷贝没有分块或免拷贝通道。对 50 MiB 上限的文件，这属于可观测的固定成本。
- `image_segments` 会先读 `metadata["ocrText"]`，只有为空且 `ocr_enabled` 才真正跑 OCR——这个短路是正确的，但没有任何命中率指标。

---

## 9. 缺口与风险清单

按严重度排序，便于后续按轮次处理：

| ID | 严重度 | 问题 | 证据 | 影响 |
| --- | --- | --- | --- | --- |
| R1 | 高 | `async def api_media_create` 内同步调用含 OCR 的 `ingest_upload`，未卸载到 threadpool | `web/routes/media.py:26,45-55` | 单次 OCR 最长阻塞整个事件循环 60s，Android 上所有 API 一起挂起 |
| R2 | 高 | JNI 异常未包装为 `AppError`，丢失 `OCR_UNAVAILABLE`/`OCR_EMPTY` 语义 | `ocr.py:1415-1419` vs `AndroidOcrBridge.java:46-47,124,142` | 错误降级为通用 500，前端无法针对性提示 |
| R3 | 中 | 启动调用无超时/取消 | `MainActivity.java:130` | Python 挂起则 UI 永久转圈，无诊断出口 |
| R4 | 中 | OCR 链路无并发限流、无队列、无背压 | 全链路无 semaphore/queue | 20 个文件一次请求可叠加成 20×60s |
| R5 | 中 | OCR 链路缺 correlation id 与分段计时 | `ocr.py` / `media` 层无对应字段 | 无法与 sidecar 同等精度定位慢在哪一段 |
| R6 | 中 | `TextRecognizer` 从不 `close()` | `AndroidOcrBridge.java:133-138` | Activity 重建累积 native 资源 |
| R7 | 低 | `rust_empty_response` 判为 `ok=True` | `gateway_client.py:118-119` | 调用方易误判成功 |
| R8 | 架构 | 「把 OCR 交给 Rust sidecar」与 Android 拓扑、凭据模型、零 Python 不变量三重冲突 | `ARCHITECTURE.md:220`、`ADR-0040`、`process_tree.py` | 直接迁移会导致双实现分叉，见下节 |

---

## 10. 若要真正建立协同：三个选项

结论：**当前不该把 OCR 迁移到 Rust sidecar**。ADR-0040 已经把 `rust_default_on_components` 明确留空，并规定 Python fallback 覆盖整个 4.x、5.0.0 之前不考虑移除。任何 OCR 委托都应排队在这个边界之后。若确需建设，三个选项的边界如下。

### 选项 A（推荐，立即可以做）：不新增委托，只移植 sidecar 的协同纪律

保持 Python 权威不变，把 sidecar 已经验证过的三件事搬到 OCR 链路上：

1. **关联 ID**：给 OCR 请求生成 `X-DeepSeek-Request-ID` 等价标识，贯穿 media route → ingestion → ocr 引擎 → ML Kit 桥 → segments 元数据。
2. **分段计时**：按 `GatewayProxyResult` 的样式拆出 multipart 解析 / 引擎构造 / JNI 拷贝 / ML Kit 推理 / 归一化后处理五段。这不需要任何新协议，只需要字段。
3. **错误分类归一**：在 `AndroidMlKitEngine` 中把 JNI 异常映射为 `AppError(code=OCR_UNAVAILABLE|OCR_EMPTY)`，与其余四个引擎对齐（同时修掉 R2）。

成本最低、不触碰任何 wire 契约、不违反 ADR-0040，且直接消解 R2/R5 两个中高优先级问题。

### 选项 B（后续，需 gate）：新增确定性预处理委托

若要在 OCR 上真正复用 Rust，正确形态不是「把识别搬过去」，而是**对标 `POST /rag/documents/prepare`**，即只委托确定性、可复现的决策：

- 页分割与渲染参数决策、重复页/空白页去重、候选增强参数的确定性计算；
- Rust **不接触**文件字节、路径、凭据，也不持有 ML Kit/API 调用——与「Rust never receives paths, raw file bytes, credentials」这一既有约束保持一致；
- Python 仍拥有真正的识别调用与凭据；
- 采纳规则沿用既有范式：Python 先算本地契约，仅采纳精确等价结果；
- 前置条件：必须先建 parity corpus。参照既有做法——MCP 是 105 例，RAG 文档准备是 125 例，向量二进制是 110 有效 + 16 畸形。没有 corpus 就不该开 flag。

### 选项 C（不建议）：把 Rust 作为服务器侧第 5 个 OCR 引擎候选

形态上插进 `_ocr_engine_candidates`（在 `tesseract` 附近），而不是做成传输层。但必须接受：

- 与 Android 拓扑分叉——设备上没有 Rust 二进制，Android 永远只能用 ML Kit；
- 凭据不能进 Rust，因此只能承载本地模型，无法覆盖 `deepseek-v4-pro` 云端多模态路径；
- 需要新增 wire 契约（`proto/` 目前没有 media/vision 命名空间），而 ADR-0040 明确「默认开启的变更需要后续 ADR 与配套证据」。

除非有实测证据表明 Tesseract 是明确瓶颈且 Rust 实现能带来可量化收益，否则不建议启动。

---

## 11. 一句话总结

这套代码库里，「Rust Sidecar」是一条**默认关闭、只做确定性准备、凭据零转发、Python 始终权威**的 HTTP/JSON 委托通道；「Android OCR」是一条**进程内、经 JNI 越过一次语言边界、被 60 秒 CountDownLatch 拉平为同步**的 ML Kit 链路；「多模态输入」是一条**multipart → segments → RAG 索引**的数据管线。三者共享同一个 Python 进程，但没有共享的传输契约。它们之间最值得先补齐的不是「传输」，而是 sidecar 已经有的那套**关联 ID + 分段计时 + 错误分类**纪律。
