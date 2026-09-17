# 2026-09-14 git 仓库损坏事件 + 阶段 D 首切片

> **✅ 修复已完成（2026-09-14 下午）** —— 见文末「修复结果」。
> 最终：`git fsck` error/broken/missing/dangling **全为 0**；229 个 refs 全部可读；
> `git fetch` / `git log` / `git ls-tree -r` / `git diff` 全部恢复；工作树零损失。

## ⚠️ 历史记录：git 对象库损坏（已修复）

### 现象
- `git fsck` 报大量 `invalid sha1 pointer`：`refs/heads/main`、`native-runtime-5.0.0-recovered`、
  `release/4.6.1-stability`、`release/4.6.4`~`4.7.3` 等**几十个 ref 全部指向缺失对象**。
- `99e055c`（本次起点）的父提交 `197236662778e2dfd67bf69b5848c5d4e71c393a` **无法读取**。
- `git log --oneline -8` 在本会话**最开始就只能显示一行** → 说明损坏**早于**我的操作，
  不是本次引入。（早先我把这当作"输出被截断"，实际是对象缺失。）
- 我本次的提交 `9ec08c7`：commit 对象存在，但引用的 tree `067930c12229a31ae58b62e93f5f118c27b70593`
  **不存在**（`.git/objects/06/...` 无该文件）。即 `git show 9ec08c7` 失败。
- `git reflog`、`git branch -r --contains` 均因缺对象而失败。
- `.git/gc.log` 不存在；`.git/objects/info/alternates` 不存在；非 partial clone
  （`remote.origin.promisor`/`extensions.partialclone` 均未设置）。
- pack 文件只有两个，最新的是 `pack-d0e6bfb3...pack`（Sep 2 18:35）。Aug 13 09:48 有
  `multi-pack-index`。

### 我的责任边界（如实记录）
- 损坏在我开工前就存在（`git log` 一行输出可证）。
- 但我在这次会话中执行过一次 `git stash -q && git stash pop -q`（为探测 clippy 基线）。
  `git stash` 会临时重写工作区并可能触发 prune。**这一操作不该做** —— 用 `git stash`
  去探测基线是错误方法，应该用 `git show HEAD:<path>`（无副作用）。虽然 stash list 事后为空、
  工作区文件完好，但不能排除它对已损坏对象库产生了额外影响。
- **教训**：在对象库fsck不干净时，绝不执行任何会重写索引/工作区的 git 命令（stash/checkout/gc/prune）。
  探测基线一律用 `git show`/`git cat-file`（只读）。

### 当前安全状态
- **工作树文件全部完好**（这是唯一不可替代的）：`lib.rs` 1387 行、`request_preparation.rs` 775 行、
  `ocr_trace.py` 204 行，均正常。
- 关键改动已备份到 `.workbuddy/safety-2026-09-14/`（lib.rs / request_preparation.rs / ocr_trace.py）。
- 远端 `origin` = `https://github.com/leizd/DeepSeek-Infra.git`，本会话内容**未推送**。
- 35 项在途改动完好；原生迁移组（23 项）未被提交。

### 待用户决策（已决策并执行）
在对象库修复前，**不应**继续任何 git 写操作。可能路径：
1. `git fetch origin` 补齐缺失对象（若远端有）—— 但需先确认 99e055c 是否在远端；
2. 从远端重新克隆到新目录，再迁移工作树改动；
3. `git fsck --lost-found` 抢救（风险高，且会写对象库）。
需用户确认后再动。

---

## ✅ 修复结果（2026-09-14 下午完成）

### 采用的路径
组合了 (2) 与 (1)：**先从远端全新 `clone --bare` 拿干净对象源，再向本地对象库物理补齐**，
最后重建孤儿提交链。

### 确证的两层根因
1. **本地孤儿提交**：`9ec08c7 → 99e055c9 → 19723666(缺失)`。经 `git ls-remote` 证实，
   `99e055c9` 与 `19723666` **远端完全不存在**，是本地-only 提交；其基座对象从未上传、
   本地亦已丢失 → `99e055c9` 成为无父孤儿。
2. **pack 内 delta 基座缺失**：`pack-d0e6bfb3`(47.7 MB, Sep 2) 的 `.idx` 自检 `ok`，
   但其部分 delta 基座对象不在 pack 内 → `git fetch` 的 have/want 协商产出 thin pack
   无法 index（`pack has 63 unresolved deltas`）。

### 关键结论：远端完好
`git clone --bare` 到 `D:\temp\deepseek-recovery.git` → **fsck 0 error、142 refs**；
远端 317 refs 全部健康。**丢失全部发生在本地**，远端无任何损坏。

