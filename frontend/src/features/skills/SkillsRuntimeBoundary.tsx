import type { PropsWithChildren } from "react";

import { SkillsProvider } from "../../contexts/SkillsContext";

export default function SkillsRuntimeBoundary({ children }: PropsWithChildren) {
  return <SkillsProvider>{children}</SkillsProvider>;
}
