export type JsonRecord = Record<string, unknown>;

export type MessageRole = "user" | "assistant" | "system";

export type StreamPhase =
  | "idle"
  | "thinking"
  | "searching"
  | "tool"
  | "agent"
  | "answering"
  | "done"
  | "error"
  | "interrupted";

export interface Attachment {
  id?: string;
  name: string;
  type?: string;
  kind?: string;
  size?: number;
  fileId?: string;
  projectId?: string;
  sourceAvailable?: boolean;
  preview?: string;
  text?: string;
  thumbnail?: string;
  imagePreview?: string;
  pageCount?: number;
  charCount?: number;
  chunkCount?: number;
  chunked?: boolean;
  truncated?: boolean;
  metadata?: JsonRecord;
}

export interface TimelineStep {
  type: string;
  phase?: string;
  status?: string;
  text?: string;
  payload?: JsonRecord;
  id?: string;
  name?: string;
  reasoning?: string;
  notes?: readonly string[];
  output?: string;
  durationMs?: number;
  collapsed?: boolean;
  search?: SearchSnapshot | null;
}

export type ChatDiagnostics = JsonRecord;
export type SearchSnapshot = JsonRecord;

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  reasoning: string;
  createdAt: number;
  updatedAt?: number;
  phase: StreamPhase;
  streaming: boolean;
  interrupted?: boolean;
  attachments: readonly Attachment[];
  timeline: readonly TimelineStep[];
  systemNotes: readonly string[];
  search?: SearchSnapshot | null;
  diagnostics?: ChatDiagnostics | null;
  model?: string;
  error?: string;
  errorCode?: string;
  agentRunId?: string;
  agentRunStatus?: string;
  agentRunLastEventIndex?: number;
  agentPlan?: readonly JsonRecord[];
  agentPlanLabel?: string;
}

export interface ChatRequestPayload extends JsonRecord {
  messages: readonly JsonRecord[];
  apiKey?: string;
  tavilyApiKey?: string;
  model?: string;
  stream?: boolean;
  agentMode?: boolean;
  thinkingEnabled?: boolean;
  searchEnabled?: boolean;
  searchMode?: "off" | "auto" | "on";
  systemPrompt?: string;
}

interface StreamEventBase {
  index?: number;
  runId?: string;
}

export interface TextStreamEvent extends StreamEventBase {
  type: "reasoning" | "content" | "system_note";
  text: string;
}

export interface SearchStreamEvent extends StreamEventBase {
  type: "search";
  search: SearchSnapshot | null;
}

export interface AgentStreamEvent extends StreamEventBase {
  type: "agent" | "agent_delta" | "agent_reasoning" | "agent_note" | "agent_search";
  phase?: string;
  status?: string;
  text?: string;
  search?: SearchSnapshot;
  payload: JsonRecord;
}

export interface MemorySuggestionStreamEvent extends StreamEventBase {
  type: "memory_suggestion";
  payload: JsonRecord;
}

export interface DoneStreamEvent extends StreamEventBase {
  type: "done";
  content?: string;
  reasoning?: string;
  model?: string;
  diagnostics?: ChatDiagnostics;
  usage?: JsonRecord;
}

export interface ErrorStreamEvent extends StreamEventBase {
  type: "error";
  error: string;
  code?: string;
}

export interface RunStatusStreamEvent extends StreamEventBase {
  type: "run_status";
  status: string;
}

export interface AgentPlanStreamEvent extends StreamEventBase {
  type: "agent_plan";
  plan: readonly JsonRecord[];
  label?: string;
}

export interface FinalResetStreamEvent extends StreamEventBase {
  type: "final_reset";
  scope?: string;
}

export interface AgentResetStreamEvent extends StreamEventBase {
  type: "agent_reset";
  phase?: string;
}

export interface AgentOutputStreamEvent extends StreamEventBase {
  type: "agent_output";
  payload: JsonRecord;
}

export interface UnknownStreamEvent extends StreamEventBase {
  type: "unknown";
  originalType: string;
  payload: JsonRecord;
}

export type ChatStreamEvent =
  | TextStreamEvent
  | SearchStreamEvent
  | AgentStreamEvent
  | MemorySuggestionStreamEvent
  | DoneStreamEvent
  | ErrorStreamEvent
  | RunStatusStreamEvent
  | AgentPlanStreamEvent
  | FinalResetStreamEvent
  | AgentResetStreamEvent
  | AgentOutputStreamEvent
  | UnknownStreamEvent;
