import { describe, expect, it, vi } from "vitest";

import { HttpClient } from "./httpClient";
import { decodeChatStreamEvent, parseStreamEventLine, readChatStream, streamChat } from "./chatStream";

function streamingResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    { headers: { "Content-Type": "application/x-ndjson" } },
  );
}

describe("chat stream protocol", () => {
  it("decodes known and forward-compatible events", () => {
    expect(decodeChatStreamEvent({ type: "content", text: "hello" })).toEqual({
      type: "content",
      text: "hello",
      index: undefined,
      runId: undefined,
    });
    expect(decodeChatStreamEvent({ type: "future", value: 1 })).toMatchObject({
      type: "unknown",
      originalType: "future",
    });
    expect(decodeChatStreamEvent({ nope: true })).toBeNull();
    expect(parseStreamEventLine("not-json", { warn() {} })).toBeNull();
  });

  it("reads NDJSON across arbitrary chunk boundaries", async () => {
    const response = streamingResponse([
      '{"type":"reasoning","text":"a"}\n{"type":"cont',
      'ent","text":"b"}\n{"type":"done"}',
    ]);
    const events = [];
    for await (const event of readChatStream(response)) events.push(event);

    expect(events.map((event) => event.type)).toEqual(["reasoning", "content", "done"]);
  });

  it("posts a streaming payload through the shared HTTP client", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.method).toBe("POST");
      expect(JSON.parse(String(init?.body))).toMatchObject({ messages: [], stream: true });
      return streamingResponse(['{"type":"content","text":"ok"}\n{"type":"done"}\n']);
    });
    const client = new HttpClient({ fetchImpl });
    const events = [];
    for await (const event of streamChat({ messages: [] }, { client })) events.push(event);

    expect(fetchImpl).toHaveBeenCalledOnce();
    expect(events.map((event) => event.type)).toEqual(["content", "done"]);
  });

  it("cancels an interrupted reader and always releases its lock", async () => {
    const abortError = new DOMException("stopped", "AbortError");
    const reader = {
      read: vi.fn(async () => { throw abortError; }),
      cancel: vi.fn(async () => undefined),
      releaseLock: vi.fn(),
    };
    const response = { body: { getReader: () => reader } } as unknown as Response;
    const consume = async () => {
      for await (const _event of readChatStream(response)) {
        // No event is expected before interruption.
      }
    };

    await expect(consume()).rejects.toBe(abortError);
    expect(reader.cancel).toHaveBeenCalledOnce();
    expect(reader.releaseLock).toHaveBeenCalledOnce();
  });
});
