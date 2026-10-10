import type { Attachment, TimelineStep } from "../chat/types";
import type { Conversation } from "./types";

/** 写入 checkpoint 信封的压缩记录（digest 只覆盖会话负载，本字段在信封上）。 */
export interface CheckpointCompaction {
  level: number;
  removedPreviewBytes: number;
  reason: "storage-pressure";
}

/** 纯压缩器的返回：压缩后的会话 + 实际剥离的预览字节数。 */
export interface StorageCompaction {
  conversation: Conversation;
  removedPreviewBytes: number;
}

/**
 * Level 1 剥离阈值：只剥离达到该大小的 `data:` 预览负载（UTF-8 字节）。
 * 更小的预览（如缩略图）保留；附件 name / type / size / fileId / 文本永远保留。
 */
export const PREVIEW_COMPACTION_MIN_BYTES = 1024;

/**
 * Level 2 上限：timeline step 原始 payload 允许的最大序列化字节数（UTF-8）。
 * 超限时整个 payload 替换为有界标记；step 的 output / status / text 等可见态保留。
 */
export const TIMELINE_RAW_PAYLOAD_CAP_BYTES = 8 * 1024;

const PREVIEW_FIELDS = ["preview", "thumbnail", "imagePreview"] as const;

function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).length;
}

/** 剥离单个附件的超大 `data:` 预览；无剥离时返回原对象（保持 identity）。 */
function stripAttachmentPreviews(attachment: Attachment): { attachment: Attachment; removedBytes: number } {
  let compacted: Attachment | null = null;
  let removedBytes = 0;
  for (const field of PREVIEW_FIELDS) {
    const value = attachment[field];
    if (typeof value !== "string" || !value.startsWith("data:")) continue;
    const bytes = utf8Bytes(value);
    if (bytes < PREVIEW_COMPACTION_MIN_BYTES) continue;
    compacted ??= { ...attachment };
    delete compacted[field];
    removedBytes += bytes;
  }
  return { attachment: compacted ?? attachment, removedBytes };
}

/** 为单个 step 的超大原始 payload 设上限；未超限时返回原对象（保持 identity）。 */
function capTimelinePayload(step: TimelineStep): TimelineStep {
  if (!step.payload) return step;
  let bytes: number;
  try {
    bytes = utf8Bytes(JSON.stringify(step.payload));
  } catch {
    // 不可序列化的 payload 由提交序列化阶段原样报错，压缩不擅自改写。
    return step;
  }
  if (bytes <= TIMELINE_RAW_PAYLOAD_CAP_BYTES) return step;
  return { ...step, payload: { compacted: true, originalBytes: bytes } };
}

/**
 * 存储压力降级用的纯压缩器，确定性：同一会话 + 同一级别 ⇒ 同一输出与同一
 * removedPreviewBytes（无时间、无随机、无外部状态）。
 * - level 1：剥离可重建的大尺寸图片预览（附件元信息与全部文本保留）；
 * - level 2：在 level 1 之上，再为超大 timeline 原始 payload 设上限。
 * 消息正文（user / assistant）在任何级别都不被触碰；无任何可压缩内容时
 * 返回原会话对象（identity 保持，不影响调用方的脏检测）。
 */
export function compactConversationForStorage(conversation: Conversation, level: number): StorageCompaction {
  if (level < 1) return { conversation, removedPreviewBytes: 0 };
  let removedPreviewBytes = 0;
  let changed = false;
  const messages = conversation.messages.map((message) => {
    let next = message;
    if (next.attachments.length) {
      const attachments = next.attachments.map((attachment) => {
        const stripped = stripAttachmentPreviews(attachment);
        if (stripped.removedBytes > 0) removedPreviewBytes += stripped.removedBytes;
        return stripped.attachment;
      });
      if (attachments.some((attachment, index) => attachment !== next.attachments[index])) {
        next = { ...next, attachments };
      }
    }
    if (level >= 2 && next.timeline.length) {
      const timeline = next.timeline.map(capTimelinePayload);
      if (timeline.some((step, index) => step !== next.timeline[index])) {
        next = { ...next, timeline };
      }
    }
    if (next !== message) changed = true;
    return next;
  });
  if (!changed) return { conversation, removedPreviewBytes: 0 };
  return { conversation: { ...conversation, messages }, removedPreviewBytes };
}
