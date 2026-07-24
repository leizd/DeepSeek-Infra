import type { Attachment } from "../../domain/chat/types";
import { isOcrRetryable } from "./attachmentMapper";

export type AttachmentUploadStatus = "uploading" | "processing" | "ready" | "error";

export interface PendingAttachment {
  id: string;
  name: string;
  size: number;
  kind: string;
  status: AttachmentUploadStatus;
  progress: number;
  error?: string;
  ocrRetryAvailable?: boolean;
  thumbnail?: string;
  imagePreview?: string;
  attachment?: Attachment;
}

export interface AttachmentQueueState {
  items: readonly PendingAttachment[];
  uploading: boolean;
}

export const initialAttachmentQueueState: AttachmentQueueState = { items: [], uploading: false };

export interface BatchSettlement {
  successes: readonly { name: string; attachment: Attachment }[];
  failures: readonly { name: string; error: string }[];
}

export type AttachmentQueueAction =
  | { type: "batchStarted"; items: readonly PendingAttachment[] }
  | { type: "readyAdded"; items: readonly PendingAttachment[] }
  | { type: "retryStarted"; id: string }
  | { type: "progressUpdated"; percent: number }
  | { type: "processingStarted" }
  | { type: "previewReady"; id: string; thumbnail: string; imagePreview: string }
  | { type: "batchSettled"; settlement: BatchSettlement }
  | { type: "batchFailed"; error: string }
  | { type: "itemRemoved"; id: string }
  | { type: "inFlightDiscarded" }
  | { type: "readyCommitted"; ids: readonly string[] }
  | { type: "cleared" };

function isInFlight(item: PendingAttachment): boolean {
  return item.status === "uploading" || item.status === "processing";
}

function mergeLocalPreviews(item: PendingAttachment, attachment: Attachment): Attachment {
  return {
    ...attachment,
    thumbnail: item.thumbnail ?? attachment.thumbnail,
    imagePreview: item.imagePreview ?? attachment.imagePreview,
  };
}

export function attachmentQueueReducer(
  state: AttachmentQueueState,
  action: AttachmentQueueAction,
): AttachmentQueueState {
  switch (action.type) {
    case "batchStarted":
      return {
        items: [...state.items, ...action.items],
        uploading: state.uploading || action.items.some(isInFlight),
      };
    case "readyAdded":
      return { ...state, items: [...state.items, ...action.items] };
    case "retryStarted":
      return {
        uploading: true,
        items: state.items.map((item) =>
          item.id === action.id ? { ...item, status: "uploading", progress: 0, error: undefined, ocrRetryAvailable: false } : item,
        ),
      };
    case "progressUpdated":
      return {
        ...state,
        items: state.items.map((item) => (item.status === "uploading" ? { ...item, progress: action.percent } : item)),
      };
    case "processingStarted":
      return {
        ...state,
        items: state.items.map((item) =>
          item.status === "uploading" ? { ...item, status: "processing", progress: 100 } : item,
        ),
      };
    case "previewReady":
      return {
        ...state,
        items: state.items.map((item) => {
          if (item.id !== action.id) return item;
          return {
            ...item,
            thumbnail: action.thumbnail,
            imagePreview: action.imagePreview,
            attachment: item.attachment
              ? { ...item.attachment, thumbnail: action.thumbnail, imagePreview: action.imagePreview }
              : item.attachment,
          };
        }),
      };
    case "batchSettled": {
      const successes = new Map(action.settlement.successes.map((entry) => [entry.name, entry.attachment]));
      const failures = new Map(action.settlement.failures.map((entry) => [entry.name, entry.error]));
      return {
        uploading: false,
        items: state.items.map((item) => {
          if (!isInFlight(item)) return item;
          const attachment = successes.get(item.name);
          if (attachment) {
            return { ...item, status: "ready", progress: 100, error: undefined, attachment: mergeLocalPreviews(item, attachment) };
          }
          const failure = failures.get(item.name) ?? "文件识别结果缺失";
          return {
            ...item,
            status: "error",
            progress: 0,
            error: failure,
            ocrRetryAvailable: isOcrRetryable(failure),
          };
        }),
      };
    }
    case "batchFailed":
      return {
        uploading: false,
        items: state.items.map((item) =>
          isInFlight(item)
            ? { ...item, status: "error", progress: 0, error: action.error, ocrRetryAvailable: isOcrRetryable(action.error) }
            : item,
        ),
      };
    case "itemRemoved":
      return { ...state, items: state.items.filter((item) => item.id !== action.id) };
    case "inFlightDiscarded":
      return { uploading: false, items: state.items.filter((item) => !isInFlight(item)) };
    case "readyCommitted": {
      const committed = new Set(action.ids);
      return { ...state, items: state.items.filter((item) => !(item.status === "ready" && committed.has(item.id))) };
    }
    case "cleared":
      return initialAttachmentQueueState;
  }
}

export interface ReadyAttachmentEntry {
  id: string;
  attachment: Attachment;
}

export function selectReadyAttachmentEntries(state: AttachmentQueueState): ReadyAttachmentEntry[] {
  return state.items
    .filter((item) => item.status === "ready" && item.attachment)
    .map((item) => ({ id: item.id, attachment: item.attachment as Attachment }));
}

export function selectReadyAttachments(state: AttachmentQueueState): Attachment[] {
  return state.items.filter((item) => item.status === "ready" && item.attachment).map((item) => item.attachment as Attachment);
}

export function selectHasErrors(state: AttachmentQueueState): boolean {
  return state.items.some((item) => item.status === "error");
}
