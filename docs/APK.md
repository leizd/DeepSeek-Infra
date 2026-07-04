# Android APK 打包说明

适用版本：v2.8.2。

本仓库现在包含 `android/` Android Studio 工程，可把 DeepSeek Infra 打包成手机上直接运行的 APK。APK 的结构是：

- Android 原生 `MainActivity` 负责启动 WebView、文件选择器和外链跳转。
- Chaquopy 在 APK 内嵌 Python 3.13 运行 `deepseek_infra` 后端；项目代码仍保持 Python 3.10+ 兼容。
- Python 后端只监听 `127.0.0.1:8000`，WebView 打开带 token 的本机地址。
- `.auth-token`、文件缓存、记忆、项目、Agent Run、trace 和语义缓存写入 Android 应用私有目录，不会写到 APK 只读资源区。
- v1.8.0 的 API 网关请求队列写入 Android 应用私有目录下的 `.request-queue/queue.sqlite3`，用于网络切换、后台等待和 DeepSeek 网关类错误的退避重试状态记录。
- 端侧推理基础设施由 Python 后端统一暴露；APK 默认仍以云端 DeepSeek 路由为主，若要在 APK 内启用 llama.cpp / MLC-LLM，需要额外打包对应平台可用的本地推理依赖和量化模型文件。
- 本地 RAG 数据层同样由 Python 后端暴露；默认 SQLite + 哈希 embedding 可直接使用，若要启用 sqlite-vec 或 ONNX Runtime embedding，需要额外打包对应 Android ABI 可用的 native 依赖。
- APK 目标 ABI 为 `arm64-v8a` 和 `x86_64`；Python 3.13 不提供 32 位 `armeabi-v7a` 运行时。

## 构建环境

需要先安装：

- Android Studio。
- Android SDK Platform 34（当前 `android/app/build.gradle` 使用 `compileSdk 34` / `targetSdk 34`）。
- JDK 17。
- 能访问 Maven Central / Google Maven 的网络，用于下载 Android Gradle Plugin 和 Chaquopy。

本地没有安装 Gradle wrapper 时，最简单的方式是用 Android Studio 打开 `android/` 目录，等待 Gradle 同步完成。

## 命令行构建

如果本机已有 Gradle：

```bash
cd android
gradle :app:assembleDebug
```

输出文件：

```text
android/app/build/outputs/apk/debug/app-debug.apk
```

安装到已连接手机：

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## 发布包

生成 release APK 前，需要在 `android/app/build.gradle` 中添加签名配置，或在 Android Studio 的 Build > Generate Signed Bundle / APK 中配置 keystore。配置后执行：

```bash
cd android
gradle :app:assembleRelease
```

## 运行方式

安装 APK 后直接点击图标即可。应用会：

1. 在应用私有目录初始化 Python 数据目录。
2. 启动本机 HTTP 服务。
3. 自动把 WebView 打开到 `http://127.0.0.1:8000/?token=...`。

DeepSeek API Key 和 Tavily API Key 可以继续在网页右上角设置中填写。APK 默认不开放局域网访问；如果以后要做“手机作为局域网服务端”，需要额外增加原生设置页并显式切换监听地址。

## 已知边界

- Agentic Observability 会把 `.traces/traces.sqlite3` 和 `.semantic-cache/cache.sqlite3` 写入应用私有目录；卸载应用或清理应用数据会删除这些本地 trace/cache。移动端空间紧张时，可以通过 `/api/semantic-cache` 清空语义缓存，trace 数据也可随应用数据一起清理。
- OCR 在 APK 内会优先调用 DeepSeek API 直接识别图片文字；如果 API Key 缺失、API 失败或返回空文本，再通过 ML Kit 中文文本识别兜底。图片和扫描 PDF OCR 不需要手机系统额外安装 Tesseract/Poppler；扫描 PDF 页面会以更高 scale 渲染并保留像素上限保护。
- Edge Inference Infra 支持本地 GGUF / MLC 模型路径和端云路由，但移动端包体、ABI、内存与散热约束更强，建议只在明确打包了对应 native 推理库和 4-bit 量化权重时开启。
- Local Data Infra 会把 `.local-rag/rag.sqlite3` 写入应用私有目录；卸载应用或清理应用数据会删除这些本地索引，重新上传/重建即可恢复。
- 首次构建会下载 Android/Chaquopy/依赖包，离线环境无法完成。
- 当前项目没有提交 Gradle wrapper 二进制文件；需要 Android Studio 或本机 Gradle。
