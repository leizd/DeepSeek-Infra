import { describe, expect, it } from "vitest";

import {
  formatAttachmentsForPrompt,
  formatBytes,
  friendlyUploadError,
  guessKind,
  hasInlineAttachmentText,
  isImageFile,
  isOcrRetryable,
  normalizeUploadedFile,
  toApiAttachments,
  validateUploadFiles,
} from "./attachmentMapper";
import { DEFAULT_UPLOAD_LIMITS } from "../../api/fileUploadApi";
import type { Attachment } from "../../domain/chat/types";

function file(name: string, size: number): File {
  return new File([new Uint8Array(size)], name);
}

describe("formatBytes", () => {
  it("formats byte ranges", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(formatBytes(2 * 1024 * 1024 * 1024)).toBe("2.00 GB");
    expect(formatBytes(undefined)).toBe("0 B");
  });
});

describe("isImageFile / guessKind", () => {
  it("detects images by mime or extension", () => {
    expect(isImageFile("a.png")).toBe(true);
    expect(isImageFile("a.bin", "image/jpeg")).toBe(true);
    expect(isImageFile("a.pdf")).toBe(false);
    expect(guessKind("a.png")).toBe("image");
    expect(guessKind("notes.md")).toBe("md");
    expect(guessKind("noext")).toBe("text");
  });
});

describe("normalizeUploadedFile", () => {
  it("maps backend response fields", () => {
    const attachment = normalizeUploadedFile({
      name: "report.pdf",
      type: "application/pdf",
      kind: "pdf",
      size: 123,
      fileId: "abc123",
      projectId: "",
      sourceAvailable: true,
      text: "hello",
      preview: "hello",
      pageCount: 3,
      charCount: 1000,
      chunkCount: 2,
      chunked: true,
      truncated: false,
    });
    expect(attachment).toMatchObject({
      name: "report.pdf",
      kind: "pdf",
      fileId: "abc123",
      pageCount: 3,
      chunked: true,
    });
  });

  it("applies defaults for missing fields", () => {
    const attachment = normalizeUploadedFile({});
    expect(attachment.name).toBe("附件");
    expect(attachment.kind).toBe("text");
    expect(attachment.fileId).toBeUndefined();
  });
});

describe("friendlyUploadError / isOcrRetryable", () => {
  it("maps status and code to friendly messages", () => {
    expect(friendlyUploadError("x", undefined, 413)).toBe("文件超过大小限制，请压缩后再试");
    expect(friendlyUploadError("x", "unsupported_file")).toBe("不支持的文件类型");
    expect(friendlyUploadError("OCR required for scanned PDF")).toContain("OCR");
    expect(friendlyUploadError("boom")).toBe("boom");
    expect(friendlyUploadError("")).toBe("文件识别失败，请重试");
  });

  it("detects ocr-retryable errors", () => {
    expect(isOcrRetryable("scanned pdf requires ocr")).toBe(true);
    expect(isOcrRetryable("扫描版 PDF")).toBe(true);
    expect(isOcrRetryable("network down")).toBe(false);
    expect(isOcrRetryable(undefined)).toBe(false);
  });
});

