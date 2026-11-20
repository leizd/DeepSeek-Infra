# 继续 Rust/Go 重构的提示词

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->


把下面整段内容交给下一个 agent。它自带全部上下文，不需要读历史对话。

---

## 任务

继续 DeepSeek Infra 的 Rust/Go 重构。目标是让服务端、桌面客户端、Android、后台任务及所有生产功能最终由 Rust/Go 承载，**生产运行和发行包不含 Python 解释器、Python 服务或 Python fallback，也不能通过另一个进程调用旧 Python 实现来冒充迁移完成**。

前端保留 React + TypeScript + Vite。

## 硬性约束（不可协商）

1. **保留现有功能、用户数据、公开接口和操作流程。** 不得以删除功能、缩减平台支持、改成云端调用或永久保留混合运行时来降低工作量。
2. **不要每完成一个阶段就询问是否继续。** 例行的实现选择自己决定；只在缺少凭证/设备/外部授权，或需要重大产品取舍时才问。
3. **不自动推送、合并、发布或操作真实生产数据。** 先完成可审阅的实现 + 本地验证，再申请具体授权。
4. **不得声称未观察到的 PASS。** 取消的 run、被覆盖的提交、网络失败都长得像结果——只读摘要会骗人。
5. **本地通过 ≠ 发布证据。** 发布需要精确提交的云端 CI + Evidence Assembly。
6. **不得下调覆盖率门槛来让门禁通过。** 找到并修根因。

## 工作位置

| 项 | 值 |
| --- | --- |
| 活动 worktree | `D:\deepseek-worktrees\tip20261106` |
| 分支 | `codex/native-s0-store-ownership-20261109` |
| 本地 HEAD | `c15eea5f02454ac5a83b921934c52e17e85cec58` |
| 远程 HEAD | `3df48a66b66dea9ad677d5c5ea849abecaab7c68`（同树） |
| 根检出 `D:\deepseek` | 分支 `codex/native-runtime-pr/20261014`，HEAD `b0316d09`，**约 184 个刻意保留的未提交文件，不要动** |
| 证据（仓库外） | `D:/deepseek-worktree-evidence/` |

**先读** `tasks/native-runtime/HANDOFF.md`（本会话刚重写，已逐项核实）。它是权威的当前状态，优先于任何记忆或旧摘要。

## 已核实的状态

