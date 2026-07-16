from __future__ import annotations

import subprocess
import textwrap
import unittest


def run_frontend_utils(script: str) -> None:
    code = (
        "const assert = require('assert');\n"
        "const { pathToFileURL } = require('url');\n"
        "globalThis.window = globalThis;\n"
        "globalThis.DeepSeekMathCore = {\n"
        "  extractInlineMath(value) { return [{ type: 'text', value: String(value || '') }]; },\n"
        "  renderMathExpression(value) { return String(value || ''); },\n"
        "};\n"
        "(async () => {\n"
        "  const bust = `?test=${Date.now()}-${Math.random()}`;\n"
        "  const load = (name) => import(pathToFileURL(`static/modules/${name}.js`).href + bust);\n"
        "  const charts = await load('charts');\n"
        "  const format = await load('format');\n"
        "  const normalize = await load('normalize');\n"
        "  const reminder = await load('reminder_parse');\n"
        "  const speech = await load('speech_text');\n"
        "  const stream = await load('stream');\n"
        "  const agentTimeline = await load('agent_timeline');\n"
        "  const credentialStore = await load('credential_store');\n"
        "  const uploadController = await load('upload_controller');\n"
        "  const skillBuilder = await load('skill_builder');\n"
        "  const context = { assert, charts, format, normalize, reminder, speech, stream, agentTimeline, credentialStore, uploadController, skillBuilder };\n"
        "  await (async function() {\n"
        + textwrap.dedent(script)
        + "\n  }).call(context);\n"
        "})().catch((error) => { console.error(error); process.exit(1); });\n"
    )
    subprocess.run(["node", "-e", code], check=True)


