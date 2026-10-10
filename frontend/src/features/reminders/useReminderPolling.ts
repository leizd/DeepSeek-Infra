import { useEffect, useRef } from "react";

import { fetchDueReminders, type Reminder } from "../../api/remindersApi";

export const REMINDER_POLL_INTERVAL_MS = 60_000;

export function notificationPermission(): NotificationPermission | "unsupported" {
  if (typeof window === "undefined" || !("Notification" in window)) return "unsupported";
  return Notification.permission;
}

export async function ensureNotificationPermission(): Promise<boolean> {
  const permission = notificationPermission();
  if (permission === "granted") return true;
  if (permission !== "default") return false;
  try {
    return (await Notification.requestPermission()) === "granted";
  } catch {
    return false;
  }
}

export async function showReminderNotification(reminder: Reminder, fallback: (text: string) => void): Promise<void> {
  const title = reminder.title || "DeepSeek 提醒";
  const body = reminder.content || "";
  const tag = reminder.id || "deepseek-reminder";
  if (!(await ensureNotificationPermission())) {
    fallback(`${title}：${body}`);
    return;
  }
  const registration = await navigator.serviceWorker?.getRegistration("/ui/").catch(() => undefined);
  if (registration?.active) {
    registration.active.postMessage({ type: "show_reminder", title, body, tag });
    return;
  }
  try {
    new Notification(title, { body, tag });
  } catch {
    fallback(`${title}：${body}`);
  }
}

export function useReminderPolling(onNotify: (text: string) => void): void {
  const notifyRef = useRef(onNotify);
  notifyRef.current = onNotify;

  useEffect(() => {
    let disposed = false;
    async function poll() {
      try {
        const due = await fetchDueReminders();
        if (disposed) return;
        for (const reminder of due) {
          await showReminderNotification(reminder, notifyRef.current);
        }
      } catch {
        // Polling failures are silent by design (offline / server down).
      }
    }
    void poll();
    const timer = window.setInterval(() => void poll(), REMINDER_POLL_INTERVAL_MS);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, []);
}
