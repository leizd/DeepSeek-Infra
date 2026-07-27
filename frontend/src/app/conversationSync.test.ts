import { describe, expect, it } from "vitest";

import {
  CONVERSATION_SYNC_CHANNEL_NAME,
  createConversationSyncChannel,
  parseConversationSyncMessage,
  type BroadcastChannelLike,
  type ConversationSyncMessage,
} from "./conversationSync";

/** 模拟同一 channel name 下的多端口总线：发送端口自身不回收消息。 */
function createFakeHub() {
  const ports: { listeners: Set<(event: { data: unknown }) => void>; closed: boolean }[] = [];
  const factory = (): BroadcastChannelLike => {
    const port = { listeners: new Set<(event: { data: unknown }) => void>(), closed: false };
    ports.push(port);
    return {
      postMessage(message: unknown) {
        for (const peer of ports) {
          if (peer === port || peer.closed) continue;
          peer.listeners.forEach((listener) => listener({ data: message }));
        }
      },
      addEventListener(_type: "message", listener: (event: { data: unknown }) => void) {
        port.listeners.add(listener);
      },
      removeEventListener(_type: "message", listener: (event: { data: unknown }) => void) {
        port.listeners.delete(listener);
      },
      close() {
        port.closed = true;
        port.listeners.clear();
      },
    };
  };
  return { factory, ports };
}

describe("parseConversationSyncMessage", () => {
  it("accepts well-formed committed and deleted messages", () => {
    expect(parseConversationSyncMessage({
      type: "conversation_committed",
      conversationId: "c1",
      revision: "2.deadbeef",
      writerId: "deadbeef",
      savedAt: 123,
    })).toEqual({ type: "conversation_committed", conversationId: "c1", revision: "2.deadbeef", writerId: "deadbeef", savedAt: 123 });
    expect(parseConversationSyncMessage({
      type: "conversation_deleted",
      conversationId: "c1",
      writerId: "deadbeef",
    })).toEqual({ type: "conversation_deleted", conversationId: "c1", writerId: "deadbeef" });
    expect(parseConversationSyncMessage({
      type: "writer_claim",
      writerSessionId: "writer-1",
      documentInstanceId: "document-1",
    })).toEqual({ type: "writer_claim", writerSessionId: "writer-1", documentInstanceId: "document-1" });
    expect(parseConversationSyncMessage({
      type: "writer_claim_ack",
      writerSessionId: "writer-1",
      documentInstanceId: "document-1",
      targetDocumentInstanceId: "document-2",
    })).toEqual({
      type: "writer_claim_ack",
      writerSessionId: "writer-1",
      documentInstanceId: "document-1",
      targetDocumentInstanceId: "document-2",
    });
  });

  it("rejects malformed payloads", () => {
    expect(parseConversationSyncMessage(null)).toBeNull();
    expect(parseConversationSyncMessage("conversation_committed")).toBeNull();
    expect(parseConversationSyncMessage({ type: "conversation_committed", conversationId: "c1" })).toBeNull();
    expect(parseConversationSyncMessage({ type: "conversation_committed", conversationId: "c1", revision: "", writerId: "w" })).toBeNull();
    expect(parseConversationSyncMessage({ type: "conversation_deleted", conversationId: "", writerId: "w" })).toBeNull();
    expect(parseConversationSyncMessage({ type: "mystery", conversationId: "c1", writerId: "w" })).toBeNull();
    expect(parseConversationSyncMessage({ type: "writer_claim", writerSessionId: "w" })).toBeNull();
  });
});

describe("createConversationSyncChannel", () => {
  it("delivers messages between channels over the injected factory", () => {
    const hub = createFakeHub();
    const channelA = createConversationSyncChannel(hub.factory);
    const channelB = createConversationSyncChannel(hub.factory);
    const receivedByA: ConversationSyncMessage[] = [];
    const receivedByB: ConversationSyncMessage[] = [];
    channelA.subscribe((message) => receivedByA.push(message));
    channelB.subscribe((message) => receivedByB.push(message));

    channelA.post({ type: "conversation_committed", conversationId: "c1", revision: "2.aaaa0001", writerId: "aaaa0001", savedAt: 1 });
    channelB.post({ type: "conversation_deleted", conversationId: "c2", writerId: "bbbb0002" });

    expect(receivedByA).toEqual([{ type: "conversation_deleted", conversationId: "c2", writerId: "bbbb0002" }]);
    expect(receivedByB).toEqual([{ type: "conversation_committed", conversationId: "c1", revision: "2.aaaa0001", writerId: "aaaa0001", savedAt: 1 }]);
  });

  it("stops delivery after unsubscribe and closes the underlying port", () => {
    const hub = createFakeHub();
    const channelA = createConversationSyncChannel(hub.factory);
    const channelB = createConversationSyncChannel(hub.factory);
    const received: ConversationSyncMessage[] = [];
    const unsubscribe = channelB.subscribe((message) => received.push(message));

    channelA.post({ type: "conversation_deleted", conversationId: "c1", writerId: "aaaa0001" });
    unsubscribe();
    channelA.post({ type: "conversation_deleted", conversationId: "c2", writerId: "aaaa0001" });

    expect(received).toEqual([{ type: "conversation_deleted", conversationId: "c1", writerId: "aaaa0001" }]);
    expect(hub.ports[0]?.closed).toBe(true);
  });

  it("is a no-op when the factory yields no channel (no BroadcastChannel environment)", () => {
    const channel = createConversationSyncChannel(() => null);
    const received: ConversationSyncMessage[] = [];
    const unsubscribe = channel.subscribe((message) => received.push(message));
    expect(() => channel.post({ type: "conversation_deleted", conversationId: "c1", writerId: "w" })).not.toThrow();
    expect(received).toEqual([]);
    expect(() => unsubscribe()).not.toThrow();
  });

  it("degrades silently when the factory or the channel throws", () => {
    const throwing = createConversationSyncChannel(() => {
      throw new Error("no channel");
    });
    expect(() => throwing.post({ type: "conversation_deleted", conversationId: "c1", writerId: "w" })).not.toThrow();
    expect(() => throwing.subscribe(() => undefined)).not.toThrow();

    const broken = createConversationSyncChannel(() => ({
      postMessage() { throw new Error("closed"); },
      addEventListener() { /* ok */ },
      removeEventListener() { /* ok */ },
      close() { /* ok */ },
    }));
    broken.subscribe(() => undefined);
    expect(() => broken.post({ type: "conversation_deleted", conversationId: "c1", writerId: "w" })).not.toThrow();
  });

  it("exposes the shared channel name", () => {
    expect(CONVERSATION_SYNC_CHANNEL_NAME).toBe("deepseek-conversation-sync");
  });
});
