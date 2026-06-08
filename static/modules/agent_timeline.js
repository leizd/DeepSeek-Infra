// v1.2.7：从 chat.js 抽出来的 agent timeline 纯函数。原 chat.js 把这些
// helper 嵌在巨大的模块体里，副作用（window.DeepSeekSeekCore、localStorage、
// DOM）让它们既无法单测，也容易在维护时被不小心耦合到 UI 状态上。抽到独立
// 模块后：(1) 可以脱 DOM 测试 ID 生成/合并/normalize 逻辑；(2) 多 Agent
// timeline 的"业务规则"集中在一处，规则变更不用翻 8000 行的 chat.js。

const AGENT_PHASE_MAX = 80;
const AGENT_NOTES_LIMIT = 20;
const AGENT_TEXT_LIMIT = 5000;
const AGENT_REASONING_LIMIT = 60_000;
const AGENT_OUTPUT_LIMIT = 60_000;
const AGENT_ID_MAX = 100;
const TIMELINE_MAX_STEPS = 60;

const VALID_AGENT_STATUS = new Set(["running", "done", "error"]);
const VALID_SEARCH_STATUS = new Set(["searching", "done", "error"]);

export function agentStepId(phase) {
  return `agent-${String(phase || "agent").slice(0, AGENT_PHASE_MAX)}`;
}

// v1.2.7：Leader 在一次会话内会被 emit 两轮（拆解 + 综合）。旧实现的
// agentStepId 只按 phase 生成，两张 Leader 卡片共享同一个 id，timelineStepKey
// 据此推出的 data-step-key 会塌成一个 DOM 节点 —— 第二张会盖掉第一张。
// 这里改成在 message.timeline 里数同 phase 已有的 agent step，把序号拼进 id，
// 让每张卡片都有独立 key。
export function createAgentStepId(message, phase) {
  const safePhase = String(phase || "agent").slice(0, AGENT_PHASE_MAX);
  const timeline = Array.isArray(message?.timeline) ? message.timeline : [];
  let count = 0;
  for (const step of timeline) {
    if (step?.kind === "agent" && step.phase === safePhase) count += 1;
  }
  return `${agentStepId(safePhase)}-${count + 1}`;
}

export function normalizeAgentNotes(value) {
  return Array.isArray(value)
    ? value.map((item) => String(item || "").trim()).filter(Boolean).slice(0, AGENT_NOTES_LIMIT)
    : [];
}

export function agentNotesSnapshot(step) {
  return normalizeAgentNotes(step?.notes).join("\n");
}

export function agentStepHasDetails(step) {
  return Boolean(step?.reasoning || step?.output || normalizeAgentNotes(step?.notes).length);
}

// v1.2.7：折叠策略分级。
//   - status !== "done"：永远不默认折叠（running / error 都需要看见）
//   - phase === "leader"：完成后也保留展开，状态说明对用户是导航信息
//   - 其他 worker（researcher / coder / reasoner / critic）完成且有内容：折叠
export function shouldCollapseAgentStep(step) {
  if (!step || step.status !== "done") return false;
  if (step.phase === "leader") return false;
  return agentStepHasDetails(step);
}

// v1.2.8：执行摘要条用的 worker Agent 固定顺序（不含 leader——Leader 是协调者，
// 不进 worker 列表）。每个 worker 给一个短标签，让 "N 个 Agent · 资料 ✓ · 代码 ✕" 这条
// 概览能在窄屏里也放得下。
const WORKER_AGENT_ORDER = [
  { phase: "researcher", label: "资料" },
  { phase: "coder", label: "代码" },
  { phase: "reasoner", label: "推理" },
  { phase: "critic", label: "复核" },
];
const AGENT_REPORT_PHASE_TITLES = new Map([
  ["researcher", "Researcher 摘要"],
  ["coder", "Coder 摘要"],
  ["reasoner", "Reasoner 摘要"],
  ["critic", "Critic 风险"],
]);

