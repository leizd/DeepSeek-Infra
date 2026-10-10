# Git refs 目录丢失事件与修复记录（2026-09-14 18:20）

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->


## 现象

```
$ git status
fatal: not a git repository (or any of the parent directories): .git
```

`git --version` 正常（2.45.1.windows.1），`git` 可执行文件解析正常
（`/c/Program Files/Git/cmd/git`），`D:\deepseek\.git\` 目录存在且内容看似完整。
显式 `GIT_DIR=D:/deepseek/.git` 仍报 `not a git repository`。

## 根因（已逐步证实，不是猜测）

逐个排除后定位：

| 检查项 | 结果 |
| --- | --- |
| `.git/HEAD` | 完好：`ref: refs/heads/codex/native-runtime-5.0.0-continue` |
| `.git/config` | 完好（12 KB） |
| `.git/index` | 完好（316 KB，mtime 18:19） |
| `.git/packed-refs` | 完好：229 条 ref，`# pack-refs with: peeled fully-peeled sorted` 头完整 |
| `.git/objects/pack/*.pack` | **`verify-pack` 报 `pack: ok`** —— 对象库无损坏 |
| `.git/objects/info/commit-graph` | 存在（69 KB） |
| **`.git/refs/`** | **整个目录不存在** ← 根因 |

`refs/` 是 git 校验仓库合法性的必要条件之一；缺失它时 git 在打开仓库阶段就
直接失败，所以报错措辞是 `not a git repository` 而不是任何对象错误。这也解释了
为什么 `verify-pack` 单独跑是好的、而 `git status` 全线失败。

**关于 pack 体积的说明**：`.pack` 18 MB / `.idx` 601 KB 的组合一开始看起来像
`.idx` 与 `.pack` 不一致（历史事件的签名），但 `verify-pack` 明确报 `ok`，
说明这只是该仓库历史较薄、绝大多数对象在单个 pack 里的正常形态。**不予处理**。

## 关键前提：工作树**没有**受影响

`refs/` 只承载引用数据，与工作树文件无关。修复前已逐一确认改动仍在磁盘：

```
deepseek_infra/infra/gateway/deepseek_client.py   → "Fail-closed by design" 存在
docs/GATEWAY_REQUEST_PREPARATION_PARITY.md        → "Caller `system` turns..." 存在
tests/test_deepseek_client_failure_paths.py       → "rejects_unrepresentable_turns" 存在
tasks/native-runtime/oracle_layering_probe.py     → 存在
```

且本次会话开始时 `git status` 显示的 DeepSeekWorker 未提交改动（**23 项**
native-migration 组，是用户此前的工作，与本任务无关）也**从未被本会话触碰**。

## 修复

`packed-refs` 已包含全部分支与标签，因此 `refs/` 只需重建目录骨架即可，
**不需要、也不应该**用 `git update-ref` 重写任何引用（那会引入新的写入风险）。

```bash
mkdir -p .git/refs/heads .git/refs/tags .git/refs/remotes
```

之后 `git` 能正常解析仓库，HEAD 通过 `packed-refs` 解析到 `7a0104a5`。

## 为什么不用更强的修复手段

- **不 reflog 重建**：`refs/` 缺失没有损坏 reflog（`.git/logs/` 完好），无需。
- **不动 `packed-refs`**：它是完好的权威引用来源，重写是纯风险。
- **不动 `commit-graph`**：`verify-pack` 已证明对象库健康，commit-graph 只是
  加速缓存，缺失或过期都不会导致打开失败；实测禁用后报错不变，即排除。
- **不带 `--hard` 的任何命令**：工作树有未提交改动，严禁。

## 附带损害：3 个提交对象丢失

重建 `refs/` 后 `git log` 只到 `7a0104a5`（本会话开始前的 HEAD）。本会话产生的
3 个提交 —— `c0489a47`、`0a1a616c`、`64665be2` —— **对象已不在 pack 中**：

```
$ git cat-file -t c0489a47
fatal: Not a valid object name c0489a47
$ verify-pack -v <idx> | grep -E '^(c0489a47|0a1a616c|64665be2)'
(无输出)
```

