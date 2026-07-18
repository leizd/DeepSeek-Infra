import {
  formatAttachmentsForPrompt,
  hasInlineAttachmentText,
  toApiAttachments,
} from "../../features/attachments/attachmentMapper";
import type { ChatMessage, ChatRequestPayload, JsonRecord } from "./types";

const DEFAULT_SYSTEM_PROMPT =
  "你是一个有帮助、回答准确、表达清晰的中文助手。涉及公式时使用标准 LaTeX，并明确说明重要假设与边界。";

export const CONTINUATION_TAIL_CHARS = 9_000;

export interface ChatRequestSettings {
  apiKey: string;
  tavilyApiKey: string;
  model: string;
  thinkingEnabled: boolean;
  searchEnabled: boolean;
}

function requestContent(message: ChatMessage): string {
  if (!hasInlineAttachmentText(message.attachments)) return message.content;
  const context = formatAttachmentsForPrompt(message.attachments);
  return message.content.trim() ? `${message.content}\n\n${context}` : context;
}

function requestMessage(message: ChatMessage, includeImages: boolean): JsonRecord {
  const record: JsonRecord = {
    role: message.role,
    content: requestContent(message),
  };
  const attachments = toApiAttachments(message.attachments, { includeImages });
  if (attachments.length) record.attachments = attachments;
  return record;
}

export function messagesToApiMessages(messages: readonly ChatMessage[]): JsonRecord[] {
  const usable = messages.filter((message) => !message.error && (message.content.trim() || message.attachments.length));
  const lastUserIndex = usable.reduce((latest, message, index) => (message.role === "user" ? index : latest), -1);
  return usable.map((message, index) => requestMessage(message, index === lastUserIndex));
}

export function basePayloadFields(settings: ChatRequestSettings): Omit<ChatRequestPayload, "messages"> {
  const apiKey = settings.apiKey.trim();
  const tavilyApiKey = settings.tavilyApiKey.trim();
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
  };
}

export function buildChatPayload(
  existingMessages: readonly ChatMessage[],
  userMessage: ChatMessage,
  settings: ChatRequestSettings,
): ChatRequestPayload {
  return {
    ...basePayloadFields(settings),
    messages: messagesToApiMessages([...existingMessages, userMessage]),
  };
}

export function buildRegenerationPayload(
  messagesBeforeAssistant: readonly ChatMessage[],
  settings: ChatRequestSettings,
): ChatRequestPayload {
  return {
    ...basePayloadFields(settings),
    messages: messagesToApiMessages(messagesBeforeAssistant),
  };
}

export function continuationPromptFor(message: ChatMessage): string {
  if (message.content.trim()) {
    return "请从上一条回答被中断的位置继续生成。不要重复已经输出过的内容，直接接着往下写。";
  }
  return "请继续完成刚才被中断的回答。上一轮可能停在思考、搜索或正文生成阶段，请接着完成最终答复，不要解释中断。";
}

function tailForContinuation(text: string, maxChars: number): string {
  return text.length > maxChars ? text.slice(text.length - maxChars) : text;
}

export function continuationContextFor(message: ChatMessage): string {
  const parts = [
    "这是一次继续生成请求。请保持原回答的语言、结构和上下文，从中断处继续；不要重新开始，不要重复已经输出过的正文。",
  ];
  if (message.reasoning) {
    parts.push(`上一次中断前已有推理过程（仅供衔接，不要原样复述给用户）：\n${tailForContinuation(message.reasoning, CONTINUATION_TAIL_CHARS)}`);
  }
  if (message.content) {
    parts.push(`上一次已经输出给用户的正文如下，请从最后一句之后继续：\n${tailForContinuation(message.content, CONTINUATION_TAIL_CHARS)}`);
  }
  return parts.join("\n\n");
}

export function buildContinuationPayload(
  messagesBeforeAssistant: readonly ChatMessage[],
  assistantMessage: ChatMessage,
  settings: ChatRequestSettings,
): ChatRequestPayload {
  const messages = messagesToApiMessages(messagesBeforeAssistant).filter((message) => String(message.content ?? "").trim());
  const partialContent = assistantMessage.content.trim();
  if (partialContent) messages.push({ role: "assistant", content: partialContent });
  messages.push({ role: "user", content: continuationPromptFor(assistantMessage) });
  return {
    ...basePayloadFields(settings),
    continuationContext: continuationContextFor(assistantMessage),
    messages,
  };
}
