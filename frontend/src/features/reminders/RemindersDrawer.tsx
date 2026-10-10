import { useCallback, useEffect, useRef, useState } from "react";

import { createReminder, deleteReminder, listReminders, type Reminder } from "../../api/remindersApi";
import { clearReloadBlocker, setReloadBlocker } from "../../app/reloadBlockers";
import { useOverlay } from "../../contexts/OverlayContext";
import { Icon } from "../../shared/ui/Icon";

function formatDueAt(dueAt: string): string {
  const parsed = Date.parse(dueAt);
  if (!Number.isFinite(parsed)) return dueAt;
  return new Date(parsed).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function toLocalInputValue(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function RemindersDrawer() {
  const overlay = useOverlay();
  const open = overlay.activeOverlay === "reminders";
  const [reminders, setReminders] = useState<readonly Reminder[]>([]);
  const [error, setError] = useState("");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [dueAt, setDueAt] = useState(() => toLocalInputValue(new Date(Date.now() + 3_600_000)));
  const initialDueAtRef = useRef(dueAt);
  const hasFormDraft = Boolean(title.trim() || content.trim() || dueAt !== initialDueAtRef.current);

  const refresh = useCallback(async () => {
    setError("");
    try {
      setReminders(await listReminders());
    } catch (reason) {
      setError(reason instanceof Error && reason.message ? reason.message : "提醒加载失败");
    }
  }, []);

  useEffect(() => {
    if (open) void refresh();
  }, [open, refresh]);

  useEffect(() => {
    setReloadBlocker({
      id: "reminder-form-draft",
      label: "提醒表单尚未保存",
      kind: "unsaved",
      active: open && hasFormDraft,
    });
    return () => clearReloadBlocker("reminder-form-draft");
  }, [hasFormDraft, open]);

  if (!open) return null;
  return (
    <section className="settings-drawer workspace-drawer" role="dialog" aria-modal="true" aria-label="提醒">
      <div className="drawer-heading">
        <div>
          <p className="eyebrow">REMINDERS</p>
          <h2>提醒</h2>
        </div>
        <button type="button" aria-label="关闭提醒面板" onClick={overlay.closeOverlay}><Icon name="close" /></button>
      </div>
      <form
        className="skill-form"
        onSubmit={(event) => {
          event.preventDefault();
          const due = new Date(dueAt);
          if (!content.trim() || !Number.isFinite(due.getTime())) return;
          void createReminder({ title: title.trim() || "DeepSeek 提醒", content: content.trim(), dueAt: due.toISOString() })
            .then(() => {
              setTitle("");
              setContent("");
              void refresh();
            })
            .catch((reason: unknown) => setError(reason instanceof Error && reason.message ? reason.message : "提醒创建失败"));
        }}
      >
        <input aria-label="提醒标题" placeholder="标题（可选）" maxLength={120} value={title} onChange={(event) => setTitle(event.target.value)} />
        <input aria-label="提醒内容" placeholder="提醒内容" maxLength={2000} value={content} onChange={(event) => setContent(event.target.value)} />
        <input aria-label="提醒时间" type="datetime-local" value={dueAt} onChange={(event) => setDueAt(event.target.value)} />
        <button className="message-action primary" type="submit" disabled={!content.trim()}>创建提醒</button>
      </form>
      <p className="credential-note">聊天中输入「提醒我」加时间（如「明早 9 点提醒我提交日报」）也会自动创建提醒。</p>
      {error && <p className="message-error" role="alert">{error}</p>}
      <div className="workspace-list">
        {!reminders.length && <p className="history-empty">还没有提醒</p>}
        {reminders.map((reminder) => (
          <div className="workspace-item memory-item" key={reminder.id}>
            <div className="memory-entry">
              <small>{formatDueAt(reminder.dueAt)}{reminder.notified ? " · 已通知" : ""}</small>
              <p>{reminder.title}{reminder.content ? `：${reminder.content}` : ""}</p>
            </div>
            <div className="conversation-item-actions">
              <button
                className="conversation-tool danger"
                type="button"
                title="删除"
                aria-label={`删除提醒 ${reminder.title}`}
                onClick={() => void deleteReminder(reminder.id).then(refresh)}
              >
                <Icon name="close" />
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
