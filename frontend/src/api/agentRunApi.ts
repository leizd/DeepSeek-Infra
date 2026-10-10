import { httpClient, type HttpClient } from "./httpClient";
import type { ChatRequestPayload, JsonRecord } from "../domain/chat/types";

export const AGENT_PHASE_IDS = ["researcher", "coder", "reasoner", "critic"] as const;
export type AgentPhaseId = (typeof AGENT_PHASE_IDS)[number];
export const MAX_AGENT_PLAN_ITEMS = 4;

export type AgentRunStatus =
  | "created"
  | "planning"
  | "awaiting_plan"
  | "running"
  | "done"
  | "failed"
  | "cancelled"
  | "orphaned";

export const ACTIVE_RUN_STATUSES: readonly AgentRunStatus[] = ["created", "planning", "running"];

export interface AgentPlanItem {
  id: string;
  task: string;
  depends_on?: string[];
}

export interface AgentRun {
  runId: string;
  status: AgentRunStatus;
  nextIndex: number;
  plan: AgentPlanItem[];
  finalAnswer: string;
  diagnostics: JsonRecord;
}

export type AgentPreset = "full" | "auto" | "code" | "research" | "reason" | "critic" | "leader" | "plan";

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

const RUN_STATUSES = new Set<string>(["created", "planning", "awaiting_plan", "running", "done", "failed", "cancelled", "orphaned"]);

export function normalizeAgentPlanItem(raw: unknown): AgentPlanItem | null {
  if (!isRecord(raw)) return null;
  const id = asString(raw.id);
  if (!AGENT_PHASE_IDS.includes(id as AgentPhaseId)) return null;
  return {
    id,
    task: asString(raw.task).slice(0, 500),
    depends_on: Array.isArray(raw.depends_on) ? raw.depends_on.filter((item): item is string => typeof item === "string") : undefined,
  };
}

export function normalizeAgentRun(raw: unknown): AgentRun {
  const record = isRecord(raw) ? raw : {};
  const status = asString(record.status);
  return {
    runId: asString(record.runId),
    status: RUN_STATUSES.has(status) ? (status as AgentRunStatus) : "failed",
    nextIndex: typeof record.nextIndex === "number" ? record.nextIndex : 0,
    plan: Array.isArray(record.plan) ? record.plan.flatMap((item) => {
      const normalized = normalizeAgentPlanItem(item);
      return normalized ? [normalized] : [];
    }) : [],
    finalAnswer: asString(record.finalAnswer),
    diagnostics: isRecord(record.diagnostics) ? record.diagnostics : {},
  };
}

export function isActiveRunStatus(status: string | undefined): boolean {
  return ACTIVE_RUN_STATUSES.includes(status as AgentRunStatus);
}

export function agentPlanForPreset(preset: string): AgentPlanItem[] {
  switch (preset) {
    case "code":
      return [{ id: "coder", task: "分析代码结构与实现细节，给出可执行的修改方案" }];
    case "research":
      return [{ id: "researcher", task: "检索并整理与问题相关的资料来源" }];
    case "reason":
      return [{ id: "reasoner", task: "对问题进行逐步推理并给出结论" }];
    case "critic":
      return [{ id: "critic", task: "审查结论中的风险、漏洞与反例" }];
    case "full":
    default:
      return [
        { id: "researcher", task: "检索并整理与问题相关的资料来源" },
        { id: "coder", task: "分析涉及的代码与实现方案", depends_on: ["researcher"] },
        { id: "reasoner", task: "综合资料与代码分析进行推理", depends_on: ["researcher"] },
        { id: "critic", task: "审查结论中的风险与漏洞", depends_on: ["researcher", "coder", "reasoner"] },
      ];
  }
}

