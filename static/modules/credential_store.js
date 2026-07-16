const CREDENTIAL_FIELDS = Object.freeze({
  deepseek: { value: "apiKey", retained: "rememberKey" },
  tavily: { value: "tavilyKey", retained: "rememberTavilyKey" },
});

export function createCredentialSession(storageKeys, stores = {}) {
  const local = stores.localStorage || globalThis.localStorage;
  const session = stores.sessionStorage || globalThis.sessionStorage;

  function migrateLegacyLocalStorage() {
    for (const field of Object.values(CREDENTIAL_FIELDS)) {
      const valueKey = storageKeys[field.value];
      const retainedKey = storageKeys[field.retained];
      const legacyValueKey = valueKey.replace("deepseek-infra.", "deepseek-mobile.");
      const legacyRetainedKey = retainedKey.replace("deepseek-infra.", "deepseek-mobile.");
      const retained = safeGet(local, retainedKey) === "1" || safeGet(local, legacyRetainedKey) === "1";
      const value = safeGet(local, valueKey) || safeGet(local, legacyValueKey) || "";
      if (retained && value && !safeGet(session, valueKey)) {
        safeSet(session, retainedKey, "1");
        safeSet(session, valueKey, value);
      }
      for (const key of [valueKey, retainedKey, legacyValueKey, legacyRetainedKey]) safeRemove(local, key);
    }
  }

  function isRetained(name) {
    const field = credentialField(name);
    return safeGet(session, storageKeys[field.retained]) === "1";
  }

  function load(name) {
    const field = credentialField(name);
    return isRetained(name) ? safeGet(session, storageKeys[field.value]) || "" : "";
  }

  function setRetained(name, retained, value = "") {
    const field = credentialField(name);
    const valueKey = storageKeys[field.value];
    const retainedKey = storageKeys[field.retained];
    if (!retained) {
      safeRemove(session, retainedKey);
      safeRemove(session, valueKey);
      return;
    }
    safeSet(session, retainedKey, "1");
    update(name, value);
  }

  function update(name, value) {
    const field = credentialField(name);
    const valueKey = storageKeys[field.value];
    if (!isRetained(name) || !String(value || "").trim()) {
      safeRemove(session, valueKey);
      return;
    }
    safeSet(session, valueKey, String(value).trim());
  }

  migrateLegacyLocalStorage();
  return { isRetained, load, setRetained, update };
}

function credentialField(name) {
  const field = CREDENTIAL_FIELDS[name];
  if (!field) throw new Error(`Unknown credential field: ${name}`);
  return field;
}

function safeGet(storage, key) {
  try {
    return storage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function safeSet(storage, key, value) {
  try {
    storage?.setItem(key, value);
  } catch {
    // Privacy modes can disable session storage. The input still retains the value in memory.
  }
}

function safeRemove(storage, key) {
  try {
    storage?.removeItem(key);
  } catch {
    // Best effort cleanup for storage-disabled browser contexts.
  }
}
