# Rust、Go 和 Docker 云端验证

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

本任务在 GitHub Actions 上编译、测试 Rust/Go，以及构建、启动 Docker 验证容器。
本机保留源码、已安装的工具和小型报告。日常源码修改、格式和类型检查仍可在本机完成。

Go 与 Docker 使用已有的 `.github/workflows/ci.yml`。它包含 Go 格式、vet、测试、
race、默认与 Android 变体的原有 95% 覆盖率门槛，以及实际 Go/Rust 进程边界。
Docker 作业构建生产、Rust 和 MCP 镜像，验证 Compose、实际容器与五实例 MinIO 测试。
Rust 使用已有的 `rust-cloud.yml`，保留完整工作区检查和原有 80% 覆盖率门槛。

`scripts/verify_native_default_bootstrap.py` 另外验证实际生产启动器从空数据目录启动，
通过公共 Agent API 暂停确认计划、重启整条启动链，再完成依赖任务和综合回答。
该验收不预置控制库，不调用离线签名初始化工具，不手动提供签名或私有 TLS 配置；
Python 只充当离线受控模型服务与结果检查器。首次测试提交的生产启动实现保持原样，
必须收集实际公共 API 的语义失败后再补齐默认初始化。编译或夹具失败不算该项 RED。
通过此项也不代表真实供应商、Android worker、全部功能或完整迁移已获验收。

完整 Go race 的 `store` 包使用同一份 `-race` 构建的测试二进制，先列出实际
Test、普通模式的 Fuzz 种子测试和 Example，再分成四组、最多并发两组执行。
每个名称只属于一组，使用锚定选择器并强制 `-test.count=1`；其他包仍执行
不带筛选的 `go test -race -json`。逐项检查父测试、子测试和包的结束事件，
拒绝遗漏、重复、缓存结果、失败或新增跳过，保留逐组原始日志与二进制 SHA256。
`tests/native/go_race_baseline_v1.json` 绑定此前精确提交的通过记录，要求原有
默认1339项与 Android1323项顶层用例仍在，并显式保留唯一原有的向量生成器跳过。
这些计数包含该跳过，不能全部表述为通过。

