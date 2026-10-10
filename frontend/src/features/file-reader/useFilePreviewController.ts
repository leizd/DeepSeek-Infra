import { useCallback, useMemo, useReducer, useRef, useState } from "react";

import {
  FILE_READER_CHUNK_COUNT,
  loadFileReaderWindow,
  type FileReference,
} from "../../api/fileReaderApi";
import type { Attachment } from "../../domain/chat/types";
import {
  closedFilePreviewState,
  filePreviewReducer,
  nextChunkStart,
  pageCountFor,
  type FilePreviewMode,
  type FilePreviewState,
} from "./filePreviewReducer";

export interface LightboxState {
  items: readonly Attachment[];
  index: number;
}

export interface FilePreviewOpenOptions {
  chunkStart?: number;
}

export interface FilePreviewController {
  state: FilePreviewState;
  lightbox: LightboxState | null;
  pageCount: number;
  open(attachment: Attachment, options?: FilePreviewOpenOptions): void;
  close(): void;
  setMode(mode: FilePreviewMode): void;
  nextWindow(): void;
  previousWindow(): void;
  setPage(page: number): void;
  openLightbox(items: readonly Attachment[], index: number): void;
  closeLightbox(): void;
  stepLightbox(direction: 1 | -1): void;
}

function referenceFor(attachment: Attachment): FileReference | null {
  if (!attachment.fileId) return null;
  return { fileId: attachment.fileId, projectId: attachment.projectId ?? "" };
}

export function useFilePreviewController(): FilePreviewController {
  const [state, dispatch] = useReducer(filePreviewReducer, closedFilePreviewState);
  const [lightbox, setLightbox] = useState<LightboxState | null>(null);
  const requestRef = useRef(0);

  const loadWindow = useCallback((attachment: Attachment, chunkStart: number) => {
    const reference = referenceFor(attachment);
    if (!reference) return;
    const requestId = ++requestRef.current;
    dispatch({ type: "loadStarted" });
    void loadFileReaderWindow(reference, chunkStart, FILE_READER_CHUNK_COUNT)
      .then((response) => {
        if (requestRef.current !== requestId) return;
        dispatch({ type: "windowLoaded", window: response.window, chunks: response.chunks });
      })
      .catch((reason: unknown) => {
        if (requestRef.current !== requestId) return;
        dispatch({ type: "loadFailed", error: reason instanceof Error && reason.message ? reason.message : "文件读取失败，请重试" });
      });
  }, []);

  const open = useCallback(
    (attachment: Attachment, options: FilePreviewOpenOptions = {}) => {
      dispatch({ type: "opened", attachment });
      if (attachment.fileId && attachment.kind !== "image") {
        loadWindow(attachment, Math.max(1, Math.round(options.chunkStart ?? 1)));
      }
    },
    [loadWindow],
  );

  const close = useCallback(() => {
    requestRef.current += 1;
    dispatch({ type: "closed" });
  }, []);

  const setMode = useCallback(
    (mode: FilePreviewMode) => {
      dispatch({ type: "modeSet", mode });
      if (mode === "extracted" && state.attachment && !state.window) {
        loadWindow(state.attachment, 1);
      }
    },
    [loadWindow, state.attachment, state.window],
  );

  const stepWindow = useCallback(
    (direction: 1 | -1) => {
      if (!state.attachment || !state.window) return;
      loadWindow(state.attachment, nextChunkStart(state.window, direction));
    },
    [loadWindow, state.attachment, state.window],
  );

  const setPage = useCallback((page: number) => dispatch({ type: "pageSet", page }), []);

  const openLightbox = useCallback((items: readonly Attachment[], index: number) => {
    if (!items.length) return;
    setLightbox({ items, index: Math.min(Math.max(0, index), items.length - 1) });
  }, []);

  const closeLightbox = useCallback(() => setLightbox(null), []);

  const stepLightbox = useCallback((direction: 1 | -1) => {
    setLightbox((current) => {
      if (!current) return current;
      const index = Math.min(Math.max(0, current.index + direction), current.items.length - 1);
      return { ...current, index };
    });
  }, []);

  return useMemo(
    () => ({
      state,
      lightbox,
      open,
      close,
      setMode,
      nextWindow: () => stepWindow(1),
      previousWindow: () => stepWindow(-1),
      setPage,
      openLightbox,
      closeLightbox,
      stepLightbox,
      pageCount: state.attachment ? pageCountFor(state.attachment) : 1,
    }),
    [state, lightbox, open, close, setMode, stepWindow, setPage, openLightbox, closeLightbox, stepLightbox],
  );
}