class FrontendUtilsTests(unittest.TestCase):
    def test_credentials_are_session_scoped_and_legacy_local_values_are_purged(self) -> None:
        run_frontend_utils(
            r"""
            const { credentialStore } = this;
            const makeStorage = (initial = {}) => {
              const values = new Map(Object.entries(initial));
              return {
                getItem: (key) => values.has(key) ? values.get(key) : null,
                setItem: (key, value) => values.set(key, String(value)),
                removeItem: (key) => values.delete(key),
                values,
              };
            };
            const keys = {
              apiKey: "deepseek-infra.api-key",
              rememberKey: "deepseek-infra.remember-key",
              tavilyKey: "deepseek-infra.tavily-key",
              rememberTavilyKey: "deepseek-infra.remember-tavily-key",
            };
            const local = makeStorage({
              [keys.apiKey]: "sk-legacy",
              [keys.rememberKey]: "1",
              "deepseek-mobile.tavily-key": "tvly-legacy",
              "deepseek-mobile.remember-tavily-key": "1",
            });
            const session = makeStorage();
            const store = credentialStore.createCredentialSession(keys, { localStorage: local, sessionStorage: session });

            assert.strictEqual(local.getItem(keys.apiKey), null);
            assert.strictEqual(local.getItem(keys.rememberKey), null);
            assert.strictEqual(local.getItem("deepseek-mobile.tavily-key"), null);
            assert.strictEqual(store.load("deepseek"), "sk-legacy");
            assert.strictEqual(store.load("tavily"), "tvly-legacy");

            store.update("deepseek", "sk-session");
            assert.strictEqual(session.getItem(keys.apiKey), "sk-session");
            store.setRetained("deepseek", false, "");
            assert.strictEqual(store.load("deepseek"), "");
            assert.strictEqual(session.getItem(keys.apiKey), null);
            """
        )

    def test_upload_task_supports_timeout_cancel_and_success(self) -> None:
        run_frontend_utils(
            r"""
            const { uploadController } = this;
            class FakeXhr {
              constructor() { this.upload = {}; this.headers = {}; this.status = 0; this.responseText = ""; }
              open(method, url) { this.method = method; this.url = url; }
              setRequestHeader(name, value) { this.headers[name] = value; }
              send(body) { this.body = body; }
              abort() { this.onabort(); }
            }
            const file = new File(["hello"], "note.txt", { type: "text/plain" });

            const cancelledXhr = new FakeXhr();
            const cancelled = uploadController.createUploadTask([file], {
              xhrFactory: () => cancelledXhr,
              timeoutMs: 1234,
              authToken: "local-token",
            });
            assert.strictEqual(cancelledXhr.timeout, 1234);
            assert.strictEqual(cancelledXhr.headers.Authorization, "Bearer local-token");
            cancelled.cancel();
            await assert.rejects(cancelled.promise, (error) => error.name === "AbortError" && error.message === "上传已取消");
            assert.strictEqual(cancelled.active, false);

            const timeoutXhr = new FakeXhr();
            const timedOut = uploadController.createUploadTask([file], { xhrFactory: () => timeoutXhr });
            timeoutXhr.ontimeout();
            await assert.rejects(timedOut.promise, /上传超时/);

            const successXhr = new FakeXhr();
            const progress = [];
            const success = uploadController.createUploadTask([file], {
              xhrFactory: () => successXhr,
              onProgress: (value) => progress.push(value),
            });
            successXhr.upload.onprogress({ lengthComputable: true, loaded: 1, total: 2 });
            successXhr.status = 200;
            successXhr.responseText = JSON.stringify({ file: { name: "note.txt" } });
            successXhr.onload();
            assert.deepStrictEqual(await success.promise, { files: [{ name: "note.txt" }], errors: [] });
            assert.deepStrictEqual(progress, [50]);
            """
        )

    def test_skill_builder_schema_logic_is_isolated_from_dom(self) -> None:
        run_frontend_utils(
            r"""
            const { skillBuilder } = this;
            const fields = [
              { key: "depth", title: "Depth", type: "integer", required: true, defaultValue: "2", enumOptions: [], maxLength: 0 },
              { key: "mode", title: "Mode", type: "enum", required: false, defaultValue: "", enumOptions: ["fast", "deep"], maxLength: 0 },
            ];
            const schema = skillBuilder.buildInputSchema(fields);
            assert.deepStrictEqual(schema.required, ["depth"]);
            assert.strictEqual(schema.properties.depth.default, 2);
            assert.deepStrictEqual(schema.properties.mode.enum, ["fast", "deep"]);
            assert.deepStrictEqual(skillBuilder.sampleInputFromFields(fields), { depth: 2 });
            assert.strictEqual(skillBuilder.fieldTypeFromSchema({ type: "string", maxLength: 500 }), "textarea");
            assert.strictEqual(skillBuilder.outputModeFromSchema(skillBuilder.defaultOutputSchema("title_content")), "title_content");
            assert.strictEqual(skillBuilder.defaultBuilderSkill(1).skillId, "skill_custom_1");
            """
        )

    def test_charts_format_normalize_and_reminders(self) -> None:
        run_frontend_utils(
            r"""
            const { charts, format, normalize, reminder } = this;

            assert.strictEqual(charts.parseChartCell("1,234.5%"), 1234.5);
            assert.ok(Number.isNaN(charts.parseChartCell("n/a")));
            assert.ok(charts.chartSvg([{ label: "<收入>", value: 3 }], "bar").includes("&lt;收入&gt;"));
            assert.ok(charts.pieChartSvg([{ label: "A", value: 1 }, { label: "B", value: 3 }]).includes("75%"));

            assert.strictEqual(format.extensionForLanguage("javascript"), "js");
            assert.strictEqual(format.vscodeUriForPath("C:\\tmp\\a.py"), "vscode://file/C:/tmp/a.py");
            assert.strictEqual(format.safeFilename("a:b  c"), "a-b-c");
            assert.strictEqual(format.fileKindFromName("report.final.PDF"), "pdf");
            assert.strictEqual(format.tailForContinuation("abcdef", 3), "def");
            assert.ok(format.quoteAwareContent("继续", { text: "第一行\n第二行" }).includes("> 第一行"));
            assert.ok(format.quoteAwareContent("解释", { isFragment: true, fragment: String.raw`S_{\bar P}` }).startsWith("关于上文中的这一段"));

            assert.strictEqual(normalize.normalizeTheme("sepia"), "system");
            assert.strictEqual(normalize.normalizeThemeStyle("linear"), "linear");
            assert.strictEqual(normalize.normalizeThemeStyle("bad"), "chatgpt");
            assert.strictEqual(normalize.normalizeThemeMode("dark"), "dark");
            assert.strictEqual(normalize.normalizeThemeMode("bad"), "system");
            assert.strictEqual(normalize.normalizeFontSize("99", 16, 14, 21), 21);
            assert.strictEqual(normalize.normalizeVoiceLanguage("zh-Hans-CN"), "zh-CN");
            assert.strictEqual(normalize.normalizeVoiceLanguage("zh_Hant_TW"), "zh-TW");
            assert.strictEqual(normalize.normalizeModel("x", new Set(["a"]), "a"), "a");
            assert.strictEqual(normalize.normalizeSeekId("  seek-1  "), "seek-1");
            const attachment = normalize.normalizeStoredAttachment({
              name: "note.txt",
              text: "hello",
              thumbnail: "data:image/jpeg;base64,thumb",
              imagePreview: "data:image/jpeg;base64,preview",
            });
            assert.strictEqual(attachment.preview, "hello");
            assert.strictEqual(attachment.thumbnail, "data:image/jpeg;base64,thumb");
            assert.strictEqual(attachment.imagePreview, "data:image/jpeg;base64,preview");
            assert.ok(attachment.id);
            // PDF 阅读器开图路径 (openFilePreview -> normalizeStoredAttachment) 必须保留
            // pageCount / sourceAvailable，否则多页 PDF 只会渲染 1 页（回归保护）。
            const pdfAttachment = normalize.normalizeStoredAttachment({
              name: "paper.pdf",
              kind: "pdf",
              type: "application/pdf",
              fileId: "f".repeat(32),
              pageCount: 58,
              sourceAvailable: true,
            });
            assert.strictEqual(pdfAttachment.pageCount, 58);
            assert.strictEqual(pdfAttachment.sourceAvailable, true);

            const parsed = reminder.detectReminderFromText("明天 9 点提醒我提交日报", new Date("2026-01-01T00:00:00Z"));
            assert.strictEqual(parsed.title, "DeepSeek 提醒");
            assert.strictEqual(parsed.content, "提交日报");
            assert.ok(parsed.dueAt);
            """
        )

    def test_speech_text_and_stream_reader(self) -> None:
        run_frontend_utils(
            r"""
            const { speech, stream } = this;

            const spoken = speech.speechTextFromMessage({
              content: "公式 $\\frac{a}{b}$ [^F1-2] | [链接](https://example.com) `code`",
            });
            assert.ok(spoken.includes("公式略"));
            assert.ok(spoken.includes("链接"));
            assert.ok(!spoken.includes("\\frac"));
            assert.ok(!spoken.includes("[^F1-2]"));
            assert.ok(!spoken.includes("|"));

            const chunks = speech.speechChunks("a".repeat(181));
            assert.strictEqual(chunks.length, 2);
            assert.ok(chunks.every((item) => item.length <= 180));
            const voice = speech.preferredSpeechVoice("zh-Hans-CN", [{ lang: "en-US" }, { lang: "zh-CN", name: "cn" }]);
            assert.strictEqual(voice.name, "cn");

            assert.deepStrictEqual(stream.parseStreamEventLine('{"type":"delta"}'), { type: "delta" });
            assert.strictEqual(stream.parseStreamEventLine("not json", { warn() {} }), null);

            const encoder = new TextEncoder();
            const response = new Response(
              new ReadableStream({
                start(controller) {
                  controller.enqueue(encoder.encode('{"type":"a"}\n{"type"'));
                  controller.enqueue(encoder.encode(':"b"}'));
                  controller.close();
                },
              })
            );
            const events = [];
            let waits = 0;
            await stream.readChatStream(response, {
              waitUntilResumed: async () => {
                waits += 1;
              },
              onEvent: (event) => events.push(event.type),
            });
            assert.deepStrictEqual(events, ["a", "b"]);
            assert.ok(waits >= 2);
            """
        )

    def test_agent_timeline_leader_two_phases_have_unique_ids(self) -> None:
        # v1.2.7：1.2.6 里 agentStepId 只按 phase 生成，Leader 两轮（拆解→综合）
        # 共享同一个 id，timelineStepKey 会塌成同一个 DOM key。本测试守住这条线。
        run_frontend_utils(
            r"""
            const { agentTimeline } = this;
            const {
              agentStepId,
              createAgentStepId,
              appendTimelineAgent,
              appendTimelineAgentDelta,
              normalizeTimeline,
              timelineStepKey,
              shouldCollapseAgentStep,
            } = agentTimeline;

            // ---- 1) Leader 两轮：每轮都拿到独立 step + 独立 id + 独立 timeline key
            const message = { timeline: [] };
            appendTimelineAgent(message, { phase: "leader", status: "running", name: "Leader", text: "正在拆解" });
            appendTimelineAgent(message, { phase: "leader", status: "done", name: "Leader", text: "已完成任务拆解" });
            // 中间夹一个 worker，验证 Leader 计数不会被其他 phase 串扰
            appendTimelineAgent(message, { phase: "researcher", status: "running", name: "Researcher", text: "" });
            appendTimelineAgent(message, { phase: "researcher", status: "done", name: "Researcher", text: "" });
            appendTimelineAgent(message, { phase: "leader", status: "running", name: "Leader", text: "正在综合" });
            appendTimelineAgent(message, { phase: "leader", status: "done", name: "Leader", text: "已完成综合" });

            const leaders = message.timeline.filter((step) => step.kind === "agent" && step.phase === "leader");
            assert.strictEqual(leaders.length, 2, "Leader 应该有两张独立卡片");
            assert.notStrictEqual(leaders[0].id, leaders[1].id, "两张 Leader 卡片 id 不能相同");
            assert.strictEqual(leaders[0].id, "agent-leader-1");
            assert.strictEqual(leaders[1].id, "agent-leader-2");
            assert.strictEqual(leaders[0].text, "已完成任务拆解");
            assert.strictEqual(leaders[1].text, "已完成综合");

            const keys = message.timeline.map((step, index) => timelineStepKey(step, index));
            assert.strictEqual(new Set(keys).size, keys.length, "所有 step 的 timelineStepKey 应该唯一");

            // ---- 2) agent_delta 比 agent 事件先到时的占位也要拿到唯一 id
            const m2 = { timeline: [] };
            appendTimelineAgent(m2, { phase: "leader", status: "running", name: "Leader", text: "正在拆解" });
            appendTimelineAgent(m2, { phase: "leader", status: "done", name: "Leader", text: "已完成任务拆解" });
            // 综合阶段，Leader 的 agent 事件还没到，agent_delta 先到（构造极端情况）
            appendTimelineAgentDelta(m2, { phase: "leader", name: "Leader", text: "综合中..." });
            const leaders2 = m2.timeline.filter((step) => step.kind === "agent" && step.phase === "leader");
            assert.strictEqual(leaders2.length, 2);
            assert.strictEqual(leaders2[1].id, "agent-leader-2", "delta 占位也要走 createAgentStepId");
            assert.strictEqual(leaders2[1].output, "综合中...");

            // ---- 3) 旧 history 里两张 Leader 卡片共享 id 时，normalizeTimeline 要补号去重
            const legacy = [
              {
                kind: "agent",
                id: "agent-leader",
                phase: "leader",
                status: "done",
                name: "Leader",
                text: "已完成任务拆解",
                reasoning: "",
                notes: [],
                output: "",
              },
              {
                kind: "agent",
                id: "agent-leader",
                phase: "leader",
                status: "done",
                name: "Leader",
                text: "已完成综合",
                reasoning: "综合 reasoning",
                notes: [],
                output: "综合内容",
              },
            ];
            const normalized = normalizeTimeline(legacy);
            assert.strictEqual(normalized.length, 2);
            assert.strictEqual(normalized[0].id, "agent-leader");
            assert.notStrictEqual(normalized[1].id, normalized[0].id, "重复 id 应该被补号去重");
            assert.strictEqual(normalized[1].id, "agent-leader-2");

            // ---- 4) 折叠策略分级：Leader 不折叠 / 错误 Agent 展开 / 普通完成 worker 折叠
            assert.strictEqual(
              shouldCollapseAgentStep({ status: "done", phase: "leader", output: "x" }),
              false,
              "Leader 完成后应该保留展开",
            );
            assert.strictEqual(
              shouldCollapseAgentStep({ status: "error", phase: "coder", output: "x" }),
              false,
              "失败 Agent 应该默认展开",
            );
            assert.strictEqual(
              shouldCollapseAgentStep({ status: "done", phase: "coder", output: "代码片段" }),
              true,
              "完成且有内容的中间 worker 默认折叠",
            );
            assert.strictEqual(
              shouldCollapseAgentStep({ status: "done", phase: "researcher" }),
              false,
              "没有内容的卡片不折叠（没什么可折叠的）",
            );

            // ---- 5) agentStepId 仍是按 phase 的纯函数（toggle 兜底用），保持向后兼容
            assert.strictEqual(agentStepId("leader"), "agent-leader");
            assert.strictEqual(createAgentStepId({ timeline: [] }, "coder"), "agent-coder-1");
            """
        )

    def test_agent_run_summary_aggregates_worker_phases_in_canonical_order(self) -> None:
        # v1.2.8：执行摘要条只统计 worker agent，Leader 不出现在 chip 列表里；
        # 顺序固定为 researcher → coder → reasoner → critic，同 phase 多张卡片取最后一张。
        run_frontend_utils(
            r"""
            const { agentTimeline } = this;
            const { appendTimelineAgent, agentRunSummary, agentRunSummarySignature } = agentTimeline;

            // 1) 空 timeline 没摘要
            const empty = agentRunSummary({ timeline: [] });
            assert.strictEqual(empty.count, 0);
            assert.deepStrictEqual(empty.items, []);
            assert.strictEqual(agentRunSummarySignature(empty), "");

            // 2) 单 Agent / 单纯 reasoning 不该出摘要条（这里只有 leader 一项 worker 是 0）
            const onlyLeader = { timeline: [] };
            appendTimelineAgent(onlyLeader, { phase: "leader", status: "running", name: "Leader", text: "" });
            appendTimelineAgent(onlyLeader, { phase: "leader", status: "done", name: "Leader", text: "", durationMs: 500 });
            const leaderOnly = agentRunSummary(onlyLeader);
            assert.strictEqual(leaderOnly.count, 0, "Leader 不进 worker 摘要");

            // 3) 多 worker 全部完成：count = 实际跑过的 worker 数，顺序固定
            const m = { timeline: [] };
            // 故意按非规范顺序 emit，验证摘要内部强制 researcher→coder→reasoner→critic 顺序
            appendTimelineAgent(m, { phase: "coder", status: "running", name: "Coder", text: "" });
            appendTimelineAgent(m, { phase: "researcher", status: "running", name: "Researcher", text: "" });
            appendTimelineAgent(m, { phase: "researcher", status: "done", name: "Researcher", text: "", durationMs: 1200 });
            appendTimelineAgent(m, { phase: "coder", status: "error", name: "Coder", text: "", durationMs: 2200 });
            appendTimelineAgent(m, { phase: "reasoner", status: "done", name: "Reasoner", text: "", durationMs: 800 });
            const summary = agentRunSummary(m);
            assert.strictEqual(summary.count, 3, "应该只统计实际跑过的 3 个 worker");
            assert.deepStrictEqual(
              summary.items.map((item) => item.phase),
              ["researcher", "coder", "reasoner"],
              "顺序固定为 researcher → coder → reasoner → critic",
            );
            assert.strictEqual(summary.items[0].status, "done");
            assert.strictEqual(summary.items[1].status, "error");
            assert.strictEqual(summary.items[2].status, "done");
            assert.strictEqual(summary.items[0].durationMs, 1200);
            assert.strictEqual(summary.items[1].durationMs, 2200);
            assert.strictEqual(summary.items[2].durationMs, 800);

            // 4) 同 phase 多张卡片，取最后一张
            const m2 = { timeline: [] };
            appendTimelineAgent(m2, { phase: "critic", status: "done", name: "Critic", text: "", durationMs: 100 });
            // 假设 Critic 重新跑（罕见但有可能）：第二张 running 卡片
            appendTimelineAgent(m2, { phase: "critic", status: "running", name: "Critic", text: "" });
            const summary2 = agentRunSummary(m2);
            assert.strictEqual(summary2.items[0].status, "running", "同 phase 最后一张状态应该 win");

            // 5) signature 不同状态/耗时会产生不同字符串，相同则一致
            const sig1 = agentRunSummarySignature(summary);
            appendTimelineAgent(m, { phase: "critic", status: "done", name: "Critic", text: "", durationMs: 600 });
            const sig2 = agentRunSummarySignature(agentRunSummary(m));
            assert.notStrictEqual(sig1, sig2, "新增 critic 后 signature 应该变化");
            """
        )

    def test_agent_execution_report_extracts_key_sections(self) -> None:
        run_frontend_utils(
            r"""
            const { agentExecutionReport } = this.agentTimeline;
            const message = {
              content: "这是最终回答。",
              timeline: [
                { kind: "agent", phase: "leader", status: "done", name: "Leader", text: "已完成任务拆解：\n- 资料：查证\n- 代码：分析" },
                { kind: "agent", phase: "researcher", status: "done", name: "Researcher", text: "已完成", output: "## 摘要\n资料摘要\n\n## 关键事实\n事实" },
                { kind: "agent", phase: "coder", status: "done", name: "Coder", text: "已完成", output: "## 摘要\n代码摘要\n\n## 完整分析\n细节" },
                { kind: "agent", phase: "reasoner", status: "done", name: "Reasoner", text: "已完成", output: "## 摘要\n推理摘要" },
                { kind: "agent", phase: "critic", status: "done", name: "Critic", text: "已完成", output: "## 风险/不确定\n主要风险" },
              ],
            };
            const report = agentExecutionReport(message);
            assert.ok(report.startsWith("# Agent 执行报告"));
            assert.ok(report.includes("## Leader 拆解\n已完成任务拆解"));
            assert.ok(report.includes("## Researcher 摘要\n资料摘要"));
            assert.ok(report.includes("## Coder 摘要\n代码摘要"));
            assert.ok(report.includes("## Reasoner 摘要\n推理摘要"));
            assert.ok(report.includes("## Critic 风险\n主要风险"));
            assert.ok(report.includes("## 最终回答\n这是最终回答。"));
            assert.strictEqual(agentExecutionReport({ content: "普通回复", timeline: [] }), "");
            """
        )

    def test_agent_timeline_carries_and_formats_duration_ms(self) -> None:
        # v1.2.8：后端 done/error agent 事件携带 durationMs；前端要在 step 上持久化、
        # 刷新还原、并能格式化成人类可读字符串。
        run_frontend_utils(
            r"""
            const { agentTimeline } = this;
            const { appendTimelineAgent, normalizeTimeline, formatAgentDuration } = agentTimeline;

            // 1) running → done 链上，durationMs 应该跟着 done 事件落到 step 上
            const message = { timeline: [] };
            appendTimelineAgent(message, { phase: "coder", status: "running", name: "Coder", text: "正在处理" });
            appendTimelineAgent(message, { phase: "coder", status: "done", name: "Coder", text: "已完成", durationMs: 1320 });
            const coder = message.timeline.find((step) => step.kind === "agent" && step.phase === "coder");
            assert.strictEqual(coder.status, "done");
            assert.strictEqual(coder.durationMs, 1320);

            // 2) error 事件也带耗时，且不会被覆盖回 null
            const m2 = { timeline: [] };
            appendTimelineAgent(m2, { phase: "reasoner", status: "running", name: "Reasoner", text: "" });
            appendTimelineAgent(m2, { phase: "reasoner", status: "error", name: "Reasoner", text: "失败", durationMs: 540 });
            const reasoner = m2.timeline.find((step) => step.kind === "agent" && step.phase === "reasoner");
            assert.strictEqual(reasoner.status, "error");
            assert.strictEqual(reasoner.durationMs, 540);

            // 3) 非法 durationMs（NaN / 负数 / 缺失）走 null，不污染数据
            const m3 = { timeline: [] };
            appendTimelineAgent(m3, { phase: "critic", status: "done", name: "Critic", text: "已完成", durationMs: "not-a-number" });
            appendTimelineAgent(m3, { phase: "researcher", status: "done", name: "Researcher", text: "已完成", durationMs: -10 });
            appendTimelineAgent(m3, { phase: "leader", status: "done", name: "Leader", text: "已完成" });
            for (const step of m3.timeline) {
              assert.strictEqual(step.durationMs, null, `${step.phase} 应该把非法 durationMs 归一为 null`);
            }

            // 4) normalizeTimeline 持久化路径要保留 durationMs，刷新后仍能恢复
            const normalized = normalizeTimeline([
              {
                kind: "agent",
                id: "agent-coder-1",
                phase: "coder",
                status: "done",
                name: "Coder",
                text: "已完成",
                reasoning: "",
                notes: [],
                output: "代码片段",
                durationMs: 2580,
              },
              {
                kind: "agent",
                id: "agent-leader-1",
                phase: "leader",
                status: "done",
                name: "Leader",
                text: "已完成综合",
                reasoning: "",
                notes: [],
                output: "",
                // 没传 durationMs：归一为 null，但其它字段照常恢复
              },
            ]);
            assert.strictEqual(normalized[0].durationMs, 2580);
            assert.strictEqual(normalized[1].durationMs, null);

            // 5) v1.2.9：历史里明确保存 durationMs: null 时，不能被 Number(null) 恢复成 0ms
            const restoredNullDuration = normalizeTimeline([
              {
                kind: "agent",
                id: "agent-coder-1",
                phase: "coder",
                status: "done",
                name: "Coder",
                text: "已完成",
                reasoning: "",
                notes: [],
                output: "代码分析",
                durationMs: null,
              },
            ]);
            assert.strictEqual(restoredNullDuration[0].durationMs, null);
            assert.strictEqual(formatAgentDuration(restoredNullDuration[0].durationMs), "");

            // 6) formatAgentDuration：单位切换 + 非法输入兜底
            assert.strictEqual(formatAgentDuration(850), "850ms");
            assert.strictEqual(formatAgentDuration(1320), "1.3s");
            assert.strictEqual(formatAgentDuration(60_000), "1m");
            assert.strictEqual(formatAgentDuration(65_000), "1m 5s");
            assert.strictEqual(formatAgentDuration(null), "");
            assert.strictEqual(formatAgentDuration(undefined), "");
            assert.strictEqual(formatAgentDuration("nope"), "");
            assert.strictEqual(formatAgentDuration(-5), "");
            """
        )

    def test_agent_timeline_reset_removes_only_target_phase(self) -> None:
        run_frontend_utils(
            r"""
            const { agentTimeline } = this;
            const { appendTimelineAgent, appendTimelineAgentDelta, resetTimelineAgentPhase, agentRunSummary } = agentTimeline;

            const message = { timeline: [] };
            appendTimelineAgent(message, { phase: "coder", status: "running", name: "Coder", text: "" });
            appendTimelineAgentDelta(message, { phase: "coder", name: "Coder", text: "old coder output" });
            appendTimelineAgent(message, { phase: "reasoner", status: "done", name: "Reasoner", text: "ok", durationMs: 10 });
            message.timeline.push({ kind: "reasoning", text: "leader thought" });

            resetTimelineAgentPhase(message, "coder");

            assert.ok(!message.timeline.some((step) => step.kind === "agent" && step.phase === "coder"));
            assert.ok(message.timeline.some((step) => step.kind === "agent" && step.phase === "reasoner"));
            assert.ok(message.timeline.some((step) => step.kind === "reasoning"));
            assert.deepStrictEqual(agentRunSummary(message).items.map((item) => item.phase), ["reasoner"]);
            """
        )

    def test_build_trace_span_tree_nests_children_and_keeps_orphans(self) -> None:
        # v2.0.5: 瀑布图把扁平 span 列表按 parentSpanId 展开成 DFS 树，
        # 同层按 offsetMs 排序，dangling parent / 环都兜底成根、不丢 span 不死循环。
        run_frontend_utils(
            r"""
            const { agentTimeline } = this;
            const { buildTraceSpanTree } = agentTimeline;

            const spans = [
              { spanId: "a", parentSpanId: "", name: "agent.researcher", offsetMs: 0 },
              { spanId: "b", parentSpanId: "a", name: "researcher:deepseek:1", offsetMs: 100 },
              { spanId: "t", parentSpanId: "a", name: "tool.web_search", offsetMs: 50 },
              { spanId: "c", parentSpanId: "", name: "agent.coder", offsetMs: 200 },
              { spanId: "orphan", parentSpanId: "missing", name: "stray", offsetMs: 300 },
            ];
            const tree = buildTraceSpanTree(spans);
            assert.deepStrictEqual(
              tree.map((node) => [node.span.name, node.depth]),
              [
                ["agent.researcher", 0],
                ["tool.web_search", 1],
                ["researcher:deepseek:1", 1],
                ["agent.coder", 0],
                ["stray", 0],
              ],
            );

            // 互相依赖（环）不能死循环，两个 span 都兜底成根、各出现一次。
            const cyclic = [
              { spanId: "x", parentSpanId: "y", name: "x" },
              { spanId: "y", parentSpanId: "x", name: "y" },
            ];
            const cyclicTree = buildTraceSpanTree(cyclic);
            assert.strictEqual(cyclicTree.length, 2);
            assert.deepStrictEqual(cyclicTree.map((node) => node.span.name).sort(), ["x", "y"]);

            assert.deepStrictEqual(buildTraceSpanTree(null), []);
            """
        )
