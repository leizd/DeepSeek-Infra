import type { AgentStreamEvent, ChatMessage, JsonRecord, TimelineStep } from "./types";

export const AGENT_NOTES_LIMIT = 20;
export const AGENT_TEXT_LIMIT = 5_000;
export const AGENT_REASONING_LIMIT = 60_000;
export const AGENT_OUTPUT_LIMIT = 60_000;
export const TIMELINE_MAX_STEPS = 60;

export const WORKER_AGENT_ORDER = [
  { phase: "researcher", label: "资料" },
  { phase: "coder", label: "代码" },
  { phase: "reasoner", label: "推理" },
  { phase: "critic", label: "复核" },
] as const;

export interface AgentRunSummaryEntry {
  phase: string;
  label: string;
  status: string;
}

function capText(text: string, limit: number): string {
  return text.length > limit ? text.slice(text.length - limit) : text;
}

function capTimeline(timeline: readonly TimelineStep[]): readonly TimelineStep[] {
  return timeline.length > TIMELINE_MAX_STEPS ? timeline.slice(timeline.length - TIMELINE_MAX_STEPS) : timeline;
}

export function agentStepId(phase: string): string {
  return `agent-${phase || "unknown"}`;
}

function createAgentStepId(timeline: readonly TimelineStep[], phase: string): string {
  const base = agentStepId(phase);
  const taken = new Set(timeline.filter((step) => step.type === "agent").map((step) => step.id));
  if (!taken.has(base)) return base;
  let suffix = 2;
  while (taken.has(`${base}-${suffix}`)) suffix += 1;
  return `${base}-${suffix}`;
}

function makeAgentPlaceholder(timeline: readonly TimelineStep[], phase: string, name = ""): TimelineStep {
  return {
    type: "agent",
    id: createAgentStepId(timeline, phase),
    phase,
    name: name || phase || "Agent",
    status: "running",
    text: "",
    reasoning: "",
    notes: [],
    output: "",
  };
}

export function shouldCollapseAgentStep(step: TimelineStep): boolean {
  if (step.status !== "done") return false;
  if (step.phase === "leader") return false;
  return Boolean(step.output?.trim() || step.text?.trim() || step.reasoning?.trim());
}

export function appendTimelineAgent(timeline: readonly TimelineStep[], event: AgentStreamEvent): readonly TimelineStep[] {
  const phase = event.phase ?? "";
  const eventName = typeof event.payload.name === "string" ? event.payload.name : "";
  for (let index = timeline.length - 1; index >= 0; index -= 1) {
    const step = timeline[index];
    if (step.type === "agent" && step.phase === phase && step.status === "running") {
      const updated: TimelineStep = {
        ...step,
        name: eventName || step.name,
        status: event.status ?? step.status,
        text: event.text ?? step.text,
        durationMs: typeof event.payload.durationMs === "number" ? event.payload.durationMs : step.durationMs,
      };
      if (event.status === "done") {
        if (event.text && !updated.output) updated.output = event.text;
        updated.collapsed = shouldCollapseAgentStep(updated);
      }
      const next = [...timeline];
      next[index] = updated;
      return capTimeline(next);
    }
  }
  const card = makeAgentPlaceholder(timeline, phase, eventName);
  const next = [...timeline, {
    ...card,
    status: event.status ?? "running",
    text: event.text ?? "",
    output: event.status === "done" ? (event.text ?? "") : "",
    durationMs: typeof event.payload.durationMs === "number" ? event.payload.durationMs : undefined,
  }];
  return capTimeline(next);
}

export function appendTimelineAgentDelta(timeline: readonly TimelineStep[], event: AgentStreamEvent): readonly TimelineStep[] {
  const phase = event.phase ?? "";
  for (let index = timeline.length - 1; index >= 0; index -= 1) {
    const step = timeline[index];
    if (step.type === "agent" && step.phase === phase && step.status === "running") {
      const next = [...timeline];
      next[index] = { ...step, output: capText((step.output ?? "") + (event.text ?? ""), AGENT_OUTPUT_LIMIT) };
      return capTimeline(next);
    }
  }
  return capTimeline([...timeline, { ...makeAgentPlaceholder(timeline, phase), output: capText(event.text ?? "", AGENT_OUTPUT_LIMIT) }]);
}

export function appendTimelineAgentReasoning(timeline: readonly TimelineStep[], event: AgentStreamEvent): readonly TimelineStep[] {
  const phase = event.phase ?? "";
  for (let index = timeline.length - 1; index >= 0; index -= 1) {
    const step = timeline[index];
    if (step.type === "agent" && step.phase === phase && step.status === "running") {
      const next = [...timeline];
      next[index] = { ...step, reasoning: capText((step.reasoning ?? "") + (event.text ?? ""), AGENT_REASONING_LIMIT) };
      return capTimeline(next);
    }
  }
  return capTimeline([...timeline, { ...makeAgentPlaceholder(timeline, phase), reasoning: capText(event.text ?? "", AGENT_REASONING_LIMIT) }]);
}

