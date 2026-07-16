import { createUploadTask } from "./upload_controller.js";

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
    return createUploadTask(files, {
      ...options,
      authToken: apiAuthToken,
      onProgress,
      onProcessing,
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
