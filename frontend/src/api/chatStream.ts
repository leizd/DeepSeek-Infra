import { httpClient, type HttpClient } from "./httpClient";
import type {
  AgentPlanStreamEvent,
  AgentStreamEvent,
  ChatRequestPayload,
  ChatStreamEvent,
  JsonRecord,
} from "../domain/chat/types";

export interface StreamReaderOptions {
  waitUntilResumed?: () => Promise<void>;
  logger?: Pick<Console, "warn">;
}

export interface StreamChatOptions extends StreamReaderOptions {
  client?: HttpClient;
  signal?: AbortSignal;
}

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function baseFields(value: JsonRecord): { index?: number; runId?: string } {
  return {
    index: typeof value.index === "number" && Number.isFinite(value.index) ? value.index : undefined,
    runId: optionalString(value.runId),
  };
}

export function decodeChatStreamEvent(value: unknown): ChatStreamEvent | null {
  if (!isRecord(value) || typeof value.type !== "string" || !value.type.trim()) return null;
  const base = baseFields(value);

  switch (value.type) {
    case "reasoning":
    case "content":
    case "system_note":
      return { ...base, type: value.type, text: optionalString(value.text) ?? "" };
    case "search":
      return { ...base, type: "search", search: isRecord(value.search) ? value.search : null };
    case "agent":
    case "agent_delta":
    case "agent_reasoning":
    case "agent_note":
    case "agent_search":
      return {
        ...base,
        type: value.type,
        phase: optionalString(value.phase),
        status: optionalString(value.status),
        text: optionalString(value.text),
        search: isRecord(value.search) ? value.search : undefined,
        payload: value,
      } satisfies AgentStreamEvent;
    case "memory_suggestion":
      return { ...base, type: "memory_suggestion", payload: value };
    case "done":
      return {
        ...base,
        type: "done",
        content: optionalString(value.content),
        reasoning: optionalString(value.reasoning),
        model: optionalString(value.model),
        diagnostics: isRecord(value.diagnostics) ? value.diagnostics : undefined,
        usage: isRecord(value.usage) ? value.usage : undefined,
      };
    case "error":
      return {
        ...base,
        type: "error",
        error: optionalString(value.error) ?? "Stream request failed",
        code: optionalString(value.code),
      };
    case "run_status":
      return { ...base, type: "run_status", status: optionalString(value.status) ?? "unknown" };
    case "agent_plan":
      return {
        ...base,
        type: "agent_plan",
        plan: Array.isArray(value.plan) ? value.plan.filter(isRecord) : [],
        label: optionalString(value.label),
      } satisfies AgentPlanStreamEvent;
    case "final_reset":
      return { ...base, type: "final_reset", scope: optionalString(value.scope) };
    case "agent_reset":
      return { ...base, type: "agent_reset", phase: optionalString(value.phase) };
    case "agent_output":
      return { ...base, type: "agent_output", payload: value };
    default:
      return { ...base, type: "unknown", originalType: value.type, payload: value };
  }
}

export function parseStreamEventLine(line: string, logger: Pick<Console, "warn"> = console): ChatStreamEvent | null {
  const trimmed = line.trim();
  if (!trimmed) return null;
  try {
    return decodeChatStreamEvent(JSON.parse(trimmed) as unknown);
  } catch (error) {
    logger.warn("Skipped invalid stream event line", error);
    return null;
  }
}

export async function* readChatStream(
  response: Response,
  options: StreamReaderOptions = {},
): AsyncGenerator<ChatStreamEvent> {
  if (!response.body) throw new Error("Streaming response body is unavailable");
  const waitUntilResumed = options.waitUntilResumed ?? (() => Promise.resolve());
  const logger = options.logger ?? console;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completed = false;

  try {
    while (true) {
      await waitUntilResumed();
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        await waitUntilResumed();
        const event = parseStreamEventLine(line, logger);
        if (event) yield event;
      }
    }

    buffer += decoder.decode();
    const finalEvent = parseStreamEventLine(buffer, logger);
    if (finalEvent) yield finalEvent;
    completed = true;
  } finally {
    if (!completed) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export async function* streamChat(
  payload: ChatRequestPayload,
  options: StreamChatOptions = {},
): AsyncGenerator<ChatStreamEvent> {
  const client = options.client ?? httpClient;
  const response = await client.request("/api/chat", {
    method: "POST",
    signal: options.signal,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, stream: true }),
  });
  yield* readChatStream(response, options);
}