export function appendTimelineAgentNote(timeline: readonly TimelineStep[], event: AgentStreamEvent): readonly TimelineStep[] {
  const phase = event.phase ?? "";
  const note = (event.text ?? "").trim();
  if (!note) return timeline;
  for (let index = timeline.length - 1; index >= 0; index -= 1) {
    const step = timeline[index];
    if (step.type === "agent" && step.phase === phase && step.status === "running") {
      const next = [...timeline];
      next[index] = { ...step, notes: [...(step.notes ?? []), note].slice(-AGENT_NOTES_LIMIT) };
      return capTimeline(next);
    }
  }
  return capTimeline([...timeline, { ...makeAgentPlaceholder(timeline, phase), notes: [note] }]);
}

export function resetTimelineAgentPhase(timeline: readonly TimelineStep[], phase: string): readonly TimelineStep[] {
  return timeline.filter((step) => !(step.phase === phase && (step.type === "agent" || step.type === "search")));
}

export function agentRunSummary(timeline: readonly TimelineStep[]): AgentRunSummaryEntry[] {
  return WORKER_AGENT_ORDER.map(({ phase, label }) => {
    const steps = timeline.filter((step) => step.type === "agent" && step.phase === phase);
    const latest = steps.at(-1);
    return { phase, label, status: latest?.status ?? "pending" };
  });
}

function agentStepBodyForReport(step: TimelineStep): string {
  if (step.status === "error") return step.output?.trim() || step.text?.trim() || "该 Agent 执行失败。";
  if (step.phase === "critic") {
    const source = step.output || step.text || "";
    const riskMatch = source.match(/#{1,4}\s*风险[^\n]*\n([\s\S]*?)(?=\n#{1,4}\s|$)/i);
    if (riskMatch?.[1]?.trim()) return riskMatch[1].trim();
    return source.trim() || step.notes?.join("\n") || "";
  }
  const source = step.output || step.text || "";
  const summaryMatch = source.match(/#{1,4}\s*(?:摘要|summary)[^\n]*\n([\s\S]*?)(?=\n#{1,4}\s|$)/i);
  if (summaryMatch?.[1]?.trim()) return summaryMatch[1].trim();
  return source.trim() || (step.notes ?? []).join("\n");
}

const REPORT_PHASE_TITLES: Record<string, string> = {
  researcher: "Researcher 摘要",
  coder: "Coder 摘要",
  reasoner: "Reasoner 摘要",
  critic: "Critic 风险",
};

export function agentExecutionReport(message: ChatMessage): string {
  const agentSteps = message.timeline.filter((step) => step.type === "agent");
  if (!agentSteps.length) return "";
  const sections = ["# Agent 执行报告"];
  const leader = [...agentSteps].reverse().find((step) => step.phase === "leader" && (step.output ?? step.text ?? "").includes("拆解"))
    ?? [...agentSteps].reverse().find((step) => step.phase === "leader");
  if (leader) {
    sections.push(`## Leader 拆解\n\n${(leader.output ?? leader.text ?? "").trim() || "（无内容）"}`);
  }
  for (const phase of Object.keys(REPORT_PHASE_TITLES)) {
    const step = [...agentSteps].reverse().find((candidate) => candidate.phase === phase);
    if (!step) continue;
    sections.push(`## ${REPORT_PHASE_TITLES[phase]}\n\n${agentStepBodyForReport(step) || "（无内容）"}`);
  }
  sections.push(`## 最终回答\n\n${message.content || "（无正文内容）"}`);
  return sections.join("\n\n");
}

export function normalizeTimeline(value: readonly TimelineStep[]): TimelineStep[] {
  const seen = new Map<string, number>();
  return value.map((step) => {
    let id = step.id;
    if (step.type === "agent") {
      const base = id ?? agentStepId(step.phase ?? "");
      const count = seen.get(base) ?? 0;
      seen.set(base, count + 1);
      id = count === 0 ? base : `${base}-${count + 1}`;
    }
    if (step.type === "agent" && step.status === "running") {
      return { ...step, id, status: "error", notes: [...(step.notes ?? []), "页面已刷新或请求已中断"], collapsed: false };
    }
    if (step.type === "search" && step.status === "searching") {
      return { ...step, id, status: "error" };
    }
    return id === step.id ? step : { ...step, id };
  });
}

export function mergeAgentSearchStep(timeline: readonly TimelineStep[], event: AgentStreamEvent): readonly TimelineStep[] {
  const phase = event.phase ?? "";
  const search = event.search ?? null;
  const round = search && typeof search === "object" ? (search as JsonRecord).round : undefined;
  const key = `s-${phase}-${typeof round === "number" ? round : "main"}`;
  const status = search && typeof search === "object" && typeof (search as JsonRecord).status === "string"
    ? ((search as JsonRecord).status as string)
    : "done";
  const existing = timeline.findIndex((step) => step.type === "search" && step.id === key);
  if (existing >= 0) {
    const next = [...timeline];
    next[existing] = { ...next[existing], search, status, phase };
    return capTimeline(next);
  }
  return capTimeline([...timeline, { type: "search", id: key, phase, status, search }]);
}
