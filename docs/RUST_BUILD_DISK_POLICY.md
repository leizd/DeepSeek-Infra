# Rust 构建磁盘策略

本项目的日常构建使用低磁盘占用配置、固定缓存和磁盘余量保护。Python
脚本仅负责离线构建与验证；产品运行时仍由 Rust/Go 二进制承担。

## GitHub 云端构建

`.github/workflows/rust-cloud.yml` 将大型 Rust 验证交给 GitHub 托管的
Ubuntu 22.04 机器。推送到 `codex/rust-cloud/**` 专用分支触发构建；
该工作流不依赖合并到主分支，现有完整 CI 继续保留。

两个独立云端任务分别执行 Rust 1.85.0 的格式检查、全部目标和特性的
Clippy、完整工作区测试，以及固定版本 `cargo-llvm-cov` 的完整工作区
80% 行覆盖率检查。前端、PDF/OCR 和离线对照依赖与现有 Rust CI 相同。
日志管道启用 Bash `pipefail`，任何检查失败都会使对应任务失败。

`CARGO_HOME` 和 `CARGO_TARGET_DIR` 均位于云端临时磁盘；依赖压缩包与
编译缓存只在云端保存和恢复，每项缓存超过 2 GiB 时不保存，不修改
账号收费或缓存配额。检查与覆盖率使用独立缓存键，键绑定 Rust 版本、
Cargo 配置、锁文件、构建脚本和源码提交。日志和覆盖率报告保留 3 天，
上传清单不包含 `target/` 或依赖缓存，本机不自动下载这些缓存。

云端启动需要实际上传待验证的源码。当前 GitHub 仓库公开；首次推送
前必须审阅源码快照并取得授权。源码清单只包含 Git 可见的项目文件，
排除被忽略的 `.env`、本机运行目录、SDK、工具和历史构建产物。工作流
保存实际云端提交的来源上下文，不能把旧远端提交的成功当作当前未提交
工作树的验证结果。此工作流本身不发布产品，也不证明完整原生迁移已完成。

运行状态和小型日志可通过以下命令查看；不要下载构建缓存：

```powershell
gh run list --repo leizd/DeepSeek-Infra --workflow rust-cloud.yml --branch codex/rust-cloud/20261010
gh run view RUN_ID --repo leizd/DeepSeek-Infra
gh run view RUN_ID --repo leizd/DeepSeek-Infra --log-failed
```

