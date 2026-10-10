import { describe, expect, it } from "vitest";

import {
  applyProjectContext,
  buildChatPayload,
  buildContinuationPayload,
  buildRegenerationPayload,
  continuationContextFor,
  continuationPromptFor,
  CONTINUATION_TAIL_CHARS,
  type ChatRequestSettings,
} from "./requestBuilder";
import type { ChatMessage } from "./types";

const settings: ChatRequestSettings = {
  apiKey: "",
  tavilyApiKey: "",
  model: "deepseek-v4-pro",
  thinkingEnabled: false,
  searchEnabled: false,
  memoryEnabled: false,
};

function message(role: "user" | "assistant", content: string): ChatMessage {
  return {
    id: `${role}-${content}`,
    role,
    content,
    reasoning: "",
    createdAt: 1,
    phase: "done",
    streaming: false,
    attachments: [],
    timeline: [],
    systemNotes: [],
  };
}

describe("buildChatPayload", () => {
  it("builds a normal chat payload without leaking unused search credentials", () => {
    const payload = buildChatPayload([message("user", "before"), message("assistant", "answer")], message("user", "next"), {
      apiKey: "  secret  ",
      tavilyApiKey: "search-secret",
      model: "deepseek-v4-pro",
      thinkingEnabled: true,
      searchEnabled: false,
      memoryEnabled: false,
    });

    expect(payload).toMatchObject({ apiKey: "secret", model: "deepseek-v4-pro", agentMode: false, stream: true });
    expect(payload.tavilyApiKey).toBeUndefined();
    expect(payload.messages).toEqual([
      { role: "user", content: "before" },
      { role: "assistant", content: "answer" },
      { role: "user", content: "next" },
    ]);
  });

  it("attaches normalized attachments to their message", () => {
    const outgoing: ChatMessage = {
      ...message("user", "看下这个文件"),
      attachments: [{ name: "report.pdf", kind: "pdf", fileId: "f1", text: "cached", charCount: 6, chunkCount: 1 }],
    };
    const payload = buildChatPayload([], outgoing, settings);
    expect(payload.messages).toHaveLength(1);
    const record = payload.messages[0];
    expect(record.attachments).toEqual([
      {
        fileId: "f1",
        projectId: "",
        name: "report.pdf",
        type: "",
        size: 0,
        kind: "pdf",
        charCount: 6,
        chunkCount: 1,
        text: "",
      },
    ]);
  });

  it("adds imageData only on the last user message", () => {
    const image = {
      name: "photo.png",
      kind: "image" as const,
      fileId: "img1",
      imagePreview: "data:image/jpeg;base64,full",
    };
    const earlier: ChatMessage = { ...message("user", "first"), attachments: [image] };
    const assistant = message("assistant", "seen");
    const latest: ChatMessage = { ...message("user", "second"), attachments: [image] };
    const payload = buildChatPayload([earlier, assistant], latest, settings);
    const [firstRecord, , lastRecord] = payload.messages as {
      attachments?: { imageData?: string }[];
    }[];
    expect(firstRecord.attachments?.[0]?.imageData).toBeUndefined();
    expect(lastRecord.attachments?.[0]?.imageData).toBe("data:image/jpeg;base64,full");
  });

  it("inlines legacy text attachments into the prompt content", () => {
    const outgoing: ChatMessage = {
      ...message("user", "总结一下"),
      attachments: [{ name: "notes.txt", kind: "text", text: "inline body", size: 11 }],
    };
    const payload = buildChatPayload([], outgoing, settings);
    const record = payload.messages[0];
    expect(String(record.content)).toContain("总结一下");
    expect(String(record.content)).toContain("[用户上传的文件内容]");
    expect(String(record.content)).toContain("inline body");
  });

  it("keeps attachment-only user messages in the payload", () => {
    const outgoing: ChatMessage = {
      ...message("user", ""),
      attachments: [{ name: "report.pdf", kind: "pdf", fileId: "f1" }],
    };
    const payload = buildChatPayload([], outgoing, settings);
    expect(payload.messages).toHaveLength(1);
    expect(payload.messages[0].attachments).toBeDefined();
  });

  it("adds memoryScope only when memory is enabled and a scope is provided", () => {
    const withMemory = buildChatPayload([], message("user", "hi"), { ...settings, memoryEnabled: true }, { memoryScope: "project:proj-1" });
    expect(withMemory.memoryScope).toBe("project:proj-1");
    const disabled = buildChatPayload([], message("user", "hi"), settings, { memoryScope: "project:proj-1" });
    expect(disabled.memoryScope).toBeUndefined();
    const globalScope = buildChatPayload([], message("user", "hi"), { ...settings, memoryEnabled: true }, { memoryScope: "" });
    expect(globalScope.memoryScope).toBeUndefined();
  });

  it("applyProjectContext stamps the project and merges its attachments", () => {
    const outgoing = applyProjectContext(message("user", "问题"), {
      projectId: "proj-1",
      projectAttachments: [{ name: "spec.pdf", kind: "pdf", fileId: "pf1", projectId: "proj-1" }],
    });
    expect(outgoing.projectId).toBe("proj-1");
    expect(outgoing.attachments).toHaveLength(1);
    const payload = buildChatPayload([], outgoing, settings);
    expect(payload.messages[0].projectId).toBe("proj-1");
    const plain = message("user", "x");
    expect(applyProjectContext(plain, {})).toBe(plain);
  });
});

describe("buildRegenerationPayload", () => {
  it("rebuilds the payload from the messages before the assistant", () => {
    const payload = buildRegenerationPayload([message("user", "question")], settings);
    expect(payload.messages).toEqual([{ role: "user", content: "question" }]);
    expect(payload.stream).toBe(true);
  });
});

describe("continuation helpers", () => {
  it("picks the prompt based on partial content", () => {
    expect(continuationPromptFor(message("assistant", "半截回答"))).toContain("接着往下写");
    expect(continuationPromptFor(message("assistant", ""))).toContain("接着完成最终答复");
  });

  it("builds a continuation context with reasoning and content tails", () => {
    const long = message("assistant", "c".repeat(CONTINUATION_TAIL_CHARS + 10));
    long.reasoning = "推理片段";
    const context = continuationContextFor(long);
    expect(context).toContain("继续生成请求");
    expect(context).toContain("推理片段");
    expect(context).toContain("c".repeat(CONTINUATION_TAIL_CHARS));
    expect(context).not.toContain("c".repeat(CONTINUATION_TAIL_CHARS + 1));
  });

  it("builds a continuation payload with partial answer and prompt", () => {
    const interrupted = message("assistant", "已输出的前半部分");
    const payload = buildContinuationPayload([message("user", "original question")], interrupted, settings);
    expect(payload.messages).toEqual([
      { role: "user", content: "original question" },
      { role: "assistant", content: "已输出的前半部分" },
      { role: "user", content: continuationPromptFor(interrupted) },
    ]);
    expect(String(payload.continuationContext)).toContain("已输出的前半部分");
  });

  it("omits the assistant stub when nothing was generated yet", () => {
    const interrupted = message("assistant", "");
    const payload = buildContinuationPayload([message("user", "q")], interrupted, settings);
    expect(payload.messages).toHaveLength(2);
    expect(payload.messages[1].role).toBe("user");
  });
});
