import { describe, expect, it } from "vitest";

import { parseMarkdownBlocks } from "./MarkdownContent";

describe("parseMarkdownBlocks", () => {
  it("parses headings, lists, quotes and fenced code without using raw HTML", () => {
    const blocks = parseMarkdownBlocks("## 标题\n\n- one\n- two\n\n> note\n\n```ts\nconst ok = true;\n```");
    expect(blocks).toEqual([
      { type: "heading", level: 2, text: "标题" },
      { type: "list", ordered: false, items: ["one", "two"] },
      { type: "quote", text: "note" },
      { type: "code", language: "ts", text: "const ok = true;" },
    ]);
  });
});
