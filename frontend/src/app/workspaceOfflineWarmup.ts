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

interface WarmupController {
  postMessage(message: unknown): void;
}

export interface WorkerBuildIdentity {
  type: "build_identity";
  buildId: string;
  assetSetDigest: string;
  cacheReady: boolean;
}

export function shouldWarmWorkspaceAssets(navigatorValue: object): boolean {
  const connection = (navigatorValue as NavigatorWithConnection).connection;
  if (connection?.saveData) return false;
  return !["slow-2g", "2g"].includes(connection?.effectiveType?.toLowerCase() ?? "");
}

export function warmWorkspaceAssets(
  controller: WarmupController,
  identity: WorkerBuildIdentity,
): void {
  controller.postMessage({
    type: "cache_workspace_primary",
    buildId: identity.buildId,
    assetSetDigest: identity.assetSetDigest,
  });
}

export function scheduleWorkspaceOfflineWarmup(
  controller: WarmupController,
  identity: WorkerBuildIdentity,
  navigatorValue: object,
  windowValue: WarmupWindow,
  controllerIsCurrent: () => boolean = () => true,
): boolean {
  if (!shouldWarmWorkspaceAssets(navigatorValue)) return false;
  const warm = () => {
    if (controllerIsCurrent()) warmWorkspaceAssets(controller, identity);
  };
  if (windowValue.requestIdleCallback) {
    windowValue.requestIdleCallback(warm, { timeout: 5000 });
  } else {
    windowValue.setTimeout(warm, 5000);
  }
  return true;
}
