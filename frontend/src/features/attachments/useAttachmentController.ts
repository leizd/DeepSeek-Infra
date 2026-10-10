import { useCallback, useMemo, useReducer, useRef } from "react";

import {
  createFileUploadTask,
  DEFAULT_UPLOAD_LIMITS,
  isAbortError,
  FileUploadError,
  type FileUploadTask,
} from "../../api/fileUploadApi";
import { createId } from "../../shared/createId";
import type { Attachment } from "../../domain/chat/types";
import { useSettings } from "../../contexts/SettingsContext";
import {
  friendlyUploadError,
  guessKind,
  isImageFile,
  MAX_PENDING_ATTACHMENTS,
  normalizeUploadError,
  normalizeUploadedFile,
  validateUploadFiles,
  type FileRejection,
} from "./attachmentMapper";
import {
  attachmentQueueReducer,
  initialAttachmentQueueState,
  selectHasErrors,
  selectReadyAttachmentEntries,
  type AttachmentQueueState,
  type PendingAttachment,
  type ReadyAttachmentEntry,
} from "./attachmentReducer";
import { createImagePreviews } from "./imagePreview";

export interface AttachmentController {
  state: AttachmentQueueState;
  readyCount: number;
  hasErrors: boolean;
  addFiles(files: Iterable<File>): void;
  addReadyAttachments(attachments: readonly Attachment[]): void;
  cancelUpload(): void;
  removeItem(id: string): void;
  retryWithOcr(id: string): void;
  clear(): void;
  peekReadyAttachments(): ReadyAttachmentEntry[];
  commitReadyAttachments(ids: readonly string[]): void;
}

function pendingItem(file: File): PendingAttachment {
  return {
    id: createId("upload"),
    name: file.name || "upload",
    size: file.size,
    kind: guessKind(file.name, file.type),
    status: "uploading",
    progress: 0,
  };
}

function rejectionItem(rejection: FileRejection): PendingAttachment {
  return {
    id: createId("upload"),
    name: rejection.name,
    size: 0,
    kind: guessKind(rejection.name),
    status: "error",
    progress: 0,
    error: rejection.error,
  };
}

export function useAttachmentController(): AttachmentController {
  const settings = useSettings();
  const [state, dispatch] = useReducer(attachmentQueueReducer, initialAttachmentQueueState);
  const filesRef = useRef(new Map<string, File>());
  const taskRef = useRef<FileUploadTask | null>(null);

  const limits = settings.runtime?.uploadLimits ?? DEFAULT_UPLOAD_LIMITS;

  const runTask = useCallback(
    (items: readonly PendingAttachment[], files: readonly File[]) => {
      const task = createFileUploadTask({
        files,
        ocrEnabled: true,
        apiKey: settings.apiKey.trim() || undefined,
        onProgress: (percent) => dispatch({ type: "progressUpdated", percent }),
        onProcessing: () => dispatch({ type: "processingStarted" }),
      });
      taskRef.current = task;
      void task.promise
        .then((result) => {
          const successes = result.files.map((raw) => {
            const attachment = normalizeUploadedFile(raw);
            return { name: attachment.name, attachment };
          });
          const failures = result.errors.map((raw) => {
            const rejection = normalizeUploadError(raw, items[0]?.name ?? "附件");
            return { name: rejection.name, error: rejection.error };
          });
          dispatch({ type: "batchSettled", settlement: { successes, failures } });
        })
        .catch((reason: unknown) => {
          if (isAbortError(reason)) {
            dispatch({ type: "inFlightDiscarded" });
            return;
          }
          const message =
            reason instanceof FileUploadError
              ? friendlyUploadError(reason.message, reason.code, reason.status)
              : reason instanceof Error
                ? reason.message
                : "上传失败，请检查网络";
          dispatch({ type: "batchFailed", error: message });
        })
        .finally(() => {
          if (taskRef.current === task) taskRef.current = null;
        });
    },
    [settings.apiKey],
  );

  const decorateWithPreviews = useCallback((entries: readonly { item: PendingAttachment; file: File }[]) => {
    for (const { item, file } of entries) {
      if (!isImageFile(file.name, file.type)) continue;
      void createImagePreviews(file).then((previews) => {
        if (!previews) return;
        dispatch({ type: "previewReady", id: item.id, thumbnail: previews.thumbnail, imagePreview: previews.imagePreview });
      });
    }
  }, []);

  const addFiles = useCallback(
    (fileInput: Iterable<File>) => {
      const files = Array.from(fileInput);
      if (!files.length || state.uploading) return;
      const slotsAvailable = MAX_PENDING_ATTACHMENTS - state.items.length;
      const { accepted, rejected } = validateUploadFiles(files, limits, slotsAvailable);

      const entries = accepted.map((file) => ({ item: pendingItem(file), file }));
      const items = [...entries.map((entry) => entry.item), ...rejected.map(rejectionItem)];
      if (!items.length) return;
      for (const { item, file } of entries) filesRef.current.set(item.id, file);
      dispatch({ type: "batchStarted", items });
      decorateWithPreviews(entries);
      if (entries.length) {
        runTask(
          entries.map((entry) => entry.item),
          entries.map((entry) => entry.file),
        );
      }
    },
    [decorateWithPreviews, limits, runTask, state.items.length, state.uploading],
  );

  const cancelUpload = useCallback(() => {
    taskRef.current?.cancel();
  }, []);

  const addReadyAttachments = useCallback((attachments: readonly Attachment[]) => {
    if (!attachments.length) return;
    const items: PendingAttachment[] = attachments.map((attachment) => ({
      id: createId("upload"),
      name: attachment.name,
      size: attachment.size ?? 0,
      kind: attachment.kind ?? "text",
      status: "ready",
      progress: 100,
      thumbnail: attachment.thumbnail,
      imagePreview: attachment.imagePreview,
      attachment,
    }));
    dispatch({ type: "readyAdded", items });
  }, []);

  const removeItem = useCallback((id: string) => {
    filesRef.current.delete(id);
    dispatch({ type: "itemRemoved", id });
  }, []);

  const retryWithOcr = useCallback(
    (id: string) => {
      const item = state.items.find((entry) => entry.id === id);
      const file = filesRef.current.get(id);
      if (!item || !file || state.uploading) return;
      dispatch({ type: "retryStarted", id });
      runTask([item], [file]);
    },
    [runTask, state.items, state.uploading],
  );

  const clear = useCallback(() => {
    taskRef.current?.cancel();
    filesRef.current.clear();
    dispatch({ type: "cleared" });
  }, []);

  const peekReadyAttachments = useCallback(
    () => selectReadyAttachmentEntries(state),
    [state],
  );

  const commitReadyAttachments = useCallback((ids: readonly string[]) => {
    if (!ids.length) return;
    const committed = new Set(ids);
    for (const item of state.items) {
      if (item.status === "ready" && committed.has(item.id)) filesRef.current.delete(item.id);
    }
    dispatch({ type: "readyCommitted", ids });
  }, [state]);

  return useMemo(
    () => ({
      state,
      readyCount: state.items.filter((item) => item.status === "ready").length,
      hasErrors: selectHasErrors(state),
      addFiles,
      addReadyAttachments,
      cancelUpload,
      removeItem,
      retryWithOcr,
      clear,
      peekReadyAttachments,
      commitReadyAttachments,
    }),
    [state, addFiles, addReadyAttachments, cancelUpload, removeItem, retryWithOcr, clear, peekReadyAttachments, commitReadyAttachments],
  );
}
