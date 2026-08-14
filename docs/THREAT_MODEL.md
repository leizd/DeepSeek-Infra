# 威胁模型（Threat Model）

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


适用版本：v4.5.0。

定位与信任假设见 [docs/SECURITY.md](SECURITY.md)：个人、本地优先的运行时，运行后端的机器可信，默认只监听 `127.0.0.1`。这一页回答更尖锐的问题：**当模型上下文里混入攻击者可控的内容（网页、文件、工具结果），或本机服务被局域网内他人触达时，每一类威胁由哪段代码挡住、由哪个测试钉住、还剩什么残余风险。**

每条缓解都是仓库里真实存在的实现；想亲手验证，离线跑 `python evals/runners/run_tool_eval.py`（26 个攻防用例，错判即退出码 1）和 `python evals/runners/run_security_corpus.py --strict`（v2.4 版本化攻击 / 良性语料）。

## 威胁清单

### T1 · 网页内容 prompt injection（搜索 / fetch_url 抓回的页面命令模型）

- **路径**：联网搜索上下文与 `fetch_url` 正文进入模型 prompt → 页面里埋「忽略上述指令 / 把密钥发出去 / 调用 forget_memory」。
- **缓解**：
  - 逐段信任打标 + 三类指令扫描（注入 / 密钥外泄 / 工具调用指令）：[context_taint.py](../deepseek_infra/infra/gateway/context_taint.py)，报告进 `diagnostics.contextTaint`；
  - 搜索上下文隔离加固（前置防注入声明 + 红action明确注入行，prompt cache 无损）：`harden_search_context`；
  - 工具结果注入清洗（外部文本字段红action，URL / id / score 保留）：[tool_policy.py](../deepseek_infra/infra/tool_runtime/tool_policy.py) `sanitize_tool_result`；
  - **污染轮升级**：本轮检出注入后，`fetch_url` / `forget_memory` / `suggest_memory` / `create_reminder` 转为待人工确认（`taint_escalated_confirmation`）。
- **测试**：[test_context_taint.py](../tests/test_context_taint.py)（13 项）、[test_tool_policy.py](../tests/test_tool_policy.py) 注入清洗用例、`run_tool_eval.py` sanitize / taint 用例。
- **残余风险**：pattern 族针对明确指令式注入；语义改写类注入无法完全消除（业界同样未解）。v2.4.0 已把版本化对抗语料纳入 CI hard gate，后续仍需持续扩充语料覆盖。

### T2 · 恶意上传文件（超大文件、压缩炸弹、文件内注入指令）

- **缓解**：
  - 资源边界：单文件 200 MB / 请求体 220 MB、流式 multipart、part 数 / header / 字段上限、DOCX / XLSX / PPTX / EPUB 的 ZIP 单条目与总解压上限、`defusedxml` 安全解析（[files.py](../deepseek_infra/infra/rag/files.py)）；
  - 文件名清理与 `fileId` 十六进制校验，杜绝路径穿越形态；
  - 文件内容打 `untrusted_file` 标签并扫描指令；文件上下文块前置确定性 guard 行（跨轮字节稳定，cache 友好）。
- **测试**：[test_files.py](../tests/test_files.py)（上限 / ZIP 炸弹 / 文件名清理）、[test_context_taint.py](../tests/test_context_taint.py)（文件段打标与 guard 行）。
- **残余风险**：解析器本身的实现漏洞依赖上游库修复——CI `security` job 的 `pip-audit` 持续盯依赖 CVE。

### T3 · `fetch_url` SSRF（借模型之手打内网 / 云元数据）

- **缓解**（两道关）：
  1. 策略层静态预判（无需 DNS）：拦 `localhost` / `.local` / `.internal`、字面私网 / 环回 / 链路本地、云元数据 `169.254.169.254`、URL 凭证、非 http(s)（`evaluate_url_safety`）；
  2. 执行层 DNS 解析后的权威校验：解析结果落在私网 / 保留段同样拒绝（[tools.py](../deepseek_infra/infra/tool_runtime/tools.py) `fetch_url`），读取上限 2 MB。
- **测试**：[test_tool_policy.py](../tests/test_tool_policy.py) SSRF 用例、[test_tools.py](../tests/test_tools.py) fetch_url 校验、`run_tool_eval.py` 5 个 SSRF 变体。
- **残余风险**：DNS rebinding 窗口由「解析后按 IP 校验 + 不跟随跨 host 重定向」压缩；Host 头白名单另防浏览器侧 rebinding。