该分组解决单个包累计运行耗尽90分钟的问题，不增大 Go 的单次测试调用时限：
每组仍为90分钟，并为每个完整变体设置150分钟墙钟预算。它改变了原先整包的
时间预算边界；完整分组结果和原有95%覆盖率分别验收，不能用部分组通过代替全量。
编译二进制和缓存仅留在云端临时目录，下载范围仍只有报告。JSON framing 使用
Go 官方 [`test2json`](https://pkg.go.dev/cmd/test2json) 的独立测试二进制模式。

专用分支不会自动触发主 CI；在已经审阅、授权上传的快照上手动启动：

```powershell
gh workflow run ci.yml --repo leizd/DeepSeek-Infra --ref codex/rust-cloud/20261010
gh run list --repo leizd/DeepSeek-Infra --workflow ci.yml --branch codex/rust-cloud/20261010 --limit 3 --json databaseId,headSha,status,conclusion,url
gh run watch <run-id> --repo leizd/DeepSeek-Infra --exit-status --interval 60
```

云端运行使用远端提交。未上传的修改必须先冻结、审阅并取得具体上传授权；
核对运行的 `headSha` 与该快照一致，再把通过的结果用于当前源码。
保留失败作业和精确提交证据；Go/Docker 的通过不能替代其他发布门槛。

2026-10-11 已授权的 Go 到期测试修复已上传为
`9dd2ea89eecc19b93b2afbd69b412b61d85601a7`，树为
`a5dbe4d362b30f2c0e785843499e27b952c3fd24`。
[Go/Docker 云端运行 38101509722](https://github.com/leizd/DeepSeek-Infra/actions/runs/38101509722)
在该精确提交上完成，14 项 Go/Docker 作业全部通过，包括默认和 Android 的
普通测试、race、Android 兼容 SQLite 检查及实际原生进程边界。默认覆盖率为
10277/10816 = 95.016642%，Android 为 10103/10626 = 95.078110%，
两者均达到原有 95% 门槛。只取回 1,259,118 字节报告，源码匹配与结果记录于
`artifacts/native-20261011-go-docker-cloud-retry/cloud-evidence.json`。
整条 CI 仍有 6 项其他失败；先前运行 `38098810886` 的 race 失败记录保留。

编译缓存、下载的依赖、Docker 镜像层和验证卷位于云端 Runner。
没有配置持久化云端缓存的作业会在 Runner 结束时释放临时产物。
收集时只下载覆盖率文件、作业状态和小型证据；不下载 Go/Cargo 缓存、镜像归档或卷。
用户已经授权清理迁移后不再需要的本机缓存。2026-10-11 清理了两个专用 Go 缓存目录、
1354 个项目归档、43 个停止的任务容器、14 个任务镜像和 64 个选定构建记录，
清空两个任务编译缓存卷；没有执行 system/volume prune。
全部 25 个卷对象和 23 个保留卷内容均已校验保留，3574 个源码文件及 Git 状态一致。
标准离线压缩后，观察到 C: 新增约 1.7 GiB、D: 新增约 73.6 GiB 可用空间。
34 个项目归档（约 18.3 MiB）的补充删除被自动审批拒绝，保留精确名单；
21 个引用相关的任务构建记录也保留，避免影响其他缓存依赖。
Docker 已恢复关闭状态。详见 `artifacts/native-20261011-go-docker-cloud/cleanup-summary.json`。
云端运行不会自动移动用户的旧数据，也不需要取回编译缓存或镜像。

执行位置与原始证据记录在
`artifacts/native-20261011-go-docker-cloud/{policy.json,dispatch.json}`。
全项目迁移继续保留 `NOT_READY`，直到实际工作负载、恢复、平台和发布证据全部满足要求。

依据：[GitHub Go 验证文档](https://docs.github.com/en/actions/tutorials/build-and-test-code/go)，
[GitHub 依赖缓存文档](https://docs.github.com/en/actions/concepts/workflows-and-actions/dependency-caching)，
[Docker 的 GitHub Actions 指南](https://docs.docker.com/guides/gha/)。

## Android 构建和模拟器验收

[Native Android cloud workflow](../.github/workflows/native-android-cloud.yml)
在已上传的 `codex/rust-cloud/android-platform-*` 分支 push、main 的 PR 候选或手动触发时执行。
独立记录 APK 源码提交、事件、ref、run/attempt 和两份 APK 的 SHA256；源码 push 与 PR 合并候选分别验收。
Go、Rust、Java/Gradle、NDK 和两 ABI APK 构建，以及 API24/API34 x86_64 模拟器都在云端。
APK 仅在同次云端作业间传递，本机只收集 JSON/日志，不下载 APK、编译缓存或模拟器镜像。

设备验收安装隔离的 debug applicationId，清空该云端测试沙盒，运行源码中全部七个现有 Java instrumentation 方法。
[报告校验器](../scripts/verify_native_android_cloud.py) 逐一要求方法开始和成功结束，拒绝跳过、重复、缺失或只有成功摘要的日志。
构建检查还核对两 ABI 载荷、产品资产哈希和 APK 中无 Python 文件。

当前 Android 包只有 Rust gateway 和 Go shadow control，尚无 Rust Agent worker。
七项现有 skills/files/OCR/持久化/启动重启测试即使全绿，也不证明生产权威、完整 Agent、成功工作负载零 Python、ARM64 设备执行或发行资格。
这些资格位在报告中保持 false；整个迁移的所有平台、功能和原始 Evidence 门槛仍须完成。

2026-10-19 的精确源码 `286bd0e1` 已构建两 ABI APK，并在 API34 完成七项仪器测试；
API24 的原生进程就绪失败，整组设备验收仍失败。仅有 logcat 和 Java 超时堆栈不足以定位子进程退出原因。
后续运行在确认源码/APK并成功重置隔离 debug 沙盒后，额外读取该沙盒
`.native-runtime-logs/{gateway,control,worker}.log` 的末尾各最多 128 KiB；缺失日志明确标记不可用。
源绑定失败时不读取应用私有目录。APK、数据库、凭据、缓存和镜像均不下载。

[Control RPC development workflow](../.github/workflows/control-rpc-development.yml)
供 config/lifecycle 的选定回归在云端快速运行 race 检查；报告绑定精确提交并核对四组测试确实执行、未跳过。
它不是完整 Go 或覆盖率资格；既有37项源码 CI、原始95%Go/Python及80%Rust门槛和各平台验收仍全部保留。


## API24 SQLite 连接诊断

精确7d4497af / Android39251758715再次在API34通过全部七项，在API24失败。
子进程stderr首次证明Go control反复拒绝SQLite连接，消息为
`sqlite3: invalid _pragma: sqlite3: disk I/O error`；此时不能把Rust linker的兼容警告当作退出根因。
连接诊断识别SQLite错误对象、主错误码值和扩展错误码值，保留实际extended code。
只有原始错误链包含OS原因时才记录errno数值；缺失OS证据明确为`errno=UNAVAILABLE`。
原错误链、连接拒绝及所有持久化配置不变。
不改变WAL、同步、文件锁、控制schema或写入权，也不重试未知远端副作用。

设备报告只增加`uname -r/-m`两项固定内核事实，用于区分运行内核和VFS失败位置；
这些不是成功工作负载或平台验收。缺失内核事实明确标记不可用。
调试沙盒日志改用带远端退出状态的`adb shell -T`；`exec-out`可把缺失文件的错误作为成功传输输出，
因此旧7d报告中71字节的worker错误文本虽标记COLLECTED，不能证明worker存在。
当前Android仍缺Rust worker，所有生产/ARM64/完整迁移资格保持false。

参考：[固定SQLite VFS实现](https://github.com/ncruces/go-sqlite3/blob/v0.35.6/vfs/README.md)
与[ADB shell选项](https://android.googlesource.com/platform/packages/modules/adb/+/refs/heads/main/docs/user/adb.1.md)。
ad7131b9的实际API24镜像报告内核`3.10.0+`，API34报告Linux6.1，缺失worker日志已正确标记不可用。
VFS文档要求OFD锁依赖Linux3.15且不同锁族并发访问不兼容；旧内核兼容性是有源码依据的线索，
还不是实际失败syscall的观测。不能直接换锁族、降低SDK或跳过API24来取得绿色结果。

固定版本的[连接实现](https://github.com/ncruces/go-sqlite3/blob/091633ebb62b0c78988d3e079149333b36337599/conn.go)
以`%w`保留pragma错误链；没有附加消息或OS信息时可以返回错误码值。
[错误类型实现](https://github.com/ncruces/go-sqlite3/blob/091633ebb62b0c78988d3e079149333b36337599/error.go)
允许三种表示通过`errors.As`转换为`ExtendedErrorCode`。因此已撤回上游字符串抹除错误链的推断。
fc5ef9c4/开发运行39350633836实际复现了旧诊断漏识别三条错误码值路径，其原有检查通过。
修复后仍须核对新提交的Go回归、原覆盖率和真实设备报告，不能把错误诊断改进当作API24修复。

## Android database locking on older kernels

The APK and Linux Android-tagged Go gates use the pinned driver's official
`sqlite3_flock` build variant, together with `deepseek_android`. The same
WAL/shared-memory support, busy timeout, defensive configuration, `query_only`
cache, schema, writer lease and coverage floors remain required. There is no
no-lock/dot-lock fallback and the minimum Android API remains 24. The preceding
real API24 run returned `SQLITE_IOERR_RDLOCK` (2314) on Linux 3.10.0+; the
upstream default OFD implementation requires Linux 3.15. The original driver's
OS errno was unavailable, so do not invent one from the kernel version.

BSD locks are not compatible with concurrent POSIX/OFD SQLite users. Before
spawning control, Android startup acquires a whole-database POSIX exclusive
lock in the Java parent. The Go child uses the separate BSD family, while
older POSIX/OFD readers and writers are mechanically excluded. This descriptor
never executes SQL, checkpoints a WAL or writes a row. A process-wide canonical
path registry prevents a second Java open/close from releasing the first
process-owned POSIX lock. No other Java component opens the Go control database.
The lease stays held until the actual bound child exits, including cancellation
and delayed shutdown; new startup refuses an existing legacy holder. Final-file
and control-directory symlinks fail before opening the database.

Keep Android Auto Backup enabled. The existing manifest has no custom backup
agent or foreground-backup override; Android documents that Auto Backup shuts
down the app. This source fact does not substitute for complete backup/restore
or process-group kill acceptance. The device suite must retain all seven
original methods and qualify the two added lock methods, including real
separate-process probes, duplicate JVM acquisition, premature close while a
child lives, legacy-owner refusal and reopen after exit. A recreated supervisor
waits at most three seconds for a registered bound child using a monitor
notification; it never opens a duplicate descriptor during the wait. This
handoff happens outside the spawn/shutdown monitor so UI cancellation stays
available, and a late exit waiter must not release the successor's lease. Probe payloads are
restricted to the test APK and never open SQLite or read/write database bytes.

References: [pinned VFS locking contract](https://github.com/ncruces/go-sqlite3/blob/091633ebb62b0c78988d3e079149333b36337599/vfs/README.md),
[Java file-lock descriptor lifetime](https://docs.oracle.com/javase/8/docs/api/java/nio/channels/FileLock.html),
[Android Auto Backup lifecycle](https://developer.android.com/identity/data/autobackup).
This repair does not qualify Rust worker availability, Go production ownership,
ARM64 device execution or the complete zero-Python product. Keep `NOT_READY`
until their original evidence gates pass.
