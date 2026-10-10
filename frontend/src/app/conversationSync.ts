/**
 * 跨标签页会话失效通道：BroadcastChannel `deepseek-conversation-sync`。
 * 每次成功提交（走锁或无锁）广播 conversation_committed；删除会话在 tombstone
 * 耐久落盘后广播 conversation_deleted（接收端本地干净则移除，本地脏则保留内容，
 * 下次提交被 tombstone 拒绝时物化为恢复副本）。
 * BroadcastChannel 缺失时退化为无操作通道；工厂可注入，便于测试。
 */

export const CONVERSATION_SYNC_CHANNEL_NAME = "deepseek-conversation-sync";

export type ConversationSyncMessage =
  | {
      type: "conversation_committed";
      conversationId: string;
      revision: string;
      writerId: string;
      savedAt: number;
    }
  | {
      type: "conversation_deleted";
      conversationId: string;
      writerId: string;
    }
  | {
      type: "writer_claim";
      writerSessionId: string;
      documentInstanceId: string;
    }
  | {
      type: "writer_claim_ack";
      writerSessionId: string;
      documentInstanceId: string;
      targetDocumentInstanceId: string;
    };

/** BroadcastChannel 的最小结构子集，测试可注入假实现。 */
export interface BroadcastChannelLike {
  postMessage(message: unknown): void;
  addEventListener(type: "message", listener: (event: { data: unknown }) => void): void;
  removeEventListener(type: "message", listener: (event: { data: unknown }) => void): void;
  close(): void;
}

export type BroadcastChannelFactory = () => BroadcastChannelLike | null;

export interface ConversationSyncChannel {
  post(message: ConversationSyncMessage): void;
  subscribe(listener: (message: ConversationSyncMessage) => void): () => void;
}

/** 入站消息形状校验：任何字段非法都丢弃（按无消息处理）。 */
export function parseConversationSyncMessage(data: unknown): ConversationSyncMessage | null {
  if (!data || typeof data !== "object") return null;
  const message = data as Partial<ConversationSyncMessage> & { type?: unknown };
  if (message.type === "writer_claim") {
    const claim = message as Partial<Extract<ConversationSyncMessage, { type: "writer_claim" }>>;
    if (typeof claim.writerSessionId !== "string" || !claim.writerSessionId
      || typeof claim.documentInstanceId !== "string" || !claim.documentInstanceId) return null;
    return { type: "writer_claim", writerSessionId: claim.writerSessionId, documentInstanceId: claim.documentInstanceId };
  }
  if (message.type === "writer_claim_ack") {
    const ack = message as Partial<Extract<ConversationSyncMessage, { type: "writer_claim_ack" }>>;
    if (typeof ack.writerSessionId !== "string" || !ack.writerSessionId
      || typeof ack.documentInstanceId !== "string" || !ack.documentInstanceId
      || typeof ack.targetDocumentInstanceId !== "string" || !ack.targetDocumentInstanceId) return null;
    return {
      type: "writer_claim_ack",
      writerSessionId: ack.writerSessionId,
      documentInstanceId: ack.documentInstanceId,
      targetDocumentInstanceId: ack.targetDocumentInstanceId,
    };
  }
  const record = data as Record<string, unknown>;
  if (typeof record.conversationId !== "string" || !record.conversationId) return null;
  if (typeof record.writerId !== "string" || !record.writerId) return null;
  if (message.type === "conversation_deleted") {
    return { type: "conversation_deleted", conversationId: record.conversationId, writerId: record.writerId };
  }
  if (message.type === "conversation_committed") {
    if (typeof record.revision !== "string" || !record.revision) return null;
    return {
      type: "conversation_committed",
      conversationId: record.conversationId,
      revision: record.revision,
      writerId: record.writerId,
      savedAt: typeof record.savedAt === "number" ? record.savedAt : 0,
    };
  }
  return null;
}

function defaultBroadcastChannelFactory(): BroadcastChannelLike | null {
  if (typeof BroadcastChannel === "undefined") return null;
  try {
    return new BroadcastChannel(CONVERSATION_SYNC_CHANNEL_NAME);
  } catch {
    return null;
  }
}

export function createConversationSyncChannel(
  factory: BroadcastChannelFactory = defaultBroadcastChannelFactory,
): ConversationSyncChannel {
  const listeners = new Set<(message: ConversationSyncMessage) => void>();
  let channel: BroadcastChannelLike | null | undefined;

  const onMessage = (event: { data: unknown }): void => {
    const message = parseConversationSyncMessage(event.data);
    if (message) listeners.forEach((listener) => listener(message));
  };

  /** 惰性打开通道：无 BroadcastChannel 的环境（或工厂返回 null）保持无操作。 */
  const ensureChannel = (): BroadcastChannelLike | null => {
    if (channel !== undefined) return channel;
    try {
      channel = factory();
    } catch {
      channel = null;
    }
    try {
      channel?.addEventListener("message", onMessage);
    } catch {
      channel = null;
    }
    return channel;
  };

  return {
    post(message) {
      try {
        ensureChannel()?.postMessage(message);
      } catch {
        // 通道失败只影响跨标签页刷新时效，静默降级。
      }
    },
    subscribe(listener) {
      listeners.add(listener);
      ensureChannel();
      return () => {
        listeners.delete(listener);
        if (listeners.size || !channel) return;
        try {
          channel.removeEventListener("message", onMessage);
          channel.close();
        } catch {
          // 关闭失败无害。
        }
        channel = undefined;
      };
    },
  };
}