### T4 · 路径越界（fileId / projectId 逃出缓存沙箱）

- **缓解**：策略层 `evaluate_path_safety` 拒 `..`、路径分隔符与非法 id；执行层只接受固定格式十六进制 id，文件读取永远经缓存索引而不是拼路径。生成类工具只写 `.generated/`、下载经 32 位随机 id，模型无法指定磁盘路径。
- **测试**：[test_tool_policy.py](../tests/test_tool_policy.py) 路径用例、[test_files.py](../tests/test_files.py) id 校验、`run_tool_eval.py` traversal 用例。

### T5 · 密钥外泄（凭证被写进长期记忆，或随工具参数发往外部）

- **缓解**：
  - 运行时自身凭证（DeepSeek / Tavily Key、本地 auth token）出现在**任何**工具调用参数里一律硬拒绝（`secret_exfiltration_blocked`，无条件生效）：`arguments_contain_secret`；
  - `suggest_memory` 内容过 `is_sensitive_memory`（API key / 密码 / token / 证件号）命中即拒，记忆建议必须用户确认才落盘；
  - 不可信上下文里的「把密钥发送到…」指令被 taint 扫描标记并触发高危工具升级确认；
  - trace / 队列持久化脱敏 `apiKey` / `tavilyApiKey` / authorization；trace JSON 导出前再次递归脱敏 API Key、auth token、cookie、敏感 URL query，并截断大段私有文本；日志红action URL 里的 `token`；`.env` 被 `.gitignore` / `.dockerignore` / 发布脚本三处排除，CI 跑 `detect-secrets` 防凭证误提交。
- **测试**：[test_context_taint.py](../tests/test_context_taint.py) 凭证外泄用例、[test_tool_policy.py](../tests/test_tool_policy.py) 敏感记忆用例、[test_release.py](../tests/test_release.py) 排除清单、`run_tool_eval.py` `secret_exfiltration_via_url`。

### T6 · 被攻陷 / 幻觉的 Agent 滥用工具（注入得手后的下一步）

- **缓解**（假设某个 worker 已被上下文劫持，限制它能做什么）：
  - **能力切片是单一事实源**：researcher 只有搜索面、coder 只有本地代码 / 文件面、reasoner / critic 无工具，offer 层与执行层两道一致，越权调用在执行期被拒（`capability_denied`）；
  - 未登记工具一律拒绝（`unknown_tool`），模型幻觉不出新能力；
  - 高风险 / 敏感写入工具要求人工确认（`requires_confirmation`），污染轮自动升级；
  - MCP / A2A 对外入口走同一闸门：每个 `tools/call` 过 Tool Policy，A2A 任务在角色 capability 切片内执行，外部 Agent 拿不到超出该角色的工具面；
  - 每条决策写入 append-only 审计日志 `.tool-audit/audit.jsonl`；token 预算与请求调度层限制失控循环的爆炸半径。
- **测试**：[test_tool_policy.py](../tests/test_tool_policy.py) 能力切片用例、[test_mcp.py](../tests/test_mcp.py) 越权调用被拒、[test_a2a.py](../tests/test_a2a.py) capability 切片载荷、`run_tool_eval.py` capability / unknown-tool / 污染升级用例。

### T7 · 外部 MCP server 恶意或失联（v2.2.1，v2.2.2 加固）

- **路径**：用户显式配置 `MCP_CLIENT_ENABLED=1` + `MCP_CLIENT_SERVERS` 后，外部 MCP server 的工具目录进入本地 Agent 工具面；恶意 server 可能伪装 read-only、暴露高风险 schema、返回 prompt injection 文本，或在执行时超时 / 失联。
- **缓解**：
  - **显式配置边界**：默认不连接任何外部 server，只消费 `MCP_CLIENT_SERVERS` 中用户列出的地址；
  - **命名隔离**：外部工具统一命名为 `mcp__<server>__<tool>`，不会覆盖 `web_search`、`python_eval` 等本地工具；
  - **保守 profile**：桥接层不完全信任 server annotations，会结合 schema 字段（url/path/token/secret 等）和描述推断 risk / network / filesystem / requiresApproval；
  - **同一 Tool Policy 闸门**：bridged tool 在 executor 内部防御式执行 policy evaluate，因此 Agent 调用链和 `/mcp tools/call` Hub 调用链都不能绕过 capability、schema、SSRF、路径、敏感写入和人工确认策略；高风险外部工具会返回待确认而非直接执行；
  - **通用参数扫描（v2.2.2）**：`network=True` 外部工具会扫描 `url` / `uri` / `endpoint` / `base_url` / `host` / `domain` 参数做 SSRF 预检查；`filesystem=True` 外部工具会扫描 path/file/filename/directory 等字段，拒绝绝对路径、`..`、`~` 和 Windows 盘符；
  - **远端工具错误不伪装成功（v2.2.2）**：外部 MCP server 返回 `isError=true` 时，本地输出为 `ok=false` / `upstream_tool_error`，审计 `errorType=tool_error`；
  - **不可信结果清洗**：外部 MCP 返回内容默认视为 `external_output`，进入 prompt injection 清洗和 context taint 路径；
  - **失联降级**：外部 server refresh / call 失败不会破坏本地工具目录；执行错误被转成工具级错误，并记录 errorType；
  - **审计**：外部 MCP 审计条目包含 server、tool、bridgedTool、argsHash、policyVerdict、risk、latencyMs、errorType、protocol 和 direction。
