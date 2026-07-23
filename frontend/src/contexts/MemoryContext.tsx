import { createContext, useContext, type PropsWithChildren } from "react";

import { useMemoryWriteController, type MemoryWriteController } from "../features/memory/useMemoryWriteController";

const MemoryContext = createContext<MemoryWriteController | null>(null);

export function MemoryProvider({ children }: PropsWithChildren) {
  const value = useMemoryWriteController();
  return <MemoryContext.Provider value={value}>{children}</MemoryContext.Provider>;
}

export function useMemory(): MemoryWriteController {
  const value = useContext(MemoryContext);
  if (!value) throw new Error("useMemory must be used inside MemoryProvider");
  return value;
}
