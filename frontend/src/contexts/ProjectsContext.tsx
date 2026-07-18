import { createContext, useContext, type PropsWithChildren } from "react";

import { useProjectController, type ProjectController } from "../features/projects/useProjectController";

const ProjectsContext = createContext<ProjectController | null>(null);

export function ProjectsProvider({ children }: PropsWithChildren) {
  const value = useProjectController();
  return <ProjectsContext.Provider value={value}>{children}</ProjectsContext.Provider>;
}

export function useProjects(): ProjectController {
  const value = useContext(ProjectsContext);
  if (!value) throw new Error("useProjects must be used inside ProjectsProvider");
  return value;
}