- **测试**：[test_mcp.py](../tests/test_mcp.py) 外部桥接用例（profile、命名隔离、策略拒绝、审批、审计、结果清洗、不可用 server 降级、Hub 路径不绕过 policy、远端 `isError=true`、schema refresh、命名碰撞），[test_tool_policy.py](../tests/test_tool_policy.py) 外部 network/filesystem 参数扫描。
- **残余风险**：外部 server 的真实副作用只能由该 server 自身保证；DeepSeek Infra 只能在调用前后做本地策略门控、审计和结果清洗。只应配置可信来源或本机可审计的 MCP server。

### T8 · 无状态 MCP 横向扩展与任务执行（v4.4.1）

- **路径**：攻击者可能从网络探测专用 MCP 入口、构造工作区越界路径或命令注入参数；客户端重试可能重复触发非幂等测试；实例失租后可能迟到写回；Redis 泄露会暴露任务参数和日志。
- **缓解**：
  - **独立鉴权与 Host 边界**：`MCP_AUTH_TOKEN` Bearer token、`MCP_ALLOWED_HOSTS` 和默认回环端口限制入口；Redis 只存在于 Compose 内网；
  - **路径和进程边界**：代码搜索与 pytest 目标都必须位于 `MCP_WORKSPACE_ROOT`；搜索使用固定字符串，pytest 使用独立 argv 和 `shell=false`，并限制输出与超时；
  - **无进程会话**：官方 TypeScript SDK handler 每个请求创建新 `McpServer`，不依赖实例内客户端 Map 或 sticky session；
  - **持久任务状态**：任务、日志、attempts、lease 和幂等索引只写 Redis；Redis 不可达时 readiness 失败，不回退为实例私有状态；
  - **幂等冲突检测**：`start_test_run` 要求幂等键并绑定规范化参数哈希；同键同参返回原任务，同键异参拒绝；
  - **lease/fencing**：认领、续租和完成是 Redis Lua 原子转换；完成必须匹配 owner 与 fencing token，旧实例不能覆盖接管结果；
  - **最小遥测**：OpenTelemetry 的固定 attributes 不记录查询、测试参数或日志正文；异常 span 可能包含错误摘要，Collector 保持私有。
- **测试**：[stateless-mcp/test/](../stateless-mcp/test/) 覆盖认证、Host、路径、幂等、lease/fencing 和遥测；`npm run smoke:failover --prefix stateless-mcp` 在双实例栈中终止 task owner，验证客户端重试、租约恢复和无重复执行；CI 分别以 `stateless-mcp` 和 `stateless-mcp-failover` 固定两层合同。
- **残余风险**：pytest 运行的是仓库代码，不是强隔离沙箱；恶意测试仍能使用容器拥有的文件、网络和环境权限。Redis AOF 与任务日志可能含敏感输出，必须按私有运行数据保护。

### T9 · 备份离机泄露、密码猜测与外部状态漏备（v4.4.2）

