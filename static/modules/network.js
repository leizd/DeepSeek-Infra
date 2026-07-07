export function createNetworkClient(storageKeys) {
  const apiAuthToken = initAuthToken(storageKeys);

  function authHeaders(headers = {}) {
    if (!apiAuthToken) return { ...headers };
    return { ...headers, "Authorization": "Bearer " + apiAuthToken };
  }

  function apiFetch(url, options = {}) {
    return fetch(url, { ...options, headers: authHeaders(options.headers || {}) });
  }

  function uploadFilesWithProgress(files, onProgress, onProcessing, options = {}) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const formData = new FormData();
      for (const file of files) {
        formData.append("files", file, file.name || "upload");
      }
      if (options.ocrEnabled) {
        formData.append("ocrEnabled", "1");
      }
      if (options.apiKey) {
        formData.append("apiKey", options.apiKey);
      }

      xhr.open("POST", "/api/file-text");
      if (apiAuthToken) {
        xhr.setRequestHeader("Authorization", "Bearer " + apiAuthToken);
      }

      xhr.upload.onprogress = (event) => {
        if (!event.lengthComputable) return;
        onProgress(Math.round((event.loaded / event.total) * 100));
      };
      xhr.upload.onload = () => {
        onProgress(100);
        onProcessing();
      };

      xhr.onload = () => {
        let data = {};
        try {
          data = JSON.parse(xhr.responseText || "{}");
        } catch {
          reject(new Error("文件识别结果不是有效 JSON"));
          return;
        }

        if (xhr.status < 200 || xhr.status >= 300) {
          if (data.code) {
            const error = new Error(data.error || "Upload failed");
            error.code = data.code;
            reject(error);
            return;
          }
          reject(new Error(data.error || `文件识别失败：${xhr.status}`));
          return;
        }

        resolve({
          files: Array.isArray(data.files) ? data.files : data.file ? [data.file] : [],
          errors: Array.isArray(data.errors) ? data.errors : [],
        });
      };

      xhr.onerror = () => reject(new Error("上传失败，请检查网络"));
      xhr.send(formData);
    });
  }

  return { apiAuthToken, authHeaders, apiFetch, uploadFilesWithProgress };
}

function initAuthToken(storageKeys) {
  const params = new URLSearchParams(window.location.search);
  var token = "";
  try {
    sessionStorage.removeItem(storageKeys.authToken);
  } catch {
    // Some privacy modes disable sessionStorage; cookie auth still works.
  }
  if (params.has("token")) {
    token = params.get("token") || "";
    params.delete("token");
    var query = params.toString();
    var nextUrl = window.location.pathname + (query ? "?" + query : "") + window.location.hash;
    window.history.replaceState(null, "", nextUrl);
  }
  return token;
}
