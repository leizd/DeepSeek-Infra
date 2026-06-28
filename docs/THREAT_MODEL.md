# 威胁模型（Threat Model）

适用版本：v2.5.1。

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
