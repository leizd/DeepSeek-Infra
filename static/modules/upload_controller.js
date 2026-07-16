const DEFAULT_UPLOAD_TIMEOUT_MS = 240_000;

export function createUploadTask(files, options = {}) {
  const xhr = (options.xhrFactory || (() => new XMLHttpRequest()))();
  const formData = new FormData();
  for (const file of files) formData.append("files", file, file.name || "upload");
  if (options.ocrEnabled) formData.append("ocrEnabled", "1");
  if (options.apiKey) formData.append("apiKey", options.apiKey);

  let settled = false;
  let resolvePromise;
  let rejectPromise;
  const promise = new Promise((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });

  const resolveOnce = (value) => {
    if (settled) return;
    settled = true;
    resolvePromise(value);
  };
  const rejectOnce = (error) => {
    if (settled) return;
    settled = true;
    rejectPromise(error);
  };

  xhr.open("POST", options.url || "/api/file-text");
  xhr.timeout = positiveTimeout(options.timeoutMs);
  if (options.authToken) xhr.setRequestHeader("Authorization", `Bearer ${options.authToken}`);

  xhr.upload.onprogress = (event) => {
    if (!event.lengthComputable) return;
    options.onProgress?.(Math.round((event.loaded / event.total) * 100));
  };
  xhr.upload.onload = () => {
    options.onProgress?.(100);
    options.onProcessing?.();
  };
  xhr.onload = () => {
    let data = {};
    try {
      data = JSON.parse(xhr.responseText || "{}");
    } catch {
      rejectOnce(new Error("文件识别结果不是有效 JSON"));
      return;
    }
    if (xhr.status < 200 || xhr.status >= 300) {
      const error = new Error(data.error || `文件识别失败：${xhr.status}`);
      if (data.code) error.code = data.code;
      rejectOnce(error);
      return;
    }
    resolveOnce({
      files: Array.isArray(data.files) ? data.files : data.file ? [data.file] : [],
      errors: Array.isArray(data.errors) ? data.errors : [],
    });
  };
  xhr.onerror = () => rejectOnce(new Error("上传失败，请检查网络"));
  xhr.ontimeout = () => rejectOnce(new Error("上传超时，请重试"));
  xhr.onabort = () => rejectOnce(abortError());
  xhr.send(formData);

  return {
    promise,
    cancel() {
      if (!settled) xhr.abort();
    },
    get active() {
      return !settled;
    },
  };
}

function positiveTimeout(value) {
  const timeout = Number(value ?? DEFAULT_UPLOAD_TIMEOUT_MS);
  return Number.isFinite(timeout) && timeout > 0 ? timeout : DEFAULT_UPLOAD_TIMEOUT_MS;
}

function abortError() {
  try {
    return new DOMException("上传已取消", "AbortError");
  } catch {
    const error = new Error("上传已取消");
    error.name = "AbortError";
    return error;
  }
}