- **路径**：明文备份被复制后暴露项目与会话；攻击者篡改密文诱导恢复；密码进入日志/进程参数；“完整备份”静默遗漏 Redis；恢复运行任务触发重复 pytest。
- **缓解**：
  - 完整内部 ZIP 由标准 age v1 scrypt 或 X25519 流式保护，Manifest 和所有工作区元数据都位于认证密文内；
  - Secret Slot 五分钟过期并在消费后清零，helper 通过独立匿名管道读取 Secret；统一解锁错误和内存失败计数限制本地猜测；
  - age 完整认证成功前不解析 ZIP，认证后仍执行路径、展开大小、摘要和 Schema 验证；
  - `strict`/`best-effort` Coverage Manifest 区分可用、遗漏和排除的外部永久数据源；
  - Redis 只导出版本化逻辑 JSONL；generation fence 阻止新任务/认领、允许既有幂等请求安全重试，并以一小时 TTL 防止备份进程崩溃后永久封锁；恢复清空 Lease 并将 queued/running 转为 interrupted。
- **测试**：[test_backup_crypto.py](../tests/test_backup_crypto.py) 覆盖 Secret 生命周期、加密往返、错误 Secret、明文元数据隐藏和覆盖策略；[stateless-mcp/test/task-store.test.ts](../stateless-mcp/test/task-store.test.ts) 覆盖 generation fence、不重放和冲突重映射；Rust helper 单测覆盖密码/X25519 往返与密文篡改拒绝。
- **残余风险**：已解锁 staging 在可信本机上短期为明文；低熵密码仍可能被离线猜测；物理介质安全擦除、Recovery Key 备份和端点外围 TLS/权限由部署者负责。

### T10 · 增量恢复资源耗尽、协议混淆与旧 Writer 迟到提交（v4.4.10）

- **路径**：恶意或损坏的 Delta 可能诱导大范围内存读取、跨协议复用错误 Chunk Map；并行扫描/上传可能突破资源预算；实例退出后旧 Multipart Writer 可能迟到完成对象；性能遥测可能泄露内容摘要。
- **缓解**：
  - Parent Range 与 Payload Chunk 只用最大 1 MiB 缓冲复制；ZIP/age、Chunk、File 与 Merkle 边界逐层验证，正式 Workspace 只接收完整 Verified Tree；
  - Snapshot 显式记录 `fastcdc-gear-v3`，v2 仅作兼容解码；协议升级强制 Full，未知/未来协议 Fail Closed；
  - Python/Rust Engine 输出逐项校验，Native 缺失或异常回退 Python；Worker 数与 `maxInFlightBytes` 同时限流，所有长循环执行取消/租约检查；
  - Multipart 每 Part 耐久记录，恢复先用 `ListParts` 对账，并在 Submit/Complete 前验证 Writer Fence，失租 Writer 不能完成上传；
  - 遥测只记录耗时、引擎、计数与字节规模，不记录 Chunk SHA、文件路径/正文、密码、Identity 或云凭证。
- **测试**：[test_backup_4410_contracts.py](../tests/test_backup_4410_contracts.py) 覆盖有界流式读取、v2/v3 升级、Python/Rust parity、并发预算、计划冻结和 Multipart 恢复/围栏。
- **残余风险**：已认证并展开的恢复 Tree 在可信本机 Staging 中仍是明文；实际磁盘吞吐和云端限流依赖部署环境。跨文件/跨备份云端 Chunk CAS 与 Convergent Encryption 不在本版本范围。

### T11 · 去重索引投毒、概率误判与跨文件引用竞态（v4.4.11）

- **路径**：损坏或半提交的本地 Chunk Index 可能把错误 Range 绑定到新 Delta；Bloom 误报可能被错误当成复用授权；跨文件 Restore 若先删除 Parent 文件会读到缺失或已替换数据；S3 上同 Key 的外国非空对象可能被 Multipart 重试误判为成功。
- **缓解**：
  - Chunk Map 由 Protocol、File Size 和 File SHA-256 内容寻址，Map 元数据与所有连续 Range 必须精确一致；Lineage、Effective Files、Map 与 Ref 在单个 `BEGIN IMMEDIATE` 事务中提交；
  - 任何索引冲突、迁移断链或未知协议都会回滚并写入耐久 stale 标记，下一轮只能 Full；Retention 物理删除后才移除 Snapshot Ref，无引用 Map 才能 GC；
  - Bloom 只排除确定 Miss，任何 Positive 必须继续执行 Immediate Parent 范围内的批量 SQLite `(SHA-256, length)` 精确查询；Bloom、路径和 Hash 均不上传；
  - `incremental-v4` Parent Range 逐段验证，所有 PUT 先从未修改 Parent Tree 写入 Prepared Tree 并校验最终文件摘要，然后才执行 Tombstone 和 Atomic Replace；
  - Multipart 冲突只有目标 Metadata SHA-256 与 Expected Size 同时匹配才收敛；Metadata 缺失或不同返回 `object-integrity-unproven`，Capability Probe 将该 Provider 标为 Scheduled Not Ready。
