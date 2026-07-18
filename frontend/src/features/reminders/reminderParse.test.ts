import { describe, expect, it } from "vitest";

import { detectReminderFromText, parseReminderTime } from "./reminderParse";

const NOW = new Date("2026-07-18T10:00:00");

describe("parseReminderTime", () => {
  it("parses plain hours and keeps them today", () => {
    expect(parseReminderTime("提醒我 14 点开会", NOW)?.getHours()).toBe(14);
  });

  it("adds twelve hours for afternoon and evening periods", () => {
    expect(parseReminderTime("提醒我下午3点喝水", NOW)?.getHours()).toBe(15);
    expect(parseReminderTime("提醒我晚上9点打卡", NOW)?.getHours()).toBe(21);
    expect(parseReminderTime("提醒我今晚 8:30 复盘", NOW)?.getMinutes()).toBe(30);
  });

  it("shifts to tomorrow for 明早/明天 and rolls forward past times", () => {
    const tomorrow = parseReminderTime("提醒我明早 9 点提交日报", NOW);
    expect(tomorrow?.getDate()).toBe(NOW.getDate() + 1);
    expect(tomorrow?.getHours()).toBe(9);
    const rolled = parseReminderTime("提醒我 8 点", NOW);
    expect(rolled?.getDate()).toBe(NOW.getDate() + 1);
  });

  it("returns null without any time expression", () => {
    expect(parseReminderTime("提醒我一下", NOW)).toBeNull();
  });
});

describe("detectReminderFromText", () => {
  it("requires the 提醒我 marker", () => {
    expect(detectReminderFromText("明天 9 点开会", NOW)).toBeNull();
  });

  it("extracts the content after the marker", () => {
    const draft = detectReminderFromText("明天 9 点提醒我提交日报。", NOW);
    expect(draft).toMatchObject({ title: "DeepSeek 提醒", content: "提交日报" });
    expect(Date.parse(draft?.dueAt ?? "")).toBeGreaterThan(NOW.getTime());
  });
});
