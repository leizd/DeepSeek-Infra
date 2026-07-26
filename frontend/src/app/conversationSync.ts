/**
 * 跨标签页会话失效通道：BroadcastChannel `deepseek-conversation-sync`。
 * 每次成功提交（走锁或无锁）广播 conversation_committed；删除会话广播
 * conversation_deleted（由后续提交的 tombstone 工作消费，当前仅入档 schema）。
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
  if (typeof message.conversationId !== "string" || !message.conversationId) return null;
  if (typeof message.writerId !== "string" || !message.writerId) return null;
  if (message.type === "conversation_deleted") {
    return { type: "conversation_deleted", conversationId: message.conversationId, writerId: message.writerId };
  }
  if (message.type === "conversation_committed") {
    if (typeof message.revision !== "string" || !message.revision) return null;
    return {
      type: "conversation_committed",
      conversationId: message.conversationId,
      revision: message.revision,
      writerId: message.writerId,
      savedAt: typeof message.savedAt === "number" ? message.savedAt : 0,
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
