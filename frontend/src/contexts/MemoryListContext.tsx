import { createContext, useContext, type PropsWithChildren } from "react";

import { useMemory } from "./MemoryContext";
import { useMemoryListController, type MemoryController } from "../features/memory/useMemoryController";

const MemoryListContext = createContext<MemoryController | null>(null);

export function MemoryListProvider({ children }: PropsWithChildren) {
  const value = useMemoryListController(useMemory());
  return <MemoryListContext.Provider value={value}>{children}</MemoryListContext.Provider>;
}

export function useMemoryList(): MemoryController {
  const value = useContext(MemoryListContext);
  if (!value) throw new Error("useMemoryList must be used inside MemoryListProvider");
  return value;
}
