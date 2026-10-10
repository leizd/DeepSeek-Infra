import type { Attachment } from "../../domain/chat/types";
import type { FileReaderChunk, FileReaderWindow } from "../../api/fileReaderApi";
import { isImageFile } from "../attachments/attachmentMapper";

export type FilePreviewMode = "extracted" | "original" | "legacy";

export interface FilePreviewState {
  attachment: Attachment | null;
  mode: FilePreviewMode;
  loading: boolean;
  error: string;
  window: FileReaderWindow | null;
  chunks: readonly FileReaderChunk[];
  page: number;
}

export const closedFilePreviewState: FilePreviewState = {
  attachment: null,
  mode: "legacy",
  loading: false,
  error: "",
  window: null,
  chunks: [],
  page: 1,
};

export type FilePreviewAction =
  | { type: "opened"; attachment: Attachment }
  | { type: "closed" }
  | { type: "modeSet"; mode: FilePreviewMode }
  | { type: "loadStarted" }
  | { type: "windowLoaded"; window: FileReaderWindow; chunks: readonly FileReaderChunk[] }
  | { type: "loadFailed"; error: string }
  | { type: "pageSet"; page: number };

export function supportsOriginalPreview(attachment: Attachment): boolean {
  if (!attachment.fileId || attachment.sourceAvailable === false) return false;
  if (attachment.kind === "pdf" || attachment.kind === "image") return true;
  if (isImageFile(attachment.name, attachment.type ?? "")) return true;
  const type = attachment.type ?? "";
  return type.startsWith("text/") || attachment.kind === "text" || attachment.kind === "md" || attachment.kind === "markdown";
}

export function initialPreviewMode(attachment: Attachment): FilePreviewMode {
  if (!attachment.fileId) return "legacy";
  if (attachment.kind === "image" || isImageFile(attachment.name, attachment.type ?? "")) {
    return supportsOriginalPreview(attachment) ? "original" : "legacy";
  }
  return "extracted";
}

export function pageCountFor(attachment: Attachment): number {
  return typeof attachment.pageCount === "number" && attachment.pageCount > 0 ? Math.round(attachment.pageCount) : 1;
}

export function clampPage(page: number, pageCount: number): number {
  return Math.min(Math.max(1, Math.round(page)), Math.max(1, pageCount));
}

export function nextChunkStart(window: FileReaderWindow, direction: 1 | -1): number {
  const step = Math.max(1, window.chunkCount);
  const target = window.chunkStart + direction * step;
  const maxStart = Math.max(1, window.totalChunks - step + 1);
  return Math.min(Math.max(1, target), maxStart);
}

export function filePreviewReducer(state: FilePreviewState, action: FilePreviewAction): FilePreviewState {
  switch (action.type) {
    case "opened":
      return {
        ...closedFilePreviewState,
        attachment: action.attachment,
        mode: initialPreviewMode(action.attachment),
        page: 1,
      };
    case "closed":
      return closedFilePreviewState;
    case "modeSet":
      return { ...state, mode: action.mode, error: "", loading: action.mode === "extracted" };
    case "loadStarted":
      return { ...state, loading: true, error: "" };
    case "windowLoaded":
      return { ...state, loading: false, window: action.window, chunks: action.chunks };
    case "loadFailed":
      return { ...state, loading: false, error: action.error };
    case "pageSet":
      return { ...state, page: state.attachment ? clampPage(action.page, pageCountFor(state.attachment)) : 1 };
  }
}