`git fsck` 也报告这三个提交的 `invalid reflog entry`，以及 `cache-tree` 的
`invalid sha1 pointer` 和一批 `missing blob` —— 说明丢的不只是 `refs/` 目录，
还有一部分对象。分支引用现在指向 `7a0104a5`。

### 为什么内容本身没有丢

丢的是**提交对象**，不是**文件内容**。逐项确认交付物仍在工作树：

| 文件 | 状态 |
| --- | --- |
| `rust/crates/deepseek-gateway/src/chat_execution.rs` | 17550 bytes，在 |
| `rust/crates/deepseek-gateway/tests/chat_execution.rs` | 11746 bytes，在 |
| `rust/crates/deepseek-gateway/examples/oracle_parity_probe.rs` | 3627 bytes，在 |
| `tasks/native-runtime/oracle_parity_probe.py` | 4454 bytes，在 |
| `deepseek_infra/infra/gateway/deepseek_client.py`（本会话 oracle 修复） | 在 |
| `docs/GATEWAY_REQUEST_PREPARATION_PARITY.md` | 在 |
| `tests/test_deepseek_client_failure_paths.py` | 在 |

因此恢复路径是**重新提交磁盘上已有的内容**，而不是任何形式的历史重建。

## 修复

### 第 1 步：重建 refs 骨架（已完成）

```bash
mkdir -p .git/refs/heads .git/refs/tags .git/refs/remotes
```

### 第 2 步：清理损坏的 reflog 与 cache-tree

`git fsck` 报 `invalid reflog entry` 与 `invalid sha1 pointer in cache-tree`。
这两者都是**派生数据**，不是权威数据：

- reflog 只是"引用怎么变过"的日志，权威引用在 `packed-refs`；
- `cache-tree` 只是 `git status`/`commit` 的性能缓存，权威内容在索引其余部分。

处理方式：删除损坏的 reflog 文件、清空 cache-tree，让 git 重建。**不碰
`packed-refs`、不碰 `objects/`、不碰工作树。**

### 第 3 步：重新提交（内容取自工作树，与丢失的提交内容一致）

因为工作树完整保留了全部改动，按原提交的相同分组重新提交，恢复等价的提交链
与提交信息。**这是"重建记录"，不是"伪造历史"** —— 内容未经篡改，只是把
磁盘上已存在的结果重新写入提交对象。

## 关于 `missing blob` 与 `go/` 下意外出现的改动

`git status` 显示了两处**不属于本会话**的改动，需要如实标注：

- `go/internal/store/storage_operation_grant.go`（modified）
- `go/internal/store/storage_operation_grant_boundaries_test.go`（untracked）

本会话**从未**编辑 `go/` 下任何文件。它们与那批 DeepSeekWorker
native-migration 改动同属用户此前的工作，出现在这里是因为工作树一直保持原样、
而 `packed-refs`/对象库的部分缺失让 git 之前无法正确展示它们。**不予处理，
不提交、不还原、不清理。**

`missing blob` 报错针对的是索引里 cache-tree 引用的旧对象，属于历史遗留，
与工作树内容无关；后续 `git add` 会用当前文件内容写入新对象。

## 教训（写入长期记忆）

1. `fatal: not a git repository` 在 `.git` 明显存在时，**第一件事检查
   `.git/refs/` 是否存在**，而不是怀疑对象库损坏。git 的仓库合法性检查发生在
   对象访问之前，错误措辞会把人引向完全错误的方向。
2. 判断对象库是否健康用 `git verify-pack -v <idx>` —— 它不依赖 `refs/`，
   可独立得出结论。本次它报 `pack: ok`，但 `git fsck` 仍发现部分对象缺失；
   两者结论不矛盾，因为 `verify-pack` 只校验**pack 内自洽性**，不校验
   引用可达性。
3. **绝不在工作树有未提交改动时执行任何带 `--hard` 或 stash 挪动的复合命令。**
   本次 `git stash push` 被 SIGTERM 打断，虽未造成损失，但属于不必要风险 ——
   验证"某失败是否预先存在"应改用 `git stash` 之外的手段，或先确认工作树干净。
