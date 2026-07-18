import { createContext, useContext, useEffect, useMemo, useState, type PropsWithChildren } from "react";

import { loadChatRuntimeConfig, type ChatRuntimeConfig } from "../api/chatApi";
import { DEFAULT_MODEL } from "../domain/conversation/migration";

const preferenceKeys = {
  model: "deepseek-infra.model",
  thinking: "deepseek-infra.thinking-enabled",
  search: "deepseek-infra.search-enabled",
  agentMode: "deepseek-infra.agent-mode",
  agentPreset: "deepseek-infra.agent-preset",
} as const;

export interface SettingsContextValue {
  apiKey: string;
  tavilyApiKey: string;
  model: string;
  thinkingEnabled: boolean;
  searchEnabled: boolean;
  agentMode: boolean;
  agentPreset: string;
  runtime: ChatRuntimeConfig | null;
  loading: boolean;
  error: string;
  setApiKey(value: string): void;
  setTavilyApiKey(value: string): void;
  setModel(value: string): void;
  setThinkingEnabled(value: boolean): void;
  setSearchEnabled(value: boolean): void;
  setAgentMode(value: boolean): void;
  setAgentPreset(value: string): void;
  reloadRuntime(): Promise<void>;
}

const SettingsContext = createContext<SettingsContextValue | null>(null);

function storedPreference(key: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  return window.localStorage.getItem(key) ?? fallback;
}

export function SettingsProvider({ children }: PropsWithChildren) {
  const [apiKey, setApiKey] = useState("");
  const [tavilyApiKey, setTavilyApiKey] = useState("");
  const [model, setModelState] = useState(() => storedPreference(preferenceKeys.model, DEFAULT_MODEL));
  const [thinkingEnabled, setThinkingState] = useState(() => storedPreference(preferenceKeys.thinking, "1") !== "0");
  const [searchEnabled, setSearchState] = useState(() => storedPreference(preferenceKeys.search, "0") === "1");
  const [agentMode, setAgentModeState] = useState(() => storedPreference(preferenceKeys.agentMode, "0") === "1");
  const [agentPreset, setAgentPresetState] = useState(() => storedPreference(preferenceKeys.agentPreset, "full"));
  const [runtime, setRuntime] = useState<ChatRuntimeConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  async function reloadRuntime() {
    setLoading(true);
    setError("");
    try {
      const config = await loadChatRuntimeConfig();
      setRuntime(config);
      if (!config.models.includes(model)) setModelState(config.defaultModel);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取服务端配置");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void reloadRuntime();
  }, []);

  function setModel(value: string) {
    setModelState(value);
    window.localStorage.setItem(preferenceKeys.model, value);
  }

  function setThinkingEnabled(value: boolean) {
    setThinkingState(value);
    window.localStorage.setItem(preferenceKeys.thinking, value ? "1" : "0");
  }

  function setSearchEnabled(value: boolean) {
    setSearchState(value);
    window.localStorage.setItem(preferenceKeys.search, value ? "1" : "0");
  }

  function setAgentMode(value: boolean) {
    setAgentModeState(value);
    window.localStorage.setItem(preferenceKeys.agentMode, value ? "1" : "0");
  }

  function setAgentPreset(value: string) {
    setAgentPresetState(value);
    window.localStorage.setItem(preferenceKeys.agentPreset, value);
  }

  const value = useMemo<SettingsContextValue>(
    () => ({
      apiKey,
      tavilyApiKey,
      model,
      thinkingEnabled,
      searchEnabled,
      agentMode,
      agentPreset,
      runtime,
      loading,
      error,
      setApiKey,
      setTavilyApiKey,
      setModel,
      setThinkingEnabled,
      setSearchEnabled,
      setAgentMode,
      setAgentPreset,
      reloadRuntime,
    }),
    [apiKey, tavilyApiKey, model, thinkingEnabled, searchEnabled, agentMode, agentPreset, runtime, loading, error],
  );

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

export function useSettings(): SettingsContextValue {
  const value = useContext(SettingsContext);
  if (!value) throw new Error("useSettings must be used inside SettingsProvider");
  return value;
}
