import { createContext, useContext, type PropsWithChildren } from "react";

import { useChatController } from "../features/chat/useChatController";

type ChatContextValue = ReturnType<typeof useChatController>;

const ChatContext = createContext<ChatContextValue | null>(null);

export function ChatProvider({ children }: PropsWithChildren) {
  const value = useChatController();
  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>;
}

export function useChat(): ChatContextValue {
  const value = useContext(ChatContext);
  if (!value) throw new Error("useChat must be used inside ChatProvider");
  return value;
}
