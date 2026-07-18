import { describe, expect, it } from "vitest";

import {
  clampPage,
  closedFilePreviewState,
  filePreviewReducer,
  initialPreviewMode,
  nextChunkStart,
  pageCountFor,
  supportsOriginalPreview,
} from "./filePreviewReducer";
import type { FileReaderWindow } from "../../api/fileReaderApi";
import type { Attachment } from "../../domain/chat/types";

const pdfAttachment: Attachment = { name: "report.pdf", kind: "pdf", fileId: "f1", sourceAvailable: true, pageCount: 10 };
const imageAttachment: Attachment = { name: "photo.png", kind: "image", fileId: "f2", sourceAvailable: true };
const textAttachment: Attachment = { name: "notes.txt", kind: "text", fileId: "f3", sourceAvailable: true };
const legacyAttachment: Attachment = { name: "old.txt", kind: "text", text: "inline" };

describe("supportsOriginalPreview / initialPreviewMode", () => {
  it("requires fileId and source availability", () => {
    expect(supportsOriginalPreview(legacyAttachment)).toBe(false);
    expect(supportsOriginalPreview({ ...pdfAttachment, sourceAvailable: false })).toBe(false);
    expect(supportsOriginalPreview(pdfAttachment)).toBe(true);
    expect(supportsOriginalPreview(imageAttachment)).toBe(true);
    expect(supportsOriginalPreview(textAttachment)).toBe(true);
  });

  it("picks initial mode by attachment kind", () => {
    expect(initialPreviewMode(legacyAttachment)).toBe("legacy");
    expect(initialPreviewMode(imageAttachment)).toBe("original");
    expect(initialPreviewMode(pdfAttachment)).toBe("extracted");
    expect(initialPreviewMode(textAttachment)).toBe("extracted");
  });
});

describe("pageCountFor / clampPage", () => {
  it("clamps pages into 1..pageCount", () => {
    expect(pageCountFor(pdfAttachment)).toBe(10);
    expect(pageCountFor({ name: "x" })).toBe(1);
    expect(clampPage(0, 10)).toBe(1);
    expect(clampPage(11, 10)).toBe(10);
    expect(clampPage(5, 10)).toBe(5);
  });
});

describe("nextChunkStart", () => {
  const window: FileReaderWindow = { chunkStart: 7, chunkEnd: 12, chunkCount: 6, totalChunks: 18, hasPrevious: true, hasNext: true };

  it("steps by the window size and clamps to bounds", () => {
    expect(nextChunkStart(window, 1)).toBe(13);
    expect(nextChunkStart(window, -1)).toBe(1);
    expect(nextChunkStart({ ...window, chunkStart: 1 }, -1)).toBe(1);
    expect(nextChunkStart({ ...window, chunkStart: 13 }, 1)).toBe(13);
  });
});

describe("filePreviewReducer", () => {
  it("opened resets state with the derived initial mode", () => {
    const dirty = { ...closedFilePreviewState, error: "stale", page: 4 };
    const state = filePreviewReducer(dirty, { type: "opened", attachment: pdfAttachment });
    expect(state).toMatchObject({ attachment: pdfAttachment, mode: "extracted", error: "", page: 1, chunks: [] });
  });

  it("modeSet to extracted triggers loading; original does not", () => {
    const opened = filePreviewReducer(closedFilePreviewState, { type: "opened", attachment: pdfAttachment });
    expect(filePreviewReducer(opened, { type: "modeSet", mode: "extracted" }).loading).toBe(true);
    expect(filePreviewReducer(opened, { type: "modeSet", mode: "original" }).loading).toBe(false);
  });

  it("windowLoaded stores window and chunks; loadFailed stores the error", () => {
    const loading = { ...closedFilePreviewState, attachment: pdfAttachment, loading: true };
    const loaded = filePreviewReducer(loading, { type: "windowLoaded", window: { chunkStart: 1, chunkEnd: 6, chunkCount: 6, totalChunks: 6, hasPrevious: false, hasNext: false }, chunks: [{ index: 1, start: 0, end: 5, lineStart: 1, lineEnd: 1, text: "hello" }] });
    expect(loaded.loading).toBe(false);
    expect(loaded.chunks).toHaveLength(1);
    const failed = filePreviewReducer(loading, { type: "loadFailed", error: "boom" });
    expect(failed).toMatchObject({ loading: false, error: "boom" });
  });

  it("pageSet clamps to the attachment page count", () => {
    const opened = filePreviewReducer(closedFilePreviewState, { type: "opened", attachment: pdfAttachment });
    expect(filePreviewReducer(opened, { type: "pageSet", page: 99 }).page).toBe(10);
    expect(filePreviewReducer(opened, { type: "pageSet", page: 3 }).page).toBe(3);
  });

  it("closed returns to the initial closed state", () => {
    const opened = filePreviewReducer(closedFilePreviewState, { type: "opened", attachment: pdfAttachment });
    expect(filePreviewReducer(opened, { type: "closed" })).toEqual(closedFilePreviewState);
  });
});
