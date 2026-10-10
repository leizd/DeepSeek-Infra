import { describe, expect, it, vi } from "vitest";

import { streamAgentRunEvents } from "./agentRunFlow";
import { HttpClient } from "../../api/httpClient";
import type { ChatStreamEvent } from "../../domain/chat/types";

function ndjsonBody(events: readonly object[]): string {
  return events.map((event) => JSON.stringify(event)).join("\n") + "\n";
}

function clientReturning(items: readonly (string | Error)[]): HttpClient {
  let call = 0;
  const fetchImpl = vi.fn(async () => {
    const item = items[Math.min(call, items.length - 1)];
    call += 1;
    if (item instanceof Error) throw item;
    return new Response(item, { status: 200 });
  });
  return new HttpClient({ fetchImpl });
}

const noSleep = () => Promise.resolve();

describe("streamAgentRunEvents", () => {
  it("yields events in order, skips replayed cursor events and stops on done", async () => {
    const client = clientReturning([
      ndjsonBody([
        { type: "run_status", status: "running", index: 0, runId: "r" },
        { type: "content", text: "a", index: 1 },
        { type: "done", index: 2 },
        { type: "content", text: "after-done", index: 3 },
      ]),
    ]);
    const events: ChatStreamEvent[] = [];
    const result = await streamAgentRunEvents({
      runId: "r",
      after: -1,
      client,
      sleep: noSleep,
      onEvent: (event) => events.push(event),
      isComplete: () => false,
    });
    expect(events.map((event) => event.type)).toEqual(["run_status", "content", "done"]);
    expect(result).toEqual({ lastEventIndex: 2, completed: true });
  });

  it("resumes from the cursor after a dropped stream", async () => {
    const client = clientReturning([
      ndjsonBody([{ type: "content", text: "first", index: 0 }]),
      new Error("network down"),
      ndjsonBody([{ type: "content", text: "replay", index: 0 }, { type: "done", index: 1 }]),
    ]);
    const events: ChatStreamEvent[] = [];
    const result = await streamAgentRunEvents({
      runId: "r",
      client,
      sleep: noSleep,
      onEvent: (event) => events.push(event),
      isComplete: () => false,
    });
    expect(events.map((event) => (event.type === "content" ? event.text : event.type))).toEqual(["first", "done"]);
    expect(result.completed).toBe(true);
  });

  it("stops when isComplete reports the run settled after a clean stream end", async () => {
    const client = clientReturning([ndjsonBody([{ type: "run_status", status: "awaiting_plan", index: 0 }])]);
    const result = await streamAgentRunEvents({
      runId: "r",
      client,
      sleep: noSleep,
      onEvent: () => undefined,
      isComplete: () => true,
    });
    expect(result.completed).toBe(true);
  });

  it("throws after exhausting stalled reconnects", async () => {
    const client = clientReturning([new Error("down")]);
    await expect(
      streamAgentRunEvents({
        runId: "r",
        client,
        sleep: noSleep,
        maxStalledReconnects: 2,
        onEvent: () => undefined,
        isComplete: () => false,
      }),
    ).rejects.toThrow("Agent Run 流多次中断");
  });

  it("returns incomplete when the signal aborts", async () => {
    const controller = new AbortController();
    const fetchImpl = vi.fn(async () => {
      controller.abort();
      return new Response(ndjsonBody([]), { status: 200 });
    });
    const result = await streamAgentRunEvents({
      runId: "r",
      client: new HttpClient({ fetchImpl }),
      signal: controller.signal,
      sleep: noSleep,
      onEvent: () => undefined,
      isComplete: () => false,
    });
    expect(result.completed).toBe(false);
  });
});
