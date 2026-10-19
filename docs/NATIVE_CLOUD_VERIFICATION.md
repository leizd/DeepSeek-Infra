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
连接错误现在额外保留SQLite extended code和OS errno的数值，原错误链、连接拒绝及所有持久化配置不变。
不改变WAL、同步、文件锁、控制schema或写入权，也不重试未知远端副作用。

设备报告只增加`uname -r/-m`两项固定内核事实，用于区分运行内核和VFS失败位置；
这些不是成功工作负载或平台验收。缺失内核事实明确标记不可用。
调试沙盒日志改用带远端退出状态的`adb shell -T`；`exec-out`可把缺失文件的错误作为成功传输输出，
因此旧7d报告中71字节的worker错误文本虽标记COLLECTED，不能证明worker存在。
当前Android仍缺Rust worker，所有生产/ARM64/完整迁移资格保持false。

参考：[固定SQLite VFS实现](https://github.com/ncruces/go-sqlite3/blob/v0.35.6/vfs/README.md)
与[ADB shell选项](https://android.googlesource.com/platform/packages/modules/adb/+/refs/heads/main/docs/user/adb.1.md)。
VFS文档要求OFD锁依赖Linux3.15且不同锁族并发访问不兼容；当前尚无本次设备内核和extended code，
因此保留实际失败并收集证据，不能先换锁族、降低SDK或跳过API24来取得绿色结果。