// v1.2.8：给前端 Activity 顶部 + inline reasoning 顶部生成一行执行摘要。
// 实现要点：
//   - 只统计 worker agent；Leader 不出现在 chip 列表里
//   - 同 phase 多张卡片时取 timeline 里最后一张（最新状态优先）
//   - 没在 timeline 出现过的 worker 直接跳过（避免 4 个固定 chip，让 UI 只反映实际跑了的）
//   - 顺序固定为 researcher → coder → reasoner → critic，避免完成顺序漂移导致 UI 抖动
export function agentRunSummary(message) {
  const timeline = Array.isArray(message?.timeline) ? message.timeline : [];
  const lastByPhase = new Map();
  for (const step of timeline) {
    if (step?.kind !== "agent") continue;
    if (step.phase === "leader") continue;
    lastByPhase.set(step.phase, step);
  }
  const items = [];
  for (const { phase, label } of WORKER_AGENT_ORDER) {
    const step = lastByPhase.get(phase);
    if (!step) continue;
    items.push({
      phase,
      label,
      status: ["running", "done", "error"].includes(step.status) ? step.status : "done",
      durationMs: normalizeDurationMs(step.durationMs),
    });
  }
  return { count: items.length, items };
}

// v1.2.8：摘要条的去重签名，用于 dataset 比对，避免每次增量都重渲染整条 chip 行。
// 把 phase / status / durationMs 拼成稳定字符串；只要这些都没变，DOM 就不动。
export function agentRunSummarySignature(summary) {
  const items = Array.isArray(summary?.items) ? summary.items : [];
  return items.map((item) => `${item.phase}:${item.status}:${item.durationMs ?? ""}`).join("|");
}

export function agentExecutionReport(message) {
  // v1.3.0: generate a plain-text Agent process report that works for live messages
  // and restored history, so copying does not need a backend round trip.
  const timeline = Array.isArray(message?.timeline) ? message.timeline : [];
  if (!timeline.some((step) => step?.kind === "agent")) return "";
  const sections = ["# Agent 执行报告"];
  const leaderPlan = agentReportLeaderPlan(message);
  if (leaderPlan) sections.push(`## Leader 拆解\n${leaderPlan}`);
  for (const { phase } of WORKER_AGENT_ORDER) {
    const step = lastAgentStepForPhase(timeline, phase);
    if (!step) continue;
    const title = AGENT_REPORT_PHASE_TITLES.get(phase) || `${phase} 摘要`;
    const body = agentReportBodyForStep(step);
    if (body) sections.push(`## ${title}\n${body}`);
  }
  const finalAnswer = String(message?.content || "").trim();
  if (finalAnswer) sections.push(`## 最终回答\n${finalAnswer}`);
  return sections.length > 1 ? sections.join("\n\n") : "";
}

// v1.2.8：把 durationMs 渲染成人类可读字符串，用于 Agent 卡片副标题。
//   < 1s   → "850ms"
//   < 60s  → "1.3s"（保留一位小数）
//   ≥ 60s  → "1m 5s"
// 输入非法（null / undefined / NaN / 非数字字符串 / 负数）时返回空字符串，调用方按"无耗时"处理。
// 注意 Number(null) === 0、Number("") === 0，所以这里先挡掉 null/undefined/非数字字符串，
// 否则它们会被当成 0ms 显示出来。
export function formatAgentDuration(durationMs) {
  if (durationMs === null || durationMs === undefined) return "";
  if (typeof durationMs === "string" && durationMs.trim() === "") return "";
  const ms = Number(durationMs);
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return rest > 0 ? `${minutes}m ${rest}s` : `${minutes}m`;
}

function normalizeAgentStatus(value) {
  return VALID_AGENT_STATUS.has(value) ? value : "done";
}

function agentReportLeaderPlan(message) {
  const timeline = Array.isArray(message?.timeline) ? message.timeline : [];
  const leaders = timeline.filter((step) => step?.kind === "agent" && step.phase === "leader");
  const planStep = leaders.find((step) => String(step.text || "").includes("任务拆解")) || leaders[0];
  return cleanAgentReportText(planStep?.output || planStep?.text || "");
}

function lastAgentStepForPhase(timeline, phase) {
  for (let index = timeline.length - 1; index >= 0; index -= 1) {
    const step = timeline[index];
    if (step?.kind === "agent" && step.phase === phase) return step;
  }
  return null;
}

function agentReportBodyForStep(step) {
  const output = cleanAgentReportText(step?.output || "");
  const text = cleanAgentReportText(step?.text || "");
  const notes = normalizeAgentNotes(step?.notes);
  if (step?.status === "error") return output || text || "该 Agent 执行失败。";
  if (step?.phase === "critic") {
    return extractAgentReportSection(output, ["风险", "风险/不确定", "风险与不确定", "risks"]) || output || text;
  }
  return extractAgentReportSection(output, ["摘要", "summary"]) || output || text || (notes.length ? notes.join("\n") : "");
}