- **测试**：[test_backup_4411_contracts.py](../tests/test_backup_4411_contracts.py) 覆盖 Effective Ref 继承、Map 单份存储、原子回滚/强制 Full、Immediate Parent 精确查询、Bloom 损坏、文件交换/Parent 删除、Range Restore、Batch Fallback、Legacy Migration/GC 与 Multipart Fail Closed；[test_backup_448_contracts.py](../tests/test_backup_448_contracts.py) 保留 v2/v3 Chain 兼容和真实 Delta Restore 合同。
- **残余风险**：本地 Chunk Index 仍会泄露同一可信工作区内部的内容相等性，必须与 Workspace 数据同级保护；索引损坏会降低复用率或触发 Full，从而增加本地 IO/存储，但不能降低恢复正确性。Convergent Encryption、云端明文 Hash Index、独立 Chunk Objects、跨 Target/Policy 或超越 Immediate Parent 的历史去重仍明确不在范围内。

### T12 · Pack Range 越界、索引膨胀与维护竞态（v4.4.12）

- **路径**：认证后的恶意 Pack Index 可能尝试绝对路径/目录穿越、负数或布尔 Offset、越界/重叠 Range；损坏 Pack 可能让多个逻辑 Blob 共享错误字节；每 Snapshot 完整物化 Workspace 会耗尽本地 SQLite；在 Commit 路径执行大规模 VACUUM 可能阻塞 Scheduler。
- **缓解**：
  - Pack Index 只接受 `payload/packs/*.pack` 相对路径、Schema v1、严格整数、8 字节对齐、声明 Pack 范围内且互不重叠的 Entry；Pack SHA、Blob SHA、File SHA 与 Merkle Root 逐层 Fail Closed；
  - Pack 只包含当前 Snapshot 的新 Payload，不允许引用其他 Snapshot、Target 或 Policy；Index 和所有明文 Hash 只存在于 Age 认证密文与可信本地缓存；
  - Index v3 以不可变 File Version、Full Checkpoint 和 Incremental PUT/DELETE 表达历史；单份 Current View 与单行 Head 在 `BEGIN IMMEDIATE` 中提交，Head/Root 不一致耐久标 stale 并 Force Full；
  - Restore 最多保留四个只读 Pack Handle，Range Copy 使用 1 MiB Buffer；Native Scanner Worker 由预计工作集预算约束，单项失败回退 Python；
  - GC 只在 Retention 已物理删除 Snapshot 后清理无引用 Version/Map；Maintenance 仅在 DB 超过 256 MiB 且空闲页超过 30% 时执行有界 `incremental_vacuum`，不在 Commit 路径完整 VACUUM。
- **测试**：[test_backup_packed_delta_contracts.py](../tests/test_backup_packed_delta_contracts.py) 覆盖 Pack 对齐/滚动、Pack/Blob 篡改、四句柄 LRU、File Version 共享、Head 不一致、GC/Compaction 和 10 万文件增长；[test_backup_s3_http_e2e.py](../tests/test_backup_s3_http_e2e.py) 在真实 HTTP S3-compatible 服务上覆盖 Multipart 中断恢复、Range GET 与 Full + v5 字节级恢复。
- **残余风险**：Pack 与 Index 仍消耗可信本机磁盘，极端 Workspace 的首次 Full 仍与总数据量成正比；本地数据库和解密 staging 应与 Workspace 同级保护。跨 Snapshot Pack、云端 Chunk CAS、Convergent Encryption、WAL 增量与 WebDAV 不在本版本范围。

### T13 · 投影选择篡改、依赖闭包泄漏与远端 Hold 生命周期（v4.4.13）

