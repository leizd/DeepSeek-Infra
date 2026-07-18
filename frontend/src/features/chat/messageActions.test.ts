import { describe, expect, it } from "vitest";

import { exportFilename, messageToMarkdown, quoteAwareContent } from "./messageActions";
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

describe("quoteAwareContent", () => {
  it("returns the content untouched without a quote", () => {
    expect(quoteAwareContent("问题", null)).toBe("问题");
    expect(quoteAwareContent("问题", { messageId: "m", role: "assistant", text: "", fragment: "", isFragment: true })).toBe("问题");
  });

  it("wraps a fragment quote with the fragment prefix", () => {
    const output = quoteAwareContent("这段对吗", {
      messageId: "m1",
      role: "assistant",
      text: "完整回答",
      fragment: "第二行\n第三行",
      isFragment: true,
    });
    expect(output).toContain("关于上文中的这一段：");
    expect(output).toContain("> 第二行\n> 第三行");
    expect(output.endsWith("这段对吗")).toBe(true);
  });

  it("wraps a whole-message quote with the question prefix", () => {
    const output = quoteAwareContent("展开讲讲", {
      messageId: "m1",
      role: "assistant",
      text: "完整回答",
      fragment: "完整回答",
      isFragment: false,
    });
    expect(output).toContain("针对这段内容提问：");
    expect(output).toContain("> 完整回答");
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
