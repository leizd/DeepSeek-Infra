import { createContext, useContext, type PropsWithChildren } from "react";

import { useSkillController, type SkillController } from "../features/skills/useSkillController";

const SkillsContext = createContext<SkillController | null>(null);

export function SkillsProvider({ children }: PropsWithChildren) {
  const value = useSkillController();
  return <SkillsContext.Provider value={value}>{children}</SkillsContext.Provider>;
}

export function useSkills(): SkillController {
  const value = useContext(SkillsContext);
  if (!value) throw new Error("useSkills must be used inside SkillsProvider");
  return value;
}
