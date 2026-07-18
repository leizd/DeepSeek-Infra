export interface ReminderDraft {
  title: string;
  content: string;
  dueAt: string;
}

const PERIOD_PATTERN = /(明早|明天|今天|今晚|早上|上午|下午|晚上)?\s*(\d{1,2})(?:[:：点](\d{1,2})?)?/;

export function parseReminderTime(text: string, now: Date = new Date()): Date | null {
  const match = text.match(PERIOD_PATTERN);
  if (!match) return null;
  const period = match[1] ?? "";
  let hour = Number(match[2]);
  if (!Number.isFinite(hour)) return null;
  if ((period === "下午" || period === "晚上" || period === "今晚") && hour < 12) hour += 12;
  const minute = Number(match[3]) || 0;
  const due = new Date(now.getTime());
  if (period === "明早" || period === "明天") due.setDate(due.getDate() + 1);
  due.setHours(Math.min(hour, 23), Math.min(minute, 59), 0, 0);
  if (due.getTime() <= now.getTime()) due.setDate(due.getDate() + 1);
  return due;
}

export function detectReminderFromText(text: string, now: Date = new Date()): ReminderDraft | null {
  if (!/提醒我/.test(text)) return null;
  const due = parseReminderTime(text, now);
  if (!due) return null;
  const content = text
    .split("提醒我")[1]
    ?.replace(/^[，,。.\s]+/, "")
    .replace(/[，,。.\s]+$/, "")
    .trim();
  return { title: "DeepSeek 提醒", content: content || text.trim().slice(0, 120), dueAt: due.toISOString() };
}
