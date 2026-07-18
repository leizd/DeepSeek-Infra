import { describe, expect, it } from "vitest";

import { exportFilename, messageToMarkdown } from "./messageActions";
import type { ChatMessage } from "../../domain/chat/types";

function assistantMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "assistant-1",
    role: "assistant",
    content: "正文内容",
    reasoning: "",
    createdAt: 1,
    phase: "done",
    streaming: false,
    attachments: [],
    timeline: [],
    systemNotes: [],
    ...overrides,
  };
}

describe("exportFilename", () => {
  it("sanitizes illegal filename characters", () => {
    expect(exportFilename('hello/world: test?')).toBe("hello-world-test.md");
  });

  it("falls back when the name is empty after sanitizing", () => {
    expect(exportFilename("///")).toBe("deepseek-reply.md");
    expect(exportFilename("")).toBe("deepseek-reply.md");
  });

  it("caps the base length", () => {
    const name = exportFilename("x".repeat(100));
    expect(name.length).toBeLessThanOrEqual(52);
  });
});

describe("messageToMarkdown", () => {
  it("includes quoted reasoning when present", () => {
    const output = messageToMarkdown(assistantMessage({ reasoning: "第一段\n第二段" }));
    expect(output).toContain("> 思考过程");
    expect(output).toContain("> 第一段");
    expect(output).toContain("> 第二段");
    expect(output).toContain("正文内容");
  });

  it("omits the reasoning block when empty and notes empty content", () => {
    expect(messageToMarkdown(assistantMessage())).not.toContain("思考过程");
    expect(messageToMarkdown(assistantMessage({ content: "" }))).toContain("（无正文内容）");
  });
});
