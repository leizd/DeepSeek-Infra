import { useCallback, useMemo, useState } from "react";

export interface ActivityController {
  openMessageId: string | null;
  openActivity(messageId: string): void;
  closeActivity(): void;
  autoOpen(messageId: string): void;
  isDismissed(messageId: string): boolean;
}

export function useActivityController(): ActivityController {
  const [openMessageId, setOpenMessageId] = useState<string | null>(null);
  const [dismissedIds, setDismissedIds] = useState<readonly string[]>([]);

  const openActivity = useCallback((messageId: string) => {
    setDismissedIds((current) => current.filter((id) => id !== messageId));
    setOpenMessageId(messageId);
  }, []);

  const closeActivity = useCallback(() => {
    setOpenMessageId((current) => {
      if (current) setDismissedIds((ids) => (ids.includes(current) ? ids : [...ids, current]));
      return null;
    });
  }, []);

  const autoOpen = useCallback(
    (messageId: string) => {
      if (dismissedIds.includes(messageId)) return;
      setOpenMessageId((current) => current ?? messageId);
    },
    [dismissedIds],
  );

  const isDismissed = useCallback((messageId: string) => dismissedIds.includes(messageId), [dismissedIds]);

  return useMemo(
    () => ({ openMessageId, openActivity, closeActivity, autoOpen, isDismissed }),
    [openMessageId, openActivity, closeActivity, autoOpen, isDismissed],
  );
}