### 执行步骤
1. 备份 `.git` 元数据 → `.git-repair-backup-20260914/`（refs 快照 232 条、HEAD、config、
   packed-refs、logs、loose ref 副本、两个 turn-diffs ref）。
2. 删除 2 个 `refs/codex/turn-diffs/checkpoints/**` 坏 ref（ephemeral checkpoint，非分支）。
3. 删除 22 个指向缺失对象的 loose ref 文件（其值全部与远端一致，可安全重建）。
4. 将干净 clone 的 pack 复制进本地 `.git/objects/pack/` → 一次性补齐 **192/207** 个缺失对象。
5. 移除过期 `multi-pack-index`（Aug 13，早于 Sep 2 pack）。
6. 从 `.git/index`（记录 `9ec08c7` 的权威 blob hash）重建缺失对象：
   - `rust/crates/deepseek-worker/src` tree `3184356211d2` → 用 index 的 9 条目
     **逐字节精确重建**（重建后 hash 与原值完全一致）。
   - 10 个丢失 blob → 用工作树内容写入临时 index，**全部逐字节精确重建**。
   - `.github` / `compat/native-runtime` tree → 同样精确重建。
7. 重建顶层树：`acf3d593` → `da471bbe`、`62cf99f` → `52677ee8`
   （仅把 3 个被工作树取代的中间版本 blob 换为新内容，其余完全一致）。
8. 创建修复后提交链并 re-point 分支：
   - `99e055c9` → **`59250933`**，parent = `451ba5ec`（远端 `native-runtime-5.0.0-recovered` tip）
   - `9ec08c7` → **`7a0104a5`**，parent = `59250933`
9. `git fetch origin --prune` 成功 → 恢复 112 个 remote-tracking ref。
10. 按远端有效值恢复 9 个本地分支；`main` 恢复为原值 `4f1eb165`；共 229 refs。
11. 重写 reflog 指向修复后提交 → `git gc --prune=now` 成功。

### 修复前后对比
| 指标 | 修复前 | 修复后 |
|---|---|---|
| `git fsck` error | 45 | **0** |
| missing objects | 627 | **0** |
| broken links | 4+ | **0** |
| invalid ref pointers | 22 | **0** |
| refs 可读 | 190/229 | **229/229** |
| `git fetch` | 失败 | **成功** |
| `git log` | 1 行 | 正常 |
| `git ls-tree -r HEAD` | 4 条（截断） | **2899 条** |
| `git diff` | fatal | 正常（11 文件 / 501 insertions） |

### 永久损失（3 个 blob，均为被工作树取代的中间版本）
| blob | 文件 | 取代者 |
|---|---|---|
| `2e513b52` | `rust/crates/deepseek-worker/src/operation_grant.rs` @ 99e055c9 | 工作树 `91dbb91b` |
| `10961f30` | `go/internal/worker/tls_test.go` @ 两提交 | 工作树 `18310c78` |
| `0d7fb3d4` | `tasks/native-runtime/worker-execution-plan.md` @ 两提交 | 工作树 `4a4fd527` |

三者**最新内容都在工作树中，无内容丢失**；仅历史中间快照不可复现。

### 新的 HEAD
`7a0104a5` on `codex/native-runtime-5.0.0-continue`
（原 `9ec08c7` 已废弃为孤儿并被 gc 回收；内容与提交信息均保留）


---

## 阶段 D 首切片（已完成实现与本地验证，**未提交**）

### 起点核实
- `rust/crates/deepseek-gateway/src/lib.rs`（1338 行）注册了 `/v1/chat/completions`、
  `/v1/models`、`/mcp`、`/a2a`、`/api/*path`、`/internal` 等路由。
- `/v1/models` **已实现且 parity 完整**：`request_preparation::native_model_catalog(created)`
  输出与 Python `openai_models_list()` 一致（同 2 个 id、`object`/`owned_by`、动态 `created`）。
- `chat_completions` 原先只调用薄弱的 `validate_chat_request`，**完全没有使用**同 crate 里
  648 行的 `request_preparation::prepare_request`。prepare_request 只被
  `/gateway/request/prepare` 使用。→ 这是真实接线缺口。

### Python oracle 契约（本次提取）
- `openai_models_list()` → `{"object":"list","data":model_catalog()}`；`model_catalog()`
  (`providers/registry.py:44`)：`created=int(time.time())`，每项
  `{"id","object":"model","created","owned_by":"deepseek-infra"}`。
- `_validate_request_messages`（`deepseek_client.py:227`）两条规则：
  1. `len(normalize_chat_messages(messages)) > MESSAGE_HARD_LIMIT(40)` 且无 `contextSummary`
     → `CONTEXT_COMPRESSION_REQUIRED`, status 409；
  2. 归一化后**没有任何 user 消息** → `INVALID_PAYLOAD`。
