interface NetworkInformationLike {
  effectiveType?: string;
  saveData?: boolean;
}

interface NavigatorWithConnection {
  connection?: NetworkInformationLike;
}

interface WarmupWindow {
  requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
  setTimeout(callback: () => void, timeout: number): number;
}

interface WarmupRegistration {
  active: { postMessage(message: unknown): void } | null;
}

export function shouldWarmWorkspaceAssets(navigatorValue: object): boolean {
  const connection = (navigatorValue as NavigatorWithConnection).connection;
  if (connection?.saveData) return false;
  return !["slow-2g", "2g"].includes(connection?.effectiveType?.toLowerCase() ?? "");
}

export function warmWorkspaceAssets(registration: WarmupRegistration): void {
  registration.active?.postMessage({ type: "cache_workspace_primary" });
}

export function scheduleWorkspaceOfflineWarmup(
  registration: WarmupRegistration,
  navigatorValue: object,
  windowValue: WarmupWindow,
): boolean {
  if (!shouldWarmWorkspaceAssets(navigatorValue)) return false;
  const warm = () => warmWorkspaceAssets(registration);
  if (windowValue.requestIdleCallback) {
    windowValue.requestIdleCallback(warm, { timeout: 5000 });
  } else {
    windowValue.setTimeout(warm, 5000);
  }
  return true;
}