GitHub 官方说明：标准托管机器在公开仓库中免费使用，
[托管机器规格](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)；
[云端依赖缓存及淘汰规则](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)；
[专用分支 push 触发](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#push)。

## 固定缓存位置

当前 Windows 主机将构建缓存固定到
`D:/CodexNativeVerification/shared-cargo-cache`，依赖缓存固定到
`D:/CodexNativeVerification/shared-cargo-home`。原 C 盘依赖缓存、工具
可执行文件和旧构建缓存保留；只复制 `registry/index/`、
`registry/cache/` 和存在时的 `git/db/`，逐文件验证原件与副本的长度
和 SHA-256，未复制凭据或全局配置。Cargo 按需解压 `.crate` 文件，
避免同时复制整份压缩包和已解压源码。依据
[Cargo 官方缓存建议](https://doc.rust-lang.org/cargo/guide/cargo-home.html)。

用户设置保存在 `~/.config/deepseek-infra/rust-build.json`，普通终端和
MSIX 应用读取同一文件，不需要重启应用或重复设置环境变量。所有调用
`cache_environment` 的打包、Android、S3 和覆盖率入口都会读取它。
它不会改变其他 Rust 项目的全局 Cargo 配置。

在项目根目录设置其他有余量的位置：

```powershell
python scripts/rust_build.py --configure-cache-root D:/CodexNativeVerification/shared-cargo-cache --cargo-home D:/CodexNativeVerification/shared-cargo-home
```

配置前检查目标分区的 2 GiB 余量，设置文件用原子替换保存；空间不足
时保留旧设置。此命令创建目录并保存位置，不移动或删除既有缓存。
若依赖缓存为空，联网构建会下载依赖，离线构建需要先准备依赖缓存。
显式 `--target-dir`、`CARGO_TARGET_DIR`、`DEEPSEEK_RUST_CACHE_DIR`
和 `CARGO_HOME` 继续优先于保存的默认值。配置损坏时停止并说明错误，
避免悄悄回到已满分区。常规体积配置依据
[Cargo 官方 profile 文档](https://doc.rust-lang.org/cargo/reference/profiles.html)，
缓存目录和环境变量优先级依据
[Cargo 官方配置文档](https://doc.rust-lang.org/cargo/reference/config.html)。

本次当前源码实测：完整工作区检查通过；两轮真实 HTTP 测试各 30 项
通过。第二轮复用全部 420 个产物，零重新编译，5,154 个缓存文件和
1,417,268,021 字节逻辑长度保持不变。全部文件启用了 NTFS 压缩，
去除硬链接重复后，`GetCompressedFileSizeW` 合计 679,048,650 字节
（约 0.63 GiB），此值不代表包含 NTFS 元数据的整卷分配量。实际 HTTP
测试程序的 COFF 符号表已裁剪，调试断言和溢出检查保持开启。
按需解压后的依赖缓存另有 343,069,627 字节逻辑长度，压缩文件存储量
为 211,999,723 字节。本次构建与依赖文件合计约 0.83 GiB 压缩存储量；
原 C 盘缓存与历史任务缓存继续保留，此数字不包含它们。

66 项相关回归、Ruff 和 Mypy 通过。新版入口仍会拒绝不足 2 GiB 的
显式 C 盘缓存位置，旧缓存文件数和长度不变。当前缓存设置与原依赖
文件的逐文件校验、编译日志、源输入 SHA-256 和完整证据索引见
`artifacts/native-20261010-rust-disk-resolution.json`。修改位于当前隔离
工作树，`D:/deepseek` 主检出保持干净，未自动合并或提交。本机正常
Rust 构建使用上述入口；直接 Cargo 不包含包装器的余量监测。

## 实测原因与处理

Windows GNU 测试程序即使关闭 DWARF，仍可能携带很大的 COFF 符号表和
字符串表。实测 `/api/chat` 测试程序原为 74,578,432 字节，其中约
23.90 MB 是这类表。仅裁剪调试信息减少约 0.77 MB；裁剪普通符号后的
实际 Cargo 产物为 50,674,688 字节，16 项真实 HTTP 测试全部通过。

五套任务缓存通过标准 NTFS 压缩释放 5,847,007,232 字节，所有 29,155
个文件的路径、长度和内容哈希保持一致，删除文件数为零。源码、SDK、
运行数据和验证日志保留。工作树原来的 `rust/target` 历史缓存另有
71.00 GB 逻辑长度，去重后的 `GetCompressedFileSizeW` 总量约 31.70 GB；
它已经全部压缩，仍予保留。这个存储量不包含小文件和簇分配的差异。

## 默认配置

- `dev` 与继承它的 `test`：关闭调试信息和增量编译，裁剪普通符号。
  优化级别仍为 0，调试断言、整数溢出检查和栈展开保持 Cargo 默认值。
- `release`：保留原有 LTO，裁剪符号。
- `diagnostic`：完整调试信息，保留符号，供源码调试使用。
- `coverage`：保留 LLVM 覆盖率映射和符号，关闭 DWARF 调试信息。
  覆盖率的源码位置来自独立嵌入的映射，避免每个测试程序重复携带 DWARF。
  覆盖率仍测完整工作区和全部特性，原始 80% 门槛和生成 Protobuf
  排除规则保持不变。

Rust 的插桩覆盖率在二进制中独立嵌入源码区域映射，详情见
[rustc 覆盖率文档](https://doc.rust-lang.org/rustc/instrument-coverage.html)。

默认产物的源码调试信息减少。需要断点、变量或详细栈信息时，显式选择
`--profile diagnostic`；这个配置需要更多空间。

## 日常命令

在项目根目录执行，Windows GNU 工具链示例：

```powershell
python scripts/rust_build.py --toolchain 1.85.0-x86_64-pc-windows-gnu -- check --workspace --locked
python scripts/rust_build.py --toolchain 1.85.0-x86_64-pc-windows-gnu -- test --workspace --locked
python scripts/rust_build.py --toolchain 1.85.0-x86_64-pc-windows-gnu -- test -p deepseek-policy --lib --profile diagnostic --locked
```

包装器在开工前要求至少 2 GiB 空闲，执行中每秒检查输出分区，保护
1 GiB 余量。低余量时，Windows Job Object 终止本次构建的进程树；
Unix 终止本次构建的进程组。已有缓存和无关程序保留，不自动删除文件。
错误退出码为 86；开工前余量不足则直接拒绝启动编译。

每次构建的 `TMPDIR`、`TEMP`、`TMP` 和 `RUSTC_TMPDIR` 指向该缓存内
独立的 `.deepseek-build-tmp/build-*` 目录，避免编译器和链接器改写另一个
已满分区。只在子进程环境中设置，调用方和其他程序的临时目录保持原样。
成功、失败或超时后清理该次临时目录；并行构建各用各的目录。该目录
若是指向缓存外的符号链接或 junction，包装器拒绝编译。

普通构建默认在子进程中设置 `RUSTUP_AUTO_INSTALL=0`；缺少工具链时
直接报告未安装，不在编译或版本查询过程中隐式下载工具链。安装工具链
应显式执行；如确需在受保护的构建中自动安装，可显式设置
`RUSTUP_AUTO_INSTALL=1`。这个变量的语义见
[rustup 官方文档](https://rust-lang.github.io/rustup/environment-variables.html)。

允许联网的 Cargo 命令同时检查和监测 `CARGO_HOME` 所在分区。显式允许
自动安装时，在调用 Cargo metadata 前检查 `RUSTUP_HOME`，配置解析和
后续编译都监测该分区。这覆盖 `+工具链`、`RUSTUP_TOOLCHAIN` 和目录
工具链选择。`--offline` 或 `CARGO_NET_OFFLINE=true` 跳过不下载依赖的
注册表分区检查，但仍保护输出及显式允许安装工具链的分区。存储根目录
不存在或不可访问时拒绝执行，父目录查找不会在缺失的盘符上无限循环。
这些余量检查是每秒采样保护，不能限制无关程序的写入或任意外部工具的
自定义输出路径；它们不等同于文件系统硬配额。

空间检查跟随 Cargo 实际使用的输出目录。命令中的 `--target-dir`
（包括 `--target-dir=路径`）会覆盖包装器的默认选择；相对输出路径
按编译工作目录解析。额外 `--config` 使用离线、锁定且不编译的真实
Cargo metadata 核对目录，保留所选工具链和配置文件的原有优先级。
无法确定目录时拒绝编译；空间不足的命令行退出码固定为 86，不输出
异常堆栈。测试程序在 `--` 后接收的参数不会被当作 Cargo 参数。
Cargo 配置与路径规则见
[官方构建文档](https://doc.rust-lang.org/cargo/commands/cargo-build.html)和
[官方配置文档](https://doc.rust-lang.org/cargo/reference/config.html)。

Windows 缓存默认位于 `LOCALAPPDATA/DeepSeekInfra/cargo/`，Linux/macOS
位于用户缓存目录，按主机、工具链和 native/Android 分类。MSIX 会将
Windows 用户目录重定向至应用包缓存，因此脚本先创建目录，再解析
实际路径，保证第一次和后续构建使用同一目录。

`CARGO_TARGET_DIR` 或显式 `--target-dir` 优先；`DEEPSEEK_RUST_CACHE_DIR`
可指定默认缓存根目录。跨终端或应用共用缓存时，指定同一个明确路径。
项目不修改其他工程的全局 Cargo 配置。Windows 缓存目录启用标准 NTFS
压缩，新文件继承压缩属性。

相同源目录、工具链、功能和编译参数的重复构建可复用产物。改变源目录
或参数仍可能产生不同的 Cargo 缓存项；源代码快照路径不等价于同一个
工作区路径。当前工作树连续两次真实构建均复用 420 个产物、零重新
编译，缓存保持 3,207 个文件和 1,195,047,632 字节逻辑长度。

## 已接入的构建入口

`build_backup_crypto.py`、`build_android_native.py`、`run_native_s3_e2e.py`
和 `run_rust_coverage.py` 均使用共享缓存和余量保护。Android 缓存不再
随每个输出目录重复创建；备份打包从所选缓存复制二进制到 `bin/`。
覆盖率的测量、LCOV 和测试清单使用同一个 `coverage` 配置。

直接调用 Cargo 仍应用工作区的体积配置。需要余量保护时使用上述入口
或 `rust_build.py`。

## 大型 Linux 验证

本机完整 Rust 验证使用限定大小的可执行 RAM `tmpfs`，将源码和注册表
缓存只读挂载，只持久化日志和覆盖率结果。容器关闭后释放中间产物，
不为每次验证另存大型缓存归档。完整测试实测 3.31 GB 中间产物全部
位于 RAM；它们不会持续累积到 Docker 的 D 盘虚拟磁盘。

全量覆盖率保留插桩映射和符号，当前验证使用 12 GiB 内存盘、14 GiB
容器内存上限、两个编译任务并禁用容器交换空间。2 GiB 启动检查和 1 GiB 运行余量保持不变。
Windows 挂载目录的 Git 检查较慢，验收脚本使用 3,640 个输入文件的
字节相同内存副本；逐文件核对哈希，真实 Git 元数据只读，源代码仍标记
为有未提交修改。传输包约 24.8 MB，仅包含源代码，不含 Cargo 编译缓存。

验证记录位于 `artifacts/native-20261008-rust-cache-compression.json`、
`native-20261009-rust-gui-cache-compression.json`、
`native-20261009-rust-disk-windows-v3.json`、
`native-20261009-rust-disk-ram-gates-v5.json`、
`native-20261009-rust-disk-diagnostic.json` 和对应覆盖率记录。
构建脚本、覆盖率契约、Android/S3 入口及相对路径打包共 44 项测试通过，
记录为 `native-20261009-rust-disk-policy-tests-v7.xml` 与 `-v8.xml`。
完整常规 Rust 测试通过 1,360 项；完整覆盖率为 81.927388%，原始
80% 门槛、1,378 项测试清单及 LCOV 均通过。总表为
`artifacts/native-20261009-rust-disk-policy.json`，记录各项证据的来源和哈希。

较早的级联对话输入 `8548646e6de8` 已通过 1,393 项全部特性测试，并保留
1 项原有忽略测试；完整 1,394 项清单、LCOV、原始 80% 门槛和严格
Clippy 通过，覆盖率为 82.001932%。记录为
`artifacts/native-20261009-chat-cascade-evidence.json`。
输出目录保护的补充回归通过 52 项，包含真实 Cargo 配置解析、相对
目录、运行中的进程树终止、Android/S3 入口和覆盖率契约。当前 Windows
真实对话测试连续两次各通过 30 项，均复用全部 420 个编译产物，缓存
保持 3,207 个文件和 1,196,555,563 字节逻辑长度，零重新编译、零新增
缓存体积。补充总表为
`artifacts/native-20261009-rust-disk-output-routing-evidence.json`。

临时目录和下载分区保护新增 8 个回归用例，相关 60 项用例通过。
第一次组合检查中生成文件计数尚为 16，实际新增 Agent gRPC 绑定使它
成为 17；保留失败日志和修正后的单项通过记录。该轮 418 文件 Rust 输入
`9a2fa7fa4cc0` 通过完整工作区编译，真实 HTTP 路由测试两轮各通过 30 项。
第二轮复用全部 411 个产物，零重新编译；缓存保持 5,067 个文件、
1,233,871,559 字节逻辑长度，实际内存盘占用 1,170,989,056 字节。
各轮完成后的构建临时目录数均为零。该结果是当前源码的编译、既有 HTTP
路由和磁盘策略证据，新增多 Agent 行为及完整生产验收仍须独立验证。
记录为 `artifacts/native-20261009-rust-scratch-ram-v1.json` 和
`native-20261009-rust-scratch-tests-v3.xml`；保留 `-v2.xml` 的初次失败及
`-contract-repair.xml` 的修复记录。

最终补充保护的 64 项相关回归测试全部通过，无跳过；其中使用真实 rustup
和本机受控 HTTP 服务验证两种缺失工具链选择均不发送下载请求，并验证
配置解析过程中磁盘余量降低时终止该次进程树。Ruff 与 Mypy 均通过。
记录为 `artifacts/native-20261009-rust-final-guard-tests-v1.xml`。真实
Windows CLI 在 C 盘只有约 1.64 GiB 空闲时，于 0.46 秒内返回 86，
没有启动编译，也没有异常堆栈；详见 `native-20261009-rust-final-low-space-cli.json`。

按用户已批准的精确名单，清理任务目录
`D:/CodexNativeVerification/native-20261006/platform-cargo-target-20261007/debug/deps`
中的 1,964 个 `.rlib/.rmeta/.o/.d` 文件，逻辑长度 1,210,407,352 字节。
执行前后核对路径边界、文件类型、长度和 SHA-256；未执行递归删除。
D 盘实际新增 631,775,232 字节空闲，同目录其余 55 个文件的 SHA-256
全部保持一致。清理记录为
`artifacts/native-20261009-authorized-task-cache-cleanup.json`。历史压缩
释放量与此次清理合计约 6.03 GiB；它们是不同操作的实测值，不能代替
当前磁盘可用空间。

此前完整 Rust 工作区检查、格式和严格全部目标/特性 Clippy 均通过。
两轮完整全部特性测试各通过 1,394 项，并保留 1 项原有忽略测试。第二轮
复用全部 599 个编译产物，零重新编译；两轮缓存均为 7,951 个文件和
4,176,035,014 字节逻辑长度，增长为零。全部中间产物位于限定大小的
内存盘，未保存 Cargo 缓存归档，每轮遗留构建临时目录数为零。

附带的多 Agent 请求验收发现测试用例误读了原实现的预算语义，初次整合
记录因此保留 `FAIL`。修正只改变验收 example；对保存的原始字节及修改
重建完整旧输入哈希，确认其他 1,172 个输入均未改变。当前 example 的
8 种真实 Go/Rust 请求场景、格式和严格 Clippy 通过，完整编译清单只
重建该 example，复用其余 598 个产物。汇总磁盘策略证据为
`artifacts/native-20261009-rust-final-policy-evidence.json`，原始失败和修复
记录分别保留，磁盘策略通过不等于全产品生产迁移完成。

当前修正了规划器长无效/重复前缀的生产解析后，严格 Clippy、十个真实
Go/Rust HTTP 场景和完整全部特性测试通过（1,394 项通过、1 项原有忽略）。
首次验证副本漏带真实 Vite 产物的失败保留；从未改变的前端输入重建
110 个真实网页产物后，原测试通过，Rust 源码未因该修复而改变。完整
测试复用全部编译产物，零重新编译；内存盘占用保持 3,669,757,952 字节，
没有新增缓存体积。见 `artifacts/native-20261009-agent-prefix-full-test-ui.json`
和 `native-20261009-agent-prefix-freeze-v3.json`。当前完整 80% 覆盖率验证
通过：74,329/91,151 行（81.544909%），全部 15 个 crate、1,395 项清单、
1,394 项通过和 1 项原有忽略，完整 LCOV 保留。共享 cfg(test) 队列锁
修复了 Hub 与 HTTP 测试的并行干扰，生产行为、断言和冻结语料不变。
最终使用 12 GiB 内存盘、14 GiB 内存上限和零交换空间，临时产物与源
副本共 9,221,550,080 字节，容器结束后释放，无缓存归档。此前 10 GiB
内存盘在报告阶段只剩 1.93 GiB，保护按设计拒绝继续；该次记录和并行
测试初次失败均保留。最终证明为
`artifacts/native-20261009-agent-prefix-qualify-v3.json` 及对应覆盖率、清理
记录；2 GiB 启动与 1 GiB 运行余量未放宽。

2026-10-10 的复核未启动新编译，也未删除缓存。保护脚本、覆盖率入口及
保护测试与此前 64 项通过测试的字节保持一致，九份原始证据的哈希未变。
真实 Windows 命令在约 0.13 秒内因余量不足返回 86，没有异常堆栈；
所选缓存执行前后的文件数和逻辑长度一致。当前空闲约 C: 1.35 GiB、
D: 2.73 GiB；历史释放量不代表当前可用空间。新记录为
`artifacts/native-20261010-rust-disk-audit-v2.json`。日常体积控制和每秒余量
检查已生效；历史缓存仍保留，直接运行 Cargo 不包含该包装器的余量保护。

再次复核记录为 `artifacts/native-20261010-rust-disk-audit-v3.json`：保护脚本、
此前 64 项通过回归及九份原始证据的字节仍一致。真实 Windows CLI 在
0.19 秒内因余量不足返回 86，所选缓存的 4,854 个文件及 1,360,702,145
字节逻辑长度保持不变；未启动新编译，也未删除缓存。本次测得 C 盘
约 1.33 GiB、D 盘约 2.59 GiB 空闲，因此该 C 盘构建入口按设计拒绝启动。

这些是本地源码与构建策略证据；全产品生产迁移及精确提交 CI 验收
仍需按 `tasks/native-runtime/` 和 release 契约完成。