- **路径**：Restore Session 一旦创建，攻击者可能在 Retry 时悄悄换选不同 Contributor/Project；投影恢复可能遗漏跨文件 Parent 依赖（Support 只物化不落盘）或把 Support 文件误写入 Workspace；选择性物化可能跳过 Merkle 校验；Remote Restore 失败/取消后可能遗留祖先 Hold 造成远端对象被 GC 或长期滞留。
- **缓解**：
  - `selection` 在 Session 创建时冻结并持久化 `selectionDigest`，Retry 改选返回 `409 restore-selection-mismatch`；Federated 交易的 `serverTransactionDigest` 纳入 `selectionDigest`；
  - Projection Planner 先完整应用 F0→I1→…→In 逻辑链并逐层校验 Merkle Root，只有 Payload 平面做选择性物化；`restoreOutputSet` 与 `restoreDependencySet` 严格分离，Support 文件绝不进入 Prepared Final Mutation List，未选中 Contributor 一律不被改动；
  - Metadata Plane 只提取 `manifest.json` / `operations.json` / Pack Index，并只解压所选 Full 条目、所需 Pack 与 Standalone；未使用 Pack 首次使用前才做 Size/SHA 校验；
  - Whole-Age Object 模型下 API/UI 如实上报 `networkSelective: false`，不把选择性物化宣传成网络级 Selective Fetch；
  - 远端祖先 Hold 在 Complete / Abort / Federated 交易前失败时释放，`recovery-required` 时保留；TTL 仍是最终兜底。
- **测试**：[test_backup_projection.py](../tests/test_backup_projection.py) 覆盖选择冻结/依赖闭包/Support 分离/完整逻辑链；[test_backup_remote_restore_projection_e2e.py](../tests/test_backup_remote_restore_projection_e2e.py) 覆盖投影 Round-Trip、Rollback 范围、Hold 生命周期与 Preview；[test_backup_production_remote_restore_e2e.py](../tests/test_backup_production_remote_restore_e2e.py) 在真实 MinIO + 真实 Age Helper 上覆盖生产全链路。
- **残余风险**：网络层仍须下载整条 Whole-Age 密文链（选择性物化不减少下载量）；网络级 Selective Fetch 需要独立加密 Pack 协议，推迟到 4.4.14 及以后。

### T14 · Object Set 元数据泄漏、外来组件与集合 GC（v4.4.14）

- **路径**：对象集让远端观察到 Component 数量/大小；被篡改 Control 可能引用 Receipt 外对象；缺少任一已提交成员会产生不完整恢复；失败 Publisher 的孤儿组件或 Retention 误删共享/Held 成员会破坏可恢复性。
- **缓解**：Receipt/Commit v4 绑定 role-blind ciphertext digest/size 集合、Control digest 与 Receipt digest；Control 内部映射只能解析到该集合，缺失/外来/大小不符全部 Fail Closed。每个 Component 使用 fresh Age randomness，Key 只用 ciphertext SHA-256。Restore Hold 列出每个成员；Retention 标记所有 live、trash-grace 与 active-hold 对象；旧未提交事务只在 orphan grace 后回收。
- **测试**：[test_backup_object_set_contracts.py](../tests/test_backup_object_set_contracts.py) 覆盖独立随机加密、Receipt/Commit v4、Control-first、精确 GET、外来/缺失组件、真实进程退出恢复、Hold 与 Orphan GC；[test_backup_production_remote_restore_e2e.py](../tests/test_backup_production_remote_restore_e2e.py) 在真实 MinIO/Age/三进程链路统计 Payload GET。
- **残余风险**：远端仍能看到 Component count 与 coarse ciphertext size；本版不加 Padding。约 64 MiB 粒度可能把同一恢复边界的无关文件放进同一 Required Component，但不会允许非 Required Component GET，也不会泄露其明文身份。

## 非目标（明确不在防护范围内）

- 运行后端的本机已被攻陷（恶意进程可直接读本地数据目录）；
- 把服务直接暴露公网（设计为本机 / 可信局域网 + 反向代理，见 [docs/DEPLOYMENT.md](DEPLOYMENT.md)）；
- DeepSeek / Tavily 上游服务侧的数据处理；
- 模型输出内容的事实正确性（注入防御 ≠ 幻觉防御）。

## 验证入口汇总

| 验证 | 命令 |
| --- | --- |
| 攻防回归（26 用例，离线） | `python evals/runners/run_tool_eval.py` |
| v2.4 版本化安全语料 | `python evals/runners/run_security_corpus.py --strict` |
| 全量单测（含上述安全测试文件） | `python -m pytest` |
| 依赖 CVE / 静态安全 / 凭证扫描 | CI `security` job（`pip-audit` · `bandit` · `detect-secrets`） |
| 运行中防火墙状态 | `GET /api/taint` · `GET /api/tool-policy` |
| 外部 MCP 工具面核对 | `GET /api/mcp/external/tools` |
| 无状态 MCP 类型/单元合同 | `npm run check --prefix stateless-mcp` |
| 无状态 MCP 双实例故障恢复 | `npm run smoke:failover --prefix stateless-mcp` |