export function normalizeEditableAgentPlan(plan: readonly AgentPlanItem[]): AgentPlanItem[] {
  const items = plan
    .map((item) => ({ id: item.id, task: item.task.trim().slice(0, 500), depends_on: item.depends_on }))
    .filter((item) => AGENT_PHASE_IDS.includes(item.id as AgentPhaseId) && item.task);
  const deduped: AgentPlanItem[] = [];
  for (const item of items) {
    if (deduped.some((existing) => existing.id === item.id)) continue;
    deduped.push(item);
    if (deduped.length >= MAX_AGENT_PLAN_ITEMS) break;
  }
  if (!deduped.length) return agentPlanForPreset("full");
  const ids = new Set(deduped.map((item) => item.id));
  return deduped.map((item) => {
    const dependsOn = (item.depends_on ?? []).filter(
      (dependency) => ids.has(dependency) && dependency !== item.id && dependency !== "critic",
    );
    return dependsOn.length ? { ...item, depends_on: dependsOn } : { id: item.id, task: item.task };
  });
}

const SERVER_AGENT_PRESETS = ["full", "auto", "code", "research", "reason", "critic", "leader"] as const;

export function normalizeAgentPreset(value: string | undefined): AgentPreset {
  const preset = (value ?? "").trim();
  if (preset === "plan") return "plan";
  return (SERVER_AGENT_PRESETS as readonly string[]).includes(preset) ? (preset as AgentPreset) : "full";
}

export interface CreateAgentRunOptions {
  payload: ChatRequestPayload;
  confirmPlan?: boolean;
  agentPreset?: string;
  conversationId?: string;
  messageId?: string;
}

export interface AgentRunActionResult {
  started: boolean;
  run: AgentRun;
}

async function postRun<T>(path: string, body: unknown, client: HttpClient): Promise<T> {
  return client.postJson<T>(path, body);
}

export async function createAgentRun(
  options: CreateAgentRunOptions,
  client: HttpClient = httpClient,
): Promise<{ runId: string; run: AgentRun }> {
  const body = await postRun<{ runId?: unknown; run?: unknown }>("/api/agent-runs", {
    payload: { ...options.payload, agentMode: true },
    confirmPlan: Boolean(options.confirmPlan),
    agentPreset: options.agentPreset ?? "full",
    conversationId: options.conversationId ?? "",
    messageId: options.messageId ?? "",
  }, client);
  const run = normalizeAgentRun(body.run);
  return { runId: asString(body.runId) || run.runId, run };
}

export async function confirmAgentPlan(
  runId: string,
  options: { payload?: JsonRecord; plan?: AgentPlanItem[] },
  client: HttpClient = httpClient,
): Promise<AgentRunActionResult> {
  const body = await postRun<{ started?: unknown; run?: unknown }>(`/api/agent-runs/${encodeURIComponent(runId)}/plan`, {
    payload: options.payload ?? {},
    ...(options.plan ? { plan: options.plan } : {}),
  }, client);
  return { started: Boolean(body.started), run: normalizeAgentRun(body.run) };
}

export async function rerunAgentPhase(
  runId: string,
  options: { payload?: JsonRecord; agentId: string; resynthesize?: boolean },
  client: HttpClient = httpClient,
): Promise<AgentRunActionResult> {
  const body = await postRun<{ started?: unknown; run?: unknown }>(`/api/agent-runs/${encodeURIComponent(runId)}/rerun`, {
    payload: options.payload ?? {},
    agentId: options.agentId,
    resynthesize: options.resynthesize !== false,
  }, client);
  return { started: Boolean(body.started), run: normalizeAgentRun(body.run) };
}

export async function resumeAgentRun(
  runId: string,
  options: { payload?: JsonRecord } = {},
  client: HttpClient = httpClient,
): Promise<AgentRunActionResult> {
  const body = await postRun<{ started?: unknown; run?: unknown }>(`/api/agent-runs/${encodeURIComponent(runId)}/resume`, {
    payload: options.payload ?? {},
  }, client);
  return { started: Boolean(body.started), run: normalizeAgentRun(body.run) };
}

export async function getAgentRun(runId: string, client: HttpClient = httpClient): Promise<AgentRun> {
  const body = await client.json<{ run?: unknown }>(`/api/agent-runs/${encodeURIComponent(runId)}`);
  return normalizeAgentRun(body.run);
}

export function agentRunStreamUrl(runId: string, after: number): string {
  const cursor = Number.isFinite(after) ? Math.trunc(after) : -1;
  return `/api/agent-runs/${encodeURIComponent(runId)}/stream?after=${cursor}`;
}
