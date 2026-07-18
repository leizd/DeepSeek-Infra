import { createContext, useContext, type PropsWithChildren } from "react";

import { useMemoryController, type MemoryController } from "../features/memory/useMemoryController";

const MemoryContext = createContext<MemoryController | null>(null);

export function MemoryProvider({ children }: PropsWithChildren) {
  const value = useMemoryController();
  return <MemoryContext.Provider value={value}>{children}</MemoryContext.Provider>;
}

export function useMemory(): MemoryController {
  const value = useContext(MemoryContext);
  if (!value) throw new Error("useMemory must be used inside MemoryProvider");
  return value;
}
