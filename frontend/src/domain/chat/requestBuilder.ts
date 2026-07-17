import type { ChatMessage, ChatRequestPayload, JsonRecord } from "./types";

const DEFAULT_SYSTEM_PROMPT =
  "你是一个有帮助、回答准确、表达清晰的中文助手。涉及公式时使用标准 LaTeX，并明确说明重要假设与边界。";

export interface ChatRequestSettings {
  apiKey: string;
  tavilyApiKey: string;
  model: string;
  thinkingEnabled: boolean;
  searchEnabled: boolean;
}

function requestMessage(message: ChatMessage): JsonRecord {
  return {
    role: message.role,
    content: message.content,
  };
}

export function buildChatPayload(
  existingMessages: readonly ChatMessage[],
  userMessage: ChatMessage,
  settings: ChatRequestSettings,
): ChatRequestPayload {
  const apiKey = settings.apiKey.trim();
  const tavilyApiKey = settings.tavilyApiKey.trim();
  const messages = [...existingMessages, userMessage]
    .filter((message) => !message.error && message.content.trim())
    .map(requestMessage);

  return {
    ...(apiKey ? { apiKey } : {}),
    ...(settings.searchEnabled && tavilyApiKey ? { tavilyApiKey } : {}),
    model: settings.model,
    stream: true,
    agentMode: false,
    autoRoute: false,
    cascade: false,
    thinkingEnabled: settings.thinkingEnabled,
    searchEnabled: settings.searchEnabled,
    searchMode: settings.searchEnabled ? "auto" : "off",
    memoryEnabled: false,
    systemPrompt: DEFAULT_SYSTEM_PROMPT,
    messages,
  };
}
