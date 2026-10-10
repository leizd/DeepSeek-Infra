import { describe, expect, it, vi } from "vitest";

import { HttpClient } from "./httpClient";
import {
  filePageImageUrl,
  fileSourceUrl,
  loadFileChunk,
  loadFilePageText,
  loadFileReaderWindow,
} from "./fileReaderApi";

function fakeClient(payload: unknown): { client: HttpClient; fetchImpl: ReturnType<typeof vi.fn> } {
  const fetchImpl = vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 }));
  return { client: new HttpClient({ fetchImpl }), fetchImpl };
}

describe("loadFileReaderWindow", () => {
  it("posts the reference and normalizes the response", async () => {
    const { client, fetchImpl } = fakeClient({
      ok: true,
      file: { name: "report.pdf", kind: "pdf", fileId: "f1", projectId: "", sourceAvailable: true, pageCount: 4 },
      window: { chunkStart: 1, chunkEnd: 6, chunkCount: 6, totalChunks: 12, hasPrevious: false, hasNext: true },
      chunks: [{ index: 1, start: 0, end: 100, lineStart: 1, lineEnd: 9, text: "hello" }],
    });
    const response = await loadFileReaderWindow({ fileId: "f1" }, 7, 6, client);
    const [, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(String(fetchImpl.mock.calls[0][0])).toBe("/api/file-reader");
    expect(JSON.parse(String(init.body))).toEqual({ fileId: "f1", projectId: "", chunkStart: 7, chunkCount: 6 });
    expect(response.file.name).toBe("report.pdf");
    expect(response.window).toMatchObject({ chunkStart: 1, totalChunks: 12, hasNext: true });
    expect(response.chunks).toHaveLength(1);
  });

  it("clamps chunkStart and chunkCount into the accepted range", async () => {
    const { client, fetchImpl } = fakeClient({ window: {}, chunks: [] });
    await loadFileReaderWindow({ fileId: "f1" }, 0, 99, client);
    const [, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toMatchObject({ chunkStart: 1, chunkCount: 12 });
  });

  it("applies defaults for a sparse response", async () => {
    const { client } = fakeClient({});
    const response = await loadFileReaderWindow({ fileId: "f1", projectId: "p1" }, 3, 6, client);
    expect(response.file).toMatchObject({ fileId: "f1", projectId: "p1", kind: "text", sourceAvailable: true });
    expect(response.window).toMatchObject({ chunkStart: 3, chunkCount: 6 });
    expect(response.chunks).toEqual([]);
  });
});

describe("loadFilePageText", () => {
  it("normalizes the page payload", async () => {
    const { client, fetchImpl } = fakeClient({ ok: true, page: { index: 2, pageCount: 5, text: "page body", hasText: true } });
    const page = await loadFilePageText({ fileId: "f1" }, 2, client);
    const [, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ fileId: "f1", projectId: "", page: 2 });
    expect(page).toEqual({ index: 2, pageCount: 5, text: "page body", hasText: true });
  });
});

describe("loadFileChunk", () => {
  it("posts the chunk index and normalizes the payload", async () => {
    const { client, fetchImpl } = fakeClient({
      file: { name: "report.pdf", kind: "pdf", fileId: "f1", projectId: "" },
      chunk: { index: 3, text: "第三块内容" },
    });
    const chunk = await loadFileChunk({ fileId: "f1", projectId: "p1" }, 3, client);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toBe("/api/file-chunk");
    expect(JSON.parse(String(init.body))).toEqual({ fileId: "f1", projectId: "p1", chunkIndex: 3 });
    expect(chunk).toEqual({
      file: { name: "report.pdf", kind: "pdf", fileId: "f1", projectId: "" },
      index: 3,
      text: "第三块内容",
    });
  });

  it("clamps the chunk index and tolerates sparse payloads", async () => {
    const { client, fetchImpl } = fakeClient({});
    const chunk = await loadFileChunk({ fileId: "f1" }, 0, client);
    const [, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body)).chunkIndex).toBe(1);
    expect(chunk.file).toMatchObject({ name: "附件", fileId: "f1" });
    expect(chunk.text).toBe("");
  });
});

describe("url builders", () => {
  it("builds file-source urls with optional project and download flag", () => {
    expect(fileSourceUrl({ fileId: "f 1" })).toBe("/api/file-source?fileId=f+1");
    expect(fileSourceUrl({ fileId: "f1", projectId: "p1" }, true)).toBe("/api/file-source?fileId=f1&projectId=p1&download=1");
  });

  it("builds page image urls clamping page and scale", () => {
    expect(filePageImageUrl({ fileId: "f1" }, 0, 9)).toBe("/api/file-page-image?fileId=f1&page=1&scale=3");
    expect(filePageImageUrl({ fileId: "f1", projectId: "p1" }, 3, 1.6)).toBe(
      "/api/file-page-image?fileId=f1&projectId=p1&page=3&scale=1.6",
    );
  });
});
