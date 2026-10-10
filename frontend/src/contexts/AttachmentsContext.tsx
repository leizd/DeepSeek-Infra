import { createContext, useContext, type PropsWithChildren } from "react";

import { useAttachmentController, type AttachmentController } from "../features/attachments/useAttachmentController";

type AttachmentsContextValue = AttachmentController;

const AttachmentsContext = createContext<AttachmentsContextValue | null>(null);

export function AttachmentsProvider({ children }: PropsWithChildren) {
  const value = useAttachmentController();
  return <AttachmentsContext.Provider value={value}>{children}</AttachmentsContext.Provider>;
}

export function useAttachments(): AttachmentsContextValue {
  const value = useContext(AttachmentsContext);
  if (!value) throw new Error("useAttachments must be used inside AttachmentsProvider");
  return value;
}
