import { describe, expect, it } from "vitest";

import { createFileUploadTask, FileUploadError, isAbortError } from "./fileUploadApi";

interface FakeXHR {
  upload: { onprogress: ((event: ProgressEvent) => void) | null; onload: (() => void) | null };
  onload: (() => void) | null;
  onerror: (() => void) | null;
  ontimeout: (() => void) | null;
  onabort: (() => void) | null;
  status: number;
  responseText: string;
  timeout: number;
  opened: { method: string; url: string } | null;
  headers: Record<string, string>;
  sent: FormData | null;
  aborted: boolean;
  open(method: string, url: string): void;
  setRequestHeader(name: string, value: string): void;
  send(body: FormData): void;
  abort(): void;
}

function createFakeXHR(): FakeXHR {
  return {
    upload: { onprogress: null, onload: null },
    onload: null,
    onerror: null,
    ontimeout: null,
    onabort: null,
    status: 0,
    responseText: "",
    timeout: 0,
    opened: null,
    headers: {},
    sent: null,
    aborted: false,
    open(method, url) {
      this.opened = { method, url };
    },
    setRequestHeader(name, value) {
      this.headers[name] = value;
    },
    send(body) {
      this.sent = body;
    },
    abort() {
      this.aborted = true;
      this.onabort?.();
    },
  };
}

function asXHR(fake: FakeXHR): () => XMLHttpRequest {
  return () => fake as unknown as XMLHttpRequest;
}

function testFile(name: string): File {
  return new File(["hello"], name, { type: "text/plain" });
}

describe("createFileUploadTask", () => {
  it("posts multipart form data to /api/file-text with defaults", async () => {
    const fake = createFakeXHR();
    const task = createFileUploadTask({ files: [testFile("a.txt")], xhrFactory: asXHR(fake) });
    expect(fake.opened).toEqual({ method: "POST", url: "/api/file-text" });
    expect(fake.timeout).toBe(240_000);
    expect(fake.sent).toBeInstanceOf(FormData);
    expect(fake.sent?.getAll("files")).toHaveLength(1);
    expect(fake.headers.Authorization).toBeUndefined();
    task.cancel();
    await expect(task.promise).rejects.toThrow("上传已取消");
  });

  it("appends ocrEnabled and apiKey fields and auth header", async () => {
    const fake = createFakeXHR();
    const task = createFileUploadTask({
      files: [testFile("a.txt")],
      ocrEnabled: true,
      apiKey: "sk-test",
      authToken: "token-1",
      timeoutMs: 5_000,
      xhrFactory: asXHR(fake),
    });
    expect(fake.sent?.get("ocrEnabled")).toBe("1");
    expect(fake.sent?.get("apiKey")).toBe("sk-test");
    expect(fake.headers.Authorization).toBe("Bearer token-1");
    expect(fake.timeout).toBe(5_000);
    task.cancel();
    await expect(task.promise).rejects.toThrow("上传已取消");
  });

  it("reports progress and processing callbacks", async () => {
    const fake = createFakeXHR();
    const progress: number[] = [];
    let processing = false;
    const task = createFileUploadTask({
      files: [testFile("a.txt")],
      onProgress: (percent) => progress.push(percent),
      onProcessing: () => {
        processing = true;
      },
      xhrFactory: asXHR(fake),
    });
    fake.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 } as ProgressEvent);
    fake.upload.onload?.();
    fake.status = 200;
    fake.responseText = JSON.stringify({ files: [{ name: "a.txt" }], errors: [] });
    fake.onload?.();
    await expect(task.promise).resolves.toEqual({ files: [{ name: "a.txt" }], errors: [] });
    expect(progress).toEqual([50, 100]);
    expect(processing).toBe(true);
  });

  it("falls back to single file field when files array is missing", async () => {
    const fake = createFakeXHR();
    const task = createFileUploadTask({ files: [testFile("a.txt")], xhrFactory: asXHR(fake) });
    fake.status = 200;
    fake.responseText = JSON.stringify({ file: { name: "a.txt" } });
    fake.onload?.();
    await expect(task.promise).resolves.toEqual({ files: [{ name: "a.txt" }], errors: [] });
  });

  it("rejects with FileUploadError carrying status and code", async () => {
    const fake = createFakeXHR();
    const task = createFileUploadTask({ files: [testFile("big.bin")], xhrFactory: asXHR(fake) });
    fake.status = 413;
    fake.responseText = JSON.stringify({ error: "too large", code: "upload_too_large" });
    fake.onload?.();
    const failure = await task.promise.catch((reason: unknown) => reason);
    expect(failure).toBeInstanceOf(FileUploadError);
    expect((failure as FileUploadError).status).toBe(413);
    expect((failure as FileUploadError).code).toBe("upload_too_large");
  });

  it("rejects when response is not JSON", async () => {
    const fake = createFakeXHR();
    const task = createFileUploadTask({ files: [testFile("a.txt")], xhrFactory: asXHR(fake) });
    fake.status = 200;
    fake.responseText = "<html>";
    fake.onload?.();
    await expect(task.promise).rejects.toThrow("文件识别结果不是有效 JSON");
  });

  it("rejects on network error and timeout", async () => {
    const errorFake = createFakeXHR();
    const errorTask = createFileUploadTask({ files: [testFile("a.txt")], xhrFactory: asXHR(errorFake) });
    errorFake.onerror?.();
    await expect(errorTask.promise).rejects.toThrow("上传失败，请检查网络");

    const timeoutFake = createFakeXHR();
    const timeoutTask = createFileUploadTask({ files: [testFile("a.txt")], xhrFactory: asXHR(timeoutFake) });
    timeoutFake.ontimeout?.();
    await expect(timeoutTask.promise).rejects.toThrow("上传超时，请重试");
  });

  it("cancel aborts the request and rejects with AbortError", async () => {
    const fake = createFakeXHR();
    const task = createFileUploadTask({ files: [testFile("a.txt")], xhrFactory: asXHR(fake) });
    expect(task.active).toBe(true);
    task.cancel();
    expect(fake.aborted).toBe(true);
    const failure = await task.promise.catch((reason: unknown) => reason);
    expect(isAbortError(failure)).toBe(true);
    expect(task.active).toBe(false);
  });

  it("settles only once when multiple events fire", async () => {
    const fake = createFakeXHR();
    const task = createFileUploadTask({ files: [testFile("a.txt")], xhrFactory: asXHR(fake) });
    fake.status = 200;
    fake.responseText = JSON.stringify({ files: [], errors: [] });
    fake.onload?.();
    fake.onerror?.();
    await expect(task.promise).resolves.toEqual({ files: [], errors: [] });
  });
});