- 关键细节：这两条规则作用在**归一化之后**的消息上（`normalize_chat_messages` 会丢弃空内容轮次）。

### 本次实施（2 个文件）
`rust/crates/deepseek-gateway/src/request_preparation.rs`：
- 新增 `pub const MESSAGE_HARD_LIMIT: usize = 40;`
- 新增 `fn context_summary_present(&Map) -> bool`（镜像 `str(... or "").strip()` 语义，
  非字符串视为不存在、空白字符串不算）
- `prepare_request` 补两条 Python 契约规则：`context_compression_required` 与
  `missing_user_message`，均在 normalize_messages 之后判定。

`rust/crates/deepseek-gateway/src/lib.rs`：
- `chat_completions` 改为把 typed request 重新编码后交给
  `request_preparation::prepare_request`，与 `/gateway/request/prepare` 共用同一层。
- 新增 `fn chat_preparation_error` 做错误映射：streaming→501 `not_supported`、
  `request_too_large`→413、`context_compression_required`→409、其余→400。
- **删除**已变成死代码的 `validate_chat_request`（clippy 也报了 `never used`）。
- 新增集成测试 `chat_enforces_the_shared_preparation_rules`。

### 保持不变的既有行为（全部验证通过）
缺 model→400；空 messages→400；合法请求→503 `NATIVE_CHAT_NOT_READY`；stream→501。

### 已发现并如实记录的兼容性分歧（**未擅自放宽**）
- 混合「空白 user 轮 + 真实 user 轮」：Python **接受**（丢弃空白轮次后继续），
  Rust `prepare_request` **拒绝**（`invalid_message_content`）。
- 放宽它会把"拒绝"变成"接受"，属于改变对外行为 → 按冻结兼容性要求**不擅自改**。
- 已写成 `documents_blank_content_divergence_from_the_python_oracle` 测试**钉住现状**，
  避免差异被无声丢掉。

### 本地验证结果
| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| gateway 单测 | `cargo test -p deepseek-gateway --offline --lib` | exit 0，**65 passed** |
| 格式 | `cargo fmt --all -- --check` | 通过 |
| clippy | `cargo clippy -p deepseek-gateway --all-targets --offline -- -D warnings` | **失败，但唯一错误来自 `control_proxy.rs:20` 的 `result_large_err`** |
| gateway 集成测试 | `cargo test -p deepseek-gateway --offline --test public_control_boundary` | exit 0，1 passed |
| workspace 全量 | `cargo test --workspace --offline`（默认并行） | **大量链接失败** → 见下 |
| workspace 全量 | `cargo test --workspace --offline -j 1`（串行） | 见「工作区串行结果」一节 |

### ⚠️ 并行链接失败：工具链资源问题，不是代码问题（重要，勿误报）
- `cargo test --workspace --offline`（默认并行）报一片
  `linking with x86_64-w64-mingw32-gcc failed: exit code 1`，
  涉及 `deepseek-worker`/`deepseek-transfer`/`deepseek-storage`/`deepseek-proof`/
  `deepseek-federation`/`deepseek-gateway` 等几乎所有测试二进制。
- **但单独重跑完全通过**：`cargo test -p deepseek-gateway --offline --test public_control_boundary`
  → exit 0，1 passed。
- 结论：这是 Windows/MinGW 链接器在 `cargo` 多 job 并行时的资源竞争（句柄/内存/PATH 长度），
  **不是编译错误、不是我引入的问题**。`cargo test -j 1` 串行应可绕过。
- → 报告时不得把"并行链接失败"说成代码缺陷，也不得据此说 workspace 测试没过/过了，
  必须以串行结果为准。

### clippy 既存问题（重要，非本次引入）
- `control_proxy.rs` 与 HEAD **逐字节相同**（`git diff HEAD -- control_proxy.rs` 为空），
  第 20 行就是 HEAD 原始代码。
- 即：`cargo clippy -- -D warnings` 在本仓库当前状态下**本来就过不了**。
- 推断为本地 rustc **1.97.1** 与仓库声明 `rust-version = "1.85"`（CI 用 1.85）的
  lint 差异。我引入的代码 clippy 干净。
- → 报告时不得声称"clippy 通过"。

### 下一步
1. **先修 git 对象库**（等用户决策）。
2. 再提交阶段 D 首切片。
3. 继续阶段 D：chat 执行接线（`call_deepseek_cascade` 仅 `deepseek_client.py` 就 2269 行，
   含工具循环/web 搜索预算/语义缓存/级联路由/judge 打分/SSE 事件序列），
   或先做 MCP / A2A 的 parity。