describe("validateUploadFiles", () => {
  const limits = { ...DEFAULT_UPLOAD_LIMITS, fileMaxBytes: 100, requestMaxBytes: 150, maxFiles: 2 };

  it("accepts files within limits", () => {
    const { accepted, rejected } = validateUploadFiles([file("a.txt", 50), file("b.txt", 90)], limits, 5);
    expect(accepted.map((item) => item.name)).toEqual(["a.txt", "b.txt"]);
    expect(rejected).toEqual([]);
  });

  it("rejects oversize files and count overflow", () => {
    const { accepted, rejected } = validateUploadFiles(
      [file("a.txt", 50), file("big.txt", 101), file("c.txt", 10), file("d.txt", 10)],
      limits,
      5,
    );
    expect(accepted.map((item) => item.name)).toEqual(["a.txt", "c.txt"]);
    expect(rejected).toHaveLength(2);
    expect(rejected[0]).toMatchObject({ name: "big.txt" });
    expect(rejected[1].error).toContain("附件数量超出上限");
  });

  it("rejects when request total would overflow", () => {
    const { accepted, rejected } = validateUploadFiles([file("a.txt", 100), file("b.txt", 100)], { ...limits, maxFiles: 5 }, 5);
    expect(accepted).toHaveLength(1);
    expect(rejected[0].error).toContain("总大小限制");
  });

  it("respects remaining slots", () => {
    const { accepted, rejected } = validateUploadFiles([file("a.txt", 10), file("b.txt", 10)], limits, 1);
    expect(accepted).toHaveLength(1);
    expect(rejected[0].error).toContain("最多 1 个");
  });
});

describe("toApiAttachments", () => {
  const uploaded: Attachment = {
    name: "report.pdf",
    kind: "pdf",
    fileId: "abc",
    text: "cached text",
    charCount: 10,
    chunkCount: 1,
  };
  const legacyText: Attachment = { name: "notes.txt", kind: "text", text: "inline body", size: 100 };
  const image: Attachment = {
    name: "photo.png",
    kind: "image",
    fileId: "img1",
    thumbnail: "data:image/jpeg;base64,thumb",
    imagePreview: "data:image/jpeg;base64,full",
  };

  it("clears inline text when fileId is present", () => {
    const [record] = toApiAttachments([uploaded]);
    expect(record.fileId).toBe("abc");
    expect(record.text).toBe("");
  });

  it("keeps text for legacy inline attachments", () => {
    const [record] = toApiAttachments([legacyText]);
    expect(record.text).toBe("inline body");
  });

  it("includes imageData only when includeImages is enabled", () => {
    expect(toApiAttachments([image])[0].imageData).toBeUndefined();
    expect(toApiAttachments([image], { includeImages: true })[0].imageData).toBe("data:image/jpeg;base64,full");
  });

  it("falls back to thumbnail when full preview is missing", () => {
    const [record] = toApiAttachments([{ ...image, imagePreview: undefined }], { includeImages: true });
    expect(record.imageData).toBe("data:image/jpeg;base64,thumb");
  });

  it("drops attachments without fileId, text or image data", () => {
    expect(toApiAttachments([{ name: "empty.bin" }])).toEqual([]);
  });
});

describe("formatAttachmentsForPrompt", () => {
  it("inlines legacy text attachments with headers", () => {
    const output = formatAttachmentsForPrompt([{ name: "a.txt", size: 10, text: "hello" }]);
    expect(output).toContain("[用户上传的文件内容]");
    expect(output).toContain("--- 文件 1: a.txt (10 B) ---");
    expect(output).toContain("hello");
  });

  it("skips fileId-backed attachments and notes truncation", () => {
    const output = formatAttachmentsForPrompt([
      { name: "a.txt", fileId: "x", text: "skip me" },
      { name: "b.txt", text: "body", truncated: true },
    ]);
    expect(output).not.toContain("skip me");
    expect(output).toContain("[文件内容较长，已截断]");
  });

  it("caps total inline characters", () => {
    const output = formatAttachmentsForPrompt(
      [
        { name: "a.txt", text: "x".repeat(100) },
        { name: "b.txt", text: "y".repeat(100) },
      ],
      50,
    );
    expect(output).toContain("x".repeat(50));
    expect(output).toContain("[其余附件内容因长度限制未发送]");
  });

  it("hasInlineAttachmentText detects inline candidates", () => {
    expect(hasInlineAttachmentText([{ name: "a", text: "t" }])).toBe(true);
    expect(hasInlineAttachmentText([{ name: "a", text: "t", fileId: "f" }])).toBe(false);
    expect(hasInlineAttachmentText([])).toBe(false);
  });
});
