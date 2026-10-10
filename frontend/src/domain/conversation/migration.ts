import type { Attachment, ChatMessage, JsonRecord, StreamPhase, TimelineStep } from "../chat/types";
import { normalizeTimeline } from "../chat/agentTimeline";
import type { Conversation } from "./types";
import { createId } from "../../shared/createId";
import { titleFromMessages } from "./reducer";

export const DEFAULT_MODEL = "deepseek-v4-pro";
export const FAST_MODEL = "deepseek-v4-flash";

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function timestamp(value: unknown, fallback = Date.now()): number {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) return value;
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

export function migrateLegacyModel(value: unknown): string {
  const model = text(value).trim();
  if (!model) return DEFAULT_MODEL;
  if (model === FAST_MODEL || model === "deepseek-chat" || model.includes("flash")) return FAST_MODEL;
  if (model === DEFAULT_MODEL || model === "deepseek-reasoner" || model.includes("reason")) return DEFAULT_MODEL;
  return model;
}

function migrateAttachments(value: unknown): Attachment[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord).map((attachment) => ({
    id: text(attachment.id) || undefined,
    name: text(attachment.name) || text(attachment.filename) || "附件",
    type: text(attachment.type) || undefined,
    kind: text(attachment.kind) || undefined,
    size: typeof attachment.size === "number" ? attachment.size : undefined,
    fileId: text(attachment.fileId) || undefined,
    preview: text(attachment.preview) || undefined,
    text: text(attachment.text) || undefined,
    metadata: isRecord(attachment.metadata) ? attachment.metadata : undefined,
  }));
}

function migrateTimelineStep(step: JsonRecord): TimelineStep {
  const type = text(step.type) || text(step.kind) || "event";
  const notes = Array.isArray(step.notes) ? step.notes.map(text).filter(Boolean) : undefined;
  return {
    type,
    phase: text(step.phase) || undefined,
    status: text(step.status) || undefined,
    text: text(step.text) || undefined,
    payload: step,
    id: text(step.id) || undefined,
    name: text(step.name) || undefined,
    reasoning: text(step.reasoning) || undefined,
    notes,
    output: text(step.output) || undefined,
    durationMs: typeof step.durationMs === "number" ? step.durationMs : undefined,
    collapsed: typeof step.collapsed === "boolean" ? step.collapsed : undefined,
    search: isRecord(step.search) ? step.search : undefined,
  };
}

function migrateTimeline(value: unknown): TimelineStep[] {
  if (!Array.isArray(value)) return [];
  return normalizeTimeline(value.filter(isRecord).map(migrateTimelineStep));
}

function messagePhase(value: JsonRecord, role: "user" | "assistant"): StreamPhase {
  if (Boolean(value.interrupted)) return "interrupted";
  if (Boolean(value.error)) return "error";
  const phase = text(value.phase || value.streamPhase);
  if (["idle", "thinking", "searching", "tool", "agent", "answering", "done", "error", "interrupted"].includes(phase)) {
    return phase as StreamPhase;
  }
  return role === "assistant" ? "done" : "done";
}

export function migrateLegacyMessage(value: unknown): ChatMessage | null {
  if (!isRecord(value) || (value.role !== "user" && value.role !== "assistant")) return null;
  const role = value.role;
  const content = text(value.content);
  const reasoning = text(value.reasoning);
  const error = typeof value.error === "string" ? value.error : Boolean(value.error) ? content || "请求失败" : undefined;
  return {
    id: text(value.id) || createId("legacy-message"),
    role,
    content,
    reasoning,
    createdAt: timestamp(value.createdAt),
    phase: messagePhase(value, role),
    streaming: false,
    interrupted: Boolean(value.interrupted),
    attachments: migrateAttachments(value.attachments),
    timeline: migrateTimeline(value.timeline),
    systemNotes: Array.isArray(value.systemNotes) ? value.systemNotes.map(text).filter(Boolean).slice(0, 20) : [],
    search: isRecord(value.search) ? value.search : null,
    diagnostics: isRecord(value.diagnostics) ? value.diagnostics : null,
    usage: isRecord(value.usage) ? value.usage : undefined,
    model: migrateLegacyModel(value.model),
    error,
    errorCode: text(value.errorCode) || undefined,
    agentRunId: text(value.agentRunId) || undefined,
    agentRunStatus: text(value.agentRunStatus) || undefined,
    agentRunLastEventIndex: typeof value.agentRunLastEventIndex === "number" ? value.agentRunLastEventIndex : undefined,
    agentPlan: Array.isArray(value.agentRunPlan)
      ? value.agentRunPlan.filter(isRecord)
      : Array.isArray(value.agentPlan)
        ? value.agentPlan.filter(isRecord)
        : undefined,
    agentPlanLabel: text(value.agentAutoPlanLabel) || text(value.agentPlanLabel) || undefined,
  };
}

export function migrateLegacyConversation(value: unknown): Conversation | null {
  if (!isRecord(value)) return null;
  const messages = Array.isArray(value.messages)
    ? value.messages.map(migrateLegacyMessage).filter((message): message is ChatMessage => Boolean(message))
    : [];
  if (!messages.length) return null;
  const createdAt = timestamp(value.createdAt, messages[0]?.createdAt);
  return {
    id: text(value.id) || createId("legacy-conversation"),
    title: text(value.title).trim() || titleFromMessages(messages),
    messages,
    model: migrateLegacyModel(value.model),
    thinkingEnabled: Boolean(value.thinkingEnabled ?? migrateLegacyModel(value.model) === DEFAULT_MODEL),
    createdAt,
    updatedAt: timestamp(value.updatedAt, messages.at(-1)?.createdAt ?? createdAt),
    customTitle: Boolean(value.customTitle),
    autoTitleDone: Boolean(value.autoTitleDone),
    favorite: Boolean(value.favorite),
    tags: Array.isArray(value.tags) ? value.tags.map(text).filter(Boolean).slice(0, 8) : [],
    seekId: text(value.seekId) || undefined,
    projectId: text(value.projectId) || undefined,
    metadata: {
      contextSummary: text(value.contextSummary),
      contextSummaryFingerprint: text(value.contextSummaryFingerprint),
      contextSummaryMessageCount: Number(value.contextSummaryMessageCount) || 0,
      contextSummaryGeneration: Number(value.contextSummaryGeneration) || 0,
      contextPins: Array.isArray(value.contextPins) ? value.contextPins : [],
    },
  };
}
