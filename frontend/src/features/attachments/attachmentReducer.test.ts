import { describe, expect, it } from "vitest";

import {
  attachmentQueueReducer,
  initialAttachmentQueueState,
  selectHasErrors,
  selectReadyAttachments,
  type AttachmentQueueState,
  type PendingAttachment,
} from "./attachmentReducer";
import type { Attachment } from "../../domain/chat/types";

function uploadingItem(id: string, name: string): PendingAttachment {
  return { id, name, size: 10, kind: "text", status: "uploading", progress: 0 };
}

function stateWith(items: readonly PendingAttachment[], uploading = true): AttachmentQueueState {
  return { items, uploading };
}

describe("attachmentQueueReducer", () => {
  it("appends batch items and flips uploading", () => {
    const state = attachmentQueueReducer(initialAttachmentQueueState, { type: "batchStarted", items: [uploadingItem("1", "a.txt")] });
    expect(state.items).toHaveLength(1);
    expect(state.uploading).toBe(true);
  });

  it("keeps uploading false when the batch only carries rejected items", () => {
    const rejected: PendingAttachment = { ...uploadingItem("1", "big.bin"), status: "error", error: "too large" };
    const state = attachmentQueueReducer(initialAttachmentQueueState, { type: "batchStarted", items: [rejected] });
    expect(state.uploading).toBe(false);
    expect(state.items[0].status).toBe("error");
  });

  it("updates progress only for uploading items", () => {
    const initial = stateWith([
      uploadingItem("1", "a.txt"),
      { ...uploadingItem("2", "b.txt"), status: "processing" },
    ]);
    const state = attachmentQueueReducer(initial, { type: "progressUpdated", percent: 40 });
    expect(state.items[0].progress).toBe(40);
    expect(state.items[1].progress).toBe(0);
  });

  it("moves uploading items to processing", () => {
    const state = attachmentQueueReducer(stateWith([uploadingItem("1", "a.txt")]), { type: "processingStarted" });
    expect(state.items[0]).toMatchObject({ status: "processing", progress: 100 });
  });

  it("settles batch by matching names and keeps local previews", () => {
    const withPreview = attachmentQueueReducer(stateWith([uploadingItem("1", "a.png")]), {
      type: "previewReady",
      id: "1",
      thumbnail: "thumb",
      imagePreview: "full",
    });
    const settled = attachmentQueueReducer(withPreview, {
      type: "batchSettled",
      settlement: {
        successes: [{ name: "a.png", attachment: { name: "a.png", kind: "image", fileId: "f1" } as Attachment }],
        failures: [],
      },
    });
    expect(settled.uploading).toBe(false);
    expect(settled.items[0].status).toBe("ready");
    expect(settled.items[0].attachment).toMatchObject({ fileId: "f1", thumbnail: "thumb", imagePreview: "full" });
    expect(selectReadyAttachments(settled)).toHaveLength(1);
  });

  it("marks unmatched items and failures as errors with ocr flag", () => {
    const initial = stateWith([uploadingItem("1", "a.pdf"), uploadingItem("2", "b.pdf")]);
    const settled = attachmentQueueReducer(initial, {
      type: "batchSettled",
      settlement: { successes: [], failures: [{ name: "a.pdf", error: "scanned pdf requires OCR" }] },
    });
    expect(settled.items[0]).toMatchObject({ status: "error", ocrRetryAvailable: true });
    expect(settled.items[1]).toMatchObject({ status: "error", error: "文件识别结果缺失", ocrRetryAvailable: false });
    expect(selectHasErrors(settled)).toBe(true);
  });

  it("batchFailed fails every in-flight item but keeps settled ones", () => {
    const ready: PendingAttachment = {
      ...uploadingItem("0", "done.txt"),
      status: "ready",
      attachment: { name: "done.txt" },
    };
    const state = attachmentQueueReducer(stateWith([ready, uploadingItem("1", "a.txt")]), {
      type: "batchFailed",
      error: "上传失败，请检查网络",
    });
    expect(state.uploading).toBe(false);
    expect(state.items[0].status).toBe("ready");
    expect(state.items[1]).toMatchObject({ status: "error", error: "上传失败，请检查网络" });
  });

  it("retryStarted puts the item back into uploading state", () => {
    const errored: PendingAttachment = { ...uploadingItem("1", "a.pdf"), status: "error", error: "ocr", ocrRetryAvailable: true };
    const state = attachmentQueueReducer(stateWith([errored], false), { type: "retryStarted", id: "1" });
    expect(state.uploading).toBe(true);
    expect(state.items[0]).toMatchObject({ status: "uploading", progress: 0, error: undefined });
  });

  it("inFlightDiscarded drops uploading items, readyConsumed drops ready items", () => {
    const items: PendingAttachment[] = [
      uploadingItem("1", "a.txt"),
      { ...uploadingItem("2", "b.txt"), status: "ready", attachment: { name: "b.txt" } },
      { ...uploadingItem("3", "c.txt"), status: "error", error: "x" },
    ];
    const discarded = attachmentQueueReducer(stateWith(items), { type: "inFlightDiscarded" });
    expect(discarded.items.map((item) => item.id)).toEqual(["2", "3"]);
    expect(discarded.uploading).toBe(false);
    const consumed = attachmentQueueReducer(discarded, { type: "readyConsumed" });
    expect(consumed.items.map((item) => item.id)).toEqual(["3"]);
  });

  it("itemRemoved and cleared reset the queue", () => {
    const state = stateWith([uploadingItem("1", "a.txt"), uploadingItem("2", "b.txt")]);
    expect(attachmentQueueReducer(state, { type: "itemRemoved", id: "1" }).items).toHaveLength(1);
    expect(attachmentQueueReducer(state, { type: "cleared" })).toEqual(initialAttachmentQueueState);
  });

  it("readyAdded injects pre-uploaded attachments without touching uploading state", () => {
    const ready: PendingAttachment = {
      id: "s1",
      name: "shared.pdf",
      size: 10,
      kind: "pdf",
      status: "ready",
      progress: 100,
      attachment: { name: "shared.pdf", fileId: "f1" },
    };
    const state = attachmentQueueReducer(initialAttachmentQueueState, { type: "readyAdded", items: [ready] });
    expect(state.uploading).toBe(false);
    expect(selectReadyAttachments(state)).toEqual([{ name: "shared.pdf", fileId: "f1" }]);
  });
});