function extractAgentReportSection(text, titles) {
  const source = cleanAgentReportText(text);
  if (!source) return "";
  const wanted = new Set(titles.map((title) => String(title || "").trim().toLowerCase()).filter(Boolean));
  const lines = source.split(/\r?\n/);
  const chunks = [];
  let capturing = false;
  for (const line of lines) {
    const heading = line.match(/^#{1,6}\s*(.+?)\s*$/);
    if (heading) {
      const normalized = heading[1].replace(/[：:]+$/, "").trim().toLowerCase();
      capturing = wanted.has(normalized);
      continue;
    }
    if (capturing) chunks.push(line);
  }
  return cleanAgentReportText(chunks.join("\n"));
}

function cleanAgentReportText(value) {
  return String(value || "").replace(/\r\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}

function normalizeDurationMs(value) {
  // v1.2.9: persisted history may contain durationMs: null. Number(null) is 0,
  // so guard null/undefined/blank strings before numeric coercion.
  if (value === null || value === undefined) return null;
  if (typeof value === "string" && value.trim() === "") return null;
  const raw = Number(value);
  if (!Number.isFinite(raw) || raw < 0) return null;
  return Math.round(raw);
}

function makeAgentPlaceholder(message, phase, event) {
  return {
    kind: "agent",
    id: createAgentStepId(message, phase),
    phase,
    status: "running",
    name: String(event.name || "Agent").slice(0, AGENT_PHASE_MAX),
    text: "",
    reasoning: "",
    notes: [],
    output: "",
    durationMs: null,
    collapsed: false,
  };
}

function readDurationMs(event) {
  // v1.2.8：后端在 done / error agent 事件上挂 durationMs（毫秒整数）；
  // running 事件不带。这里只做防御性数值化，非有限数或负数都按 null 处理。
  return normalizeDurationMs(event?.durationMs);
}

export function appendTimelineAgent(message, event) {
  if (!Array.isArray(message.timeline)) message.timeline = [];
  const phase = String(event.phase || "agent").slice(0, AGENT_PHASE_MAX);
  const step = {
    kind: "agent",
    id: createAgentStepId(message, phase),
    phase,
    status: normalizeAgentStatus(event.status),
    name: String(event.name || "Agent").slice(0, AGENT_PHASE_MAX),
    text: String(event.text || ""),
    // v1.2.4：保留之前 agent_delta 累积下来的 output，状态切换不应该把 worker 流式输出抹掉
    reasoning: "",
    notes: [],
    output: "",
    durationMs: readDurationMs(event),
    collapsed: false,
  };
  // 多 Agent 模式下，多个 worker 并行流式输出，timeline 末尾可能不是同一个 phase。
  // 找最后一个匹配 phase 的 running step 替换，让每个 agent 自己的 timeline 项保持单条。
  // 命中 done/error 就停下、走 push 分支，让 Leader 两轮（拆解→综合）各开一张卡。
  for (let i = message.timeline.length - 1; i >= 0; i--) {
    const candidate = message.timeline[i];
    if (candidate?.kind !== "agent" || candidate.phase !== step.phase) continue;
    if (candidate.status !== "running") break;
    step.id = candidate.id || step.id;
    step.reasoning = String(candidate.reasoning || "");
    step.notes = normalizeAgentNotes(candidate.notes);
    step.output = String(candidate.output || "");
    // v1.2.8：done/error 事件带 durationMs；running 事件不带。running 阶段 candidate 没有
    // durationMs（null）；当 event 升级到 done/error 时直接采用新值。如果同 phase 多次 running
    // 之间反复刷新（不会发生，但兜底），running 也不该把 done 的 duration 抹掉。
    if (step.durationMs === null && candidate.durationMs !== null && candidate.durationMs !== undefined) {
      step.durationMs = candidate.durationMs;
    }
    step.collapsed = shouldCollapseAgentStep(step);
    message.timeline[i] = step;
    return;
  }
  step.collapsed = shouldCollapseAgentStep(step);
  message.timeline.push(step);
}

export function appendTimelineAgentReasoning(message, event) {
  // worker 的推理增量按 phase 收进对应 Agent 卡片，和 agent_delta 共用占位创建逻辑。
  if (!Array.isArray(message.timeline)) message.timeline = [];
  const phase = String(event.phase || "agent").slice(0, AGENT_PHASE_MAX);
  const delta = String(event.text || "");
  if (!delta) return;
  for (let i = message.timeline.length - 1; i >= 0; i--) {
    const candidate = message.timeline[i];
    if (candidate?.kind !== "agent" || candidate.phase !== phase) continue;
    if (candidate.status !== "running") break;
    candidate.reasoning = String(candidate.reasoning || "") + delta;
    return;
  }
  const placeholder = makeAgentPlaceholder(message, phase, event);
  placeholder.reasoning = delta;
  message.timeline.push(placeholder);
}

export function appendTimelineAgentNote(message, event) {
  if (!Array.isArray(message.timeline)) message.timeline = [];
  const phase = String(event.phase || "agent").slice(0, AGENT_PHASE_MAX);
  const note = String(event.text || "").trim();
  if (!note) return;
  for (let i = message.timeline.length - 1; i >= 0; i--) {
    const candidate = message.timeline[i];
    if (candidate?.kind !== "agent" || candidate.phase !== phase) continue;
    if (candidate.status !== "running") break;
    candidate.notes = [...normalizeAgentNotes(candidate.notes), note].slice(-AGENT_NOTES_LIMIT);
    return;
  }
  const placeholder = makeAgentPlaceholder(message, phase, event);
  placeholder.notes = [note];
  message.timeline.push(placeholder);
}

export function appendTimelineAgentDelta(message, event) {
  // worker 的流式 content。按 phase 找到对应的 running agent 步，把 text 累积到 step.output。
  // 找不到时（agent_delta 比 agent running 事件先到）就先建一个 running 占位。
  if (!Array.isArray(message.timeline)) message.timeline = [];
  const phase = String(event.phase || "agent").slice(0, AGENT_PHASE_MAX);
  const delta = String(event.text || "");
  if (!delta) return;
  for (let i = message.timeline.length - 1; i >= 0; i--) {
    const candidate = message.timeline[i];
    if (candidate?.kind !== "agent" || candidate.phase !== phase) continue;
    if (candidate.status !== "running") break;
    candidate.output = String(candidate.output || "") + delta;
    return;
  }
  const placeholder = makeAgentPlaceholder(message, phase, event);
  placeholder.output = delta;
  message.timeline.push(placeholder);
}

export function resetTimelineAgentPhase(message, phase) {
  if (!Array.isArray(message?.timeline)) return;
  const target = String(phase || "").slice(0, AGENT_PHASE_MAX);
  if (!target) return;
  message.timeline = message.timeline.filter((step) => {
    if (!step || typeof step !== "object") return false;
    return !((step.kind === "agent" || step.kind === "search") && step.phase === target);
  });
}

export function timelineStepKey(step, index) {
  // v1.2.4：search 加 phase 前缀，避免 researcher 的 round 1 和主线/其他 Agent 的 round 1 互相覆盖
  if (step.kind === "search") return `s-${step.phase || "main"}-${Number(step.round) || index}`;
  // v1.2.7：agent step 的 id 现在带序号，是唯一的；旧 history 里只有 phase 的 id
  // 在 normalizeTimeline 里也会被去重补号，所以这里直接信赖 step.id 即可。
  if (step.kind === "agent") return `a-${step.id || agentStepId(step.phase || "agent")}`;
  return `r${index}`;
}

export function normalizeTimeline(value) {
  if (!Array.isArray(value)) return [];
  const result = [];
  // v1.2.7：旧 history 里同 phase 的多张卡片可能共享 id（agentStepId 旧实现只按 phase），
  // 这里按出现顺序补序号兜底，让重复 id 也能恢复成唯一。
  const phaseCounts = new Map();
  const seenIds = new Set();
  for (const step of value) {
    if (!step || typeof step !== "object") continue;
    if (step.kind === "reasoning") {
      const text = typeof step.text === "string" ? step.text : "";
      if (text) result.push({ kind: "reasoning", text });
    } else if (step.kind === "agent") {
      const text = typeof step.text === "string" ? step.text : "";
      // v1.2.5：reasoning/output 都属于 Agent 卡片状态，刷新后也要还原。
      const reasoning = typeof step.reasoning === "string" ? step.reasoning : "";
      const notes = normalizeAgentNotes(step.notes);
      const output = typeof step.output === "string" ? step.output : "";
      const rawStatus = VALID_AGENT_STATUS.has(step.status) ? step.status : "error";
      const status = rawStatus === "running" ? "error" : rawStatus;
      if (!(text || reasoning || output || notes.length)) continue;
      const phase = String(step.phase || "agent").slice(0, AGENT_PHASE_MAX);
      const nth = (phaseCounts.get(phase) || 0) + 1;
      phaseCounts.set(phase, nth);
      let id = String(step.id || "").slice(0, AGENT_ID_MAX);
      if (!id || seenIds.has(id)) id = `${agentStepId(phase)}-${nth}`;
      seenIds.add(id);
      const candidate = { kind: "agent", id, phase, status, reasoning, notes, output };
      const collapsedDefault = shouldCollapseAgentStep(candidate);
      // v1.2.9：历史里的 null/空字符串代表“没有耗时数据”，不能被 Number(...) 还原成 0ms。
      const durationMs = normalizeDurationMs(step.durationMs);
      result.push({
        kind: "agent",
        id,
        phase,
        status,
        name: String(step.name || "Agent").slice(0, AGENT_PHASE_MAX),
        text: text.slice(0, AGENT_TEXT_LIMIT),
        reasoning: reasoning.slice(0, AGENT_REASONING_LIMIT),
        notes,
        output: output.slice(0, AGENT_OUTPUT_LIMIT),
        durationMs,
        collapsed: typeof step.collapsed === "boolean" ? step.collapsed : collapsedDefault,
      });
    } else if (step.kind === "search") {
      const round = Number(step.round);
      if (!Number.isFinite(round) || round <= 0) continue;
      const rawStatus = VALID_SEARCH_STATUS.has(step.status) ? step.status : "done";
      const status = rawStatus === "searching" ? "error" : rawStatus;
      const results = Array.isArray(step.results)
        ? step.results
            .filter((item) => item && typeof item === "object" && typeof item.url === "string")
            .map((item) => ({
              title: String(item.title || "").slice(0, 240),
              url: String(item.url || ""),
              snippet: typeof item.snippet === "string" ? item.snippet.slice(0, 200) : "",
              content: typeof item.content === "string" ? item.content.slice(0, 200) : "",
              cite: typeof item.cite === "string" ? item.cite : "",
              citation_id: typeof item.citation_id === "string" ? item.citation_id : "",
              favicon: typeof item.favicon === "string" ? item.favicon : "",
            }))
            .slice(0, 20)
        : [];
      result.push({
        kind: "search",
        // v1.2.4：phase 标注哪个 Agent（或 main 主线）发的搜索，timelineStepKey 据此隔离
        phase: String(step.phase || "main").slice(0, AGENT_PHASE_MAX),
        round,
        query: String(step.query || "").slice(0, 500),
        status,
        error:
          typeof step.error === "string" && step.error
            ? step.error.slice(0, 400)
            : rawStatus === "searching"
              ? "搜索未完成（页面已刷新或请求已中断）"
              : "",
        results,
      });
    }
    if (result.length >= TIMELINE_MAX_STEPS) break;
  }
  return result;
}

// v2.0.5：把扁平的 trace span 列表（每个带 parentSpanId）按父子关系展开成
// 深度优先的有序列表，供瀑布图按 depth 缩进渲染成树。同层按 offsetMs 排序，
// 父不存在的当作根；环/自引用兜底为根，保证不丢 span 也不死循环。
function traceSpanOffset(span) {
  const value = Number(span && span.offsetMs);
  return Number.isFinite(value) ? value : 0;
}

export function buildTraceSpanTree(spans) {
  const list = Array.isArray(spans) ? spans.filter((span) => span && typeof span === "object") : [];
  const byId = new Map();
  for (const span of list) {
    if (span.spanId) byId.set(span.spanId, span);
  }
  const childrenByParent = new Map();
  const roots = [];
  for (const span of list) {
    const parentId = span.parentSpanId && byId.has(span.parentSpanId) && span.parentSpanId !== span.spanId ? span.parentSpanId : "";
    if (parentId) {
      if (!childrenByParent.has(parentId)) childrenByParent.set(parentId, []);
      childrenByParent.get(parentId).push(span);
    } else {
      roots.push(span);
    }
  }
  const ordered = [];
  const visited = new Set();
  const walk = (span, depth) => {
    if (!span || visited.has(span.spanId)) return;
    visited.add(span.spanId);
    ordered.push({ span, depth });
    const children = (childrenByParent.get(span.spanId) || []).slice().sort((a, b) => traceSpanOffset(a) - traceSpanOffset(b));
    for (const child of children) walk(child, depth + 1);
  };
  for (const root of roots.slice().sort((a, b) => traceSpanOffset(a) - traceSpanOffset(b))) {
    walk(root, 0);
  }
  // Any span left unvisited (dangling parent / cycle) is surfaced as a root so nothing is dropped.
  for (const span of list) {
    if (!visited.has(span.spanId)) ordered.push({ span, depth: 0 });
  }
  return ordered;
}