`ci.yml` run [43816807364](https://github.com/leizd/DeepSeek-Infra/actions/runs/43816807364) @ `d8ea1d5f`：**40 job 全 success，0 failure，0 skip**。

从产物（不是 job 结论）读到的数字：

- Go 语句覆盖：默认 95.010592%（17043/17938），Android 95.004377%（17363/18276），门槛 95.0
- Rust 行覆盖：83.2749%（103967/124848），门槛 80.0
- Python 覆盖：95.59%，门槛 95.0
- fuzz：三个 libFuzzer 目标共 27,023,603 次执行，0 findings

**15/15 发布门禁有证据**，见 `release/native_runtime_gate_evidence_v1.json`（status `PARTIAL`、readiness `NOT_READY`，刻意如此）。

**7/7 零值不变量已实测**（48 样本 / 96 操作），由 `scripts/measure_native_zero_invariants.py` 产出。

**5.0 评估 `release/native_runtime_5_0_evidence_v1.json` 必须保持 `NOT_READY`**：它声明 `release: 5.0.0` 而 `VERSION` 是 `4.8.0`，DELIVERED 路径在此仓库永远无法通过校验（`VERSION_MISMATCH`），且它自己的 `assessment_scope` 写着"仅仓库快照，不是发布证据"。空的 `measurements`/`gates` 和 `exact_head: null` 是设计意图。**不要"修好"它。**

**为什么仍 NOT_READY**：七项零值不变量测的是隔离的 shadow 模式进程，不是生产流量，且这套运行时没有生产部署。这个缺口不是代码或测试能填的。

## 剩余工作（按可完成性排序）

### 1. 路由迁移 —— 最大存量

按**路由注册**口径统计（Python `@router.<verb>("...")` 装饰器、Rust `.route("...")` 调用）。不要用裸字符串 grep：那会匹配测试夹具和辅助常量，把原生侧虚高。

- Python 注册 **106** 条 `/api/workspace/*` 形状，Rust 注册 **27** 条，**79 条未迁移**
- Rust `/api` 形状共 82 条（27 workspace + 55 非 workspace）
- traces **4/4 已迁移**（`/api/traces`、`/api/traces/{id}`、`/api/traces/{id}/export.json`、`/trace/{id}`）。注意 `tests/trace_routes.rs` 里的 `doesnotexist` 路径是测试夹具，不是路由

切片计划见 `tasks/native-runtime/workspace-backup-dr-migration-plan.md`（S0–S8）。当前状态：

| 切片 | 状态 |
| --- | --- |
| S1a | 已云端确认（`e3aa9e22`） |
| S1b | 已云端确认（`2a0f1ef9`），被 trace run 重新确认 |
| S1c | 已云端确认（`rust-cloud.yml` run 42459534356 @ `aed5d99f`） |
| S2a `GET /api/workspace/backup-catalog` | **仅实现+本地验证，无云端 run** |
| S2-prep commit 读取器 | 本次落地，由 40/40 run 验证 |

**S2 三条写路由已在工作区实现（未提交，需先验收）**：

- `POST\|DELETE /api/workspace/backup-catalog/{backup_id}/pin`
- `POST /api/workspace/backups/{backup_id}/scrub`
- `POST /api/workspace/backups/{backup_id}/verify-unlock`

对应文件：`rust/crates/deepseek-gateway/src/backup_catalog_write_routes.rs`（831 行）、
`rust/crates/deepseek-policy/src/backup_writer_lease.rs`（587 行，`TargetWriterLease` 完整协议）、
`rust/crates/deepseek-policy/src/backup_unlock.rs`，测试
`rust/crates/deepseek-gateway/tests/backup_catalog_write_routes.rs`（898 行，4 个用例）与
`tests/test_native_backup_catalog_write_parity.py`、`tests/test_native_backup_writer_lease_parity.py`。

**接手第一件事**：`git status` 确认这批未提交改动，跑上面两个 parity 测试，然后**在云端验证 Rust 编译**（本机不能编译）。它们声称覆盖了 404→409 拒绝语义、方法守卫、Python 链验证、对象集 scrub、租约争用、过期接管。**未经过云端 run 之前不得视为完成。**

### 2. 下一任务：验收并落地 S2 写路由（工作区已有实现）

工作区里有另一条线产出的 S2 三条写路由实现（见上一节文件清单），**尚未提交、尚未云端验证**。按顺序：

1. `git status` 核对范围，确认没有混入无关改动
2. 跑 `tests/test_native_backup_catalog_write_parity.py` 与 `tests/test_native_backup_writer_lease_parity.py`
3. 逐项审阅实现是否真的满足：`_append_entry` 的 8 字段契约、`entryHash` 链、事件文件 `<root>/events/<hash[:2]>/<hash>.json`、`O_EXCL` lease 与 fencing token、423 拒绝、`python_disabled` 下成功而默认模式拒绝且不落盘
4. 推送到精确提交，走云端 `ci.yml` 验证 Rust 编译与全部测试
5. 通过后记入 `migration-matrix.md`，写清被确认的提交

**若审阅发现实现不完整**，按缺口补，不要为了赶进度放行。注意 `deepseek-policy` 里同时存在 `backup_writer_lease.rs`（新，完整协议）与 `deepseek-transfer/src/writer_lease.rs`（旧，189 行纯决策回放）——确认二者关系，避免留下两套 lease 语义。

参考契约（Python 侧）：

- `deepseek_infra/infra/workspace/backup_writer_lease.py` —— 396 行，lease 协议
- `deepseek_infra/infra/workspace/backup_catalog.py`（568 行）—— `_append_entry`、`pin_backup`、`_utc_iso`、`_target_generation`（注意下划线，它调 `backup_publish.latest_commit`）
- `deepseek_infra/infra/workspace/backup_publish.py` —— `latest_commit`、`_create_exclusive`、`_commit_hash`

### 3. 所有权反转

`current_production_authority` 仍是 `python`。`backup_catalog_store` 已加入 `DECLARED_NATIVE_DATA_DOMAINS`（7→8），并用三种运行模式实测了与 Python 的对称性。Go 控制面（policies/targets/DR）仍需对着**真正停写断流的 Python 源**做一次真实晋升。

### 4. 打包与零 Python 部署

- 桌面（WebView2）、Android APK、launcher 打包必须独立运行原生运行时
- `production_artifact_zero_python` 对默认原生**镜像**已通过（容器内检查证明 `PATH` 上没有 `python`/`python3`/`node`/`nodejs`）；但**不**由 release zip 满足——那是 4294 条目、含 1029 个 Python 文件的源码树
- 零 Python 洁净环境下的非零成功工作负载仍待做

## 验证规则（每个切片）

1. 默认模式下 Python-oracle 逐字节对比（status/body/content-type）
2. 写入必须在所有权闸之后；默认模式测**拒绝**，`python_disabled` 下测**成功**
3. 数据切换切片必须交付 export/import/完整性/围栏/恢复/回滚证据，在隔离副本上做，绝不碰真实数据
4. 故障矩阵：进程杀/重启、超时、租约丢失、陈旧 epoch、未知远端效应、恢复/接管。未知效应保持 `EFFECT_UNKNOWN`
5. 云端证据：精确提交、报告/日志产物；失败时保留 prior-failure 记录
6. 每个切片记入 `migration-matrix.md`，写**诚实状态**和**被确认的精确提交**

## 本机限制（务必知道，否则会浪费整轮）

- **本地不能编译 Rust**：`cargo check` 在依赖的 build script 就失败。Rust 改动只能在云端编译验证。缓解：`tests/test_native_workspace_store_cutover.py` 现在断言 Rust 定长数组长度，把一类 `E0308` 变成本地可测的失败。**本轮就有人因为「数组声明 8 个但只放了 7 个元素」一次拖垮 17 个 job。**
- **本地不能跑 Go race 检测器**（`0xc0000139`，缺运行时 DLL，未改动的包同样失败）。`-race` 只能云端验证。
- **Docker、真实 MinIO 拓扑、provider 工作负载都是云端专属。**
- 本地可用：`ruff`、`mypy`（1035 文件）、`scripts/update_docs_language_nav.py --check`（239 文件）、`scripts/check_release_version.py`、`scripts/native_runtime_contract.py --check`、`scripts/production_runtime_inventory.py --check`、`scripts/native_store_writer_gate.py --check`、`gofmt`、`go vet`、`go build`、`go test`（非 race）、`rustfmt`、`cargo metadata --locked --offline`、pytest 子集。

## 推送方式（git 传输在此机损坏）

git HTTPS 报 `schannel: failed to receive handshake`。所有推送走 GitHub API（blob → tree → commit → ref PATCH），脚本见 `HANDOFF.md` 第 8 节，**必须**保留其中的 tree 相等断言。

要点：

- 远程分支 head 是 API 创建的提交，SHA 与本地不同但**树相同**；永远按树匹配，不要按本地 SHA
- `gh` 必须以 `PYTHONIOENCODING=utf-8` 和 `encoding="utf-8", errors="replace"` 运行，否则 API 响应按 `gbk` 解码失败
- 专用分支不会自动触发 `ci.yml`，需 `gh workflow run ci.yml --repo leizd/DeepSeek-Infra --ref <branch>`
- `native-go` 是长尾（拆分后约 70 分钟），读结果前预留完整一轮时间
- 取消被覆盖的 run，别让它跑完——被取消的 run 会把只是被打断的 job 报成 `failure`

## 踩过的坑（每条都花掉过一轮）

1. **读产物，不要读结论。** 绿 job 在本工作中三次实际什么都没测：硬编码的 `informationalOnly: true`、按时钟漂移的覆盖率、吞掉后续所有 flag 的 YAML `#` 注释。
2. **`>-` 块里的 `#` 不是 YAML 注释**，它被折进 shell 命令，截断其后一切。测试现在断言**解析后**的命令串。
3. **任何记录摘要的文件都要钉 `eol=lf`。** 工作区 CRLF、仓库 LF，记录的 SHA-256 只在一个平台成立。
4. **固定 sleep 不是同步。** `time.Sleep(4300ms)` 会悄悄变成负载相关；改成条件等待，并用 8 个满载进程验证。
5. **共享夹具要加锁**，如果驱动 goroutine 写、测试读——否则 `-race` 有权判它失败。
6. **不要为了让门禁通过而下调门槛。** 两次 Android 抖动都定位到各自一条语句并从源头修好。

## 期望的回报格式

每轮说明：做了什么、验证证明了什么（附产物路径/run URL/精确提交）、还缺什么、下一步是什么。

**不要**用代码行数、文件数、主观百分比作为验收依据。目标未达成时明确写「未完成」并给出具体剩余项。

---

## 附：本次会话的关键教训（供参考）

**我犯过并修正的错误**，下一个 agent 可以直接避开：

- 把 `DECLARED_NATIVE_DATA_DOMAINS` 长度改成 8 却只加了注释、没加元素 → `E0308`，17 个 job 失败
- 添加 `recordCall` 辅助函数时把它自己写成了无限递归，真正的调用点仍是裸 append → CI 报 4~6 条 DATA RACE
- 把 race 失败误诊为"4 并行 CPU 争抢"并回退到 2；第二次在 2 并行下仍失败，证明真实原因是测试依赖空闲 CPU
- 交接文档初稿有三处数字未核实：S2a 被夸大成"已云端确认"、路由计数用裸字符串匹配虚高、非 workspace 路由数偏差 17 条
- 两次网络故障导致推送失败、误派发到旧提交

**这些的共同点**：都是"看起来完成"而不是"实际完成"。读产物、跑脚本重算、验证断言真的会失败——这是唯一可靠的判据。
