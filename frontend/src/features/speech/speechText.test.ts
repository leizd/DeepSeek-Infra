import { describe, expect, it } from "vitest";

import {
  normalizeVoiceLanguage,
  preferredSpeechVoice,
  speechChunks,
  speechTextFromContent,
  splitLongSpeechSegment,
} from "./speechText";

describe("speechTextFromContent", () => {
  it("strips code fences, markdown syntax and citations", () => {
    const text = speechTextFromContent(
      "结论[^W1]：**重要** `code`，详见[文档](https://x.example)。\n```ts\nconst a = 1;\n```\n> 引用行",
    );
    expect(text).not.toContain("```");
    expect(text).not.toContain("[^W1]");
    expect(text).not.toContain("**");
    expect(text).toContain("重要");
    expect(text).toContain("code");
    expect(text).toContain("文档");
  });

  it("replaces formulas with a spoken placeholder and dedupes it", () => {
    expect(speechTextFromContent("已知 $x^2$ 和 $$y^2$$ 成立")).toBe("已知 公式略 和 公式略 成立");
    expect(speechTextFromContent("$a$ $b$")).toBe("公式略");
  });

  it("caps the output length", () => {
    expect(speechTextFromContent("x".repeat(9_000))).toHaveLength(8_000);
  });
});

describe("splitLongSpeechSegment / speechChunks", () => {
  it("keeps short segments intact and splits long ones by words", () => {
    expect(splitLongSpeechSegment("短句", 10)).toEqual(["短句"]);
    const pieces = splitLongSpeechSegment("alpha beta gamma delta", 12);
    expect(pieces.every((piece) => piece.length <= 12)).toBe(true);
    expect(pieces.join(" ")).toContain("alpha");
  });

  it("hard-splits unbreakable strings", () => {
    const pieces = splitLongSpeechSegment("x".repeat(25), 10);
    expect(pieces).toEqual(["x".repeat(10), "x".repeat(10), "x".repeat(5)]);
  });

  it("groups sentences into bounded chunks greedily", () => {
    const chunks = speechChunks("第一句。第二句。第三句。", 12);
    expect(chunks).toEqual(["第一句。 第二句。", "第三句。"]);
    expect(speechChunks("")).toEqual([]);
  });
});

describe("voice selection", () => {
  it("prefers an exact language match, then the base language", () => {
    const voices = [{ lang: "en-US" }, { lang: "zh-CN" }, { lang: "zh-HK" }];
    expect(preferredSpeechVoice("zh-CN", voices)?.lang).toBe("zh-CN");
    expect(preferredSpeechVoice("zh", [{ lang: "zh-HK" }])?.lang).toBe("zh-HK");
    expect(preferredSpeechVoice("fr-FR", voices)).toBeNull();
    expect(preferredSpeechVoice("zh-CN", [])).toBeNull();
  });

  it("normalizes empty language input", () => {
    expect(normalizeVoiceLanguage("  ")).toBe("zh-CN");
    expect(normalizeVoiceLanguage("en-US")).toBe("en-US");
  });
});
