import { agentRunStreamUrl } from "../../api/agentRunApi";
import { readChatStream } from "../../api/chatStream";
import { httpClient, type HttpClient } from "../../api/httpClient";
import type { ChatStreamEvent } from "../../domain/chat/types";

export const AGENT_STREAM_MAX_STALLED_RECONNECTS = 6;

export interface AgentRunFlowOptions {
  runId: string;
  after?: number;
  signal?: AbortSignal;
  client?: HttpClient;
  sleep?: (ms: number) => Promise<void>;
  maxStalledReconnects?: number;
  waitUntilResumed?: () => Promise<void>;
  onEvent(event: ChatStreamEvent): void;
  isComplete(): boolean;
}

export interface AgentRunFlowResult {
  lastEventIndex: number;
  completed: boolean;
}

function isTerminalEvent(event: ChatStreamEvent): boolean {
  return event.type === "done" || event.type === "error";
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

export async function streamAgentRunEvents(options: AgentRunFlowOptions): Promise<AgentRunFlowResult> {
  const client = options.client ?? httpClient;
  const sleep = options.sleep ?? defaultSleep;
  const maxStalled = options.maxStalledReconnects ?? AGENT_STREAM_MAX_STALLED_RECONNECTS;
  let cursor = options.after ?? -1;
  let stalled = 0;

  while (true) {
    if (options.signal?.aborted) return { lastEventIndex: cursor, completed: false };
    let progressed = false;
    try {
      const response = await client.request(agentRunStreamUrl(options.runId, cursor), {
        signal: options.signal,
        headers: { Accept: "application/x-ndjson" },
      });
      for await (const event of readChatStream(response, { waitUntilResumed: options.waitUntilResumed })) {
        if (typeof event.index === "number") {
          if (event.index <= cursor) continue;
          cursor = event.index;
        }
        progressed = true;
        options.onEvent(event);
        if (isTerminalEvent(event)) return { lastEventIndex: cursor, completed: true };
      }
      if (options.isComplete()) return { lastEventIndex: cursor, completed: true };
    } catch (reason) {
      if (options.signal?.aborted || (reason instanceof Error && reason.name === "AbortError")) {
        return { lastEventIndex: cursor, completed: false };
      }
      if (options.isComplete()) return { lastEventIndex: cursor, completed: true };
    }

    stalled = progressed ? 0 : stalled + 1;
    if (stalled > maxStalled) {
      throw new Error("Agent Run 流多次中断，未能读到最终结果");
    }
    await sleep(Math.min(2_000, 400 * stalled));
  }
}
