import { createContext, useContext, type PropsWithChildren } from "react";

import { useActivityController, type ActivityController } from "../features/activity/useActivityController";

const ActivityContext = createContext<ActivityController | null>(null);

export function ActivityProvider({ children }: PropsWithChildren) {
  const value = useActivityController();
  return <ActivityContext.Provider value={value}>{children}</ActivityContext.Provider>;
}

export function useActivity(): ActivityController {
  const value = useContext(ActivityContext);
  if (!value) throw new Error("useActivity must be used inside ActivityProvider");
  return value;
}
