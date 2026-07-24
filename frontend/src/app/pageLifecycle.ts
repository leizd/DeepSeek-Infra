import {
  flushReloadPersistence,
  getReloadBlockerSnapshot,
} from "./reloadBlockers";

export interface PageLifecycleEnvironment {
  windowValue: Pick<Window, "addEventListener" | "removeEventListener">;
  documentValue: Pick<Document, "visibilityState" | "addEventListener" | "removeEventListener">;
}

/**
 * 把刷新前持久化扩展到真实页面生命周期：手动刷新、关闭标签页和
 * 移动端后台回收都不保证 React 会正常卸载组件，所以这里直接监听
 * 页面事件并同步调用已注册的 Flusher。
 *
 * 故意不注册 `unload`，也不在这里清理 Store、BroadcastChannel 或
 * Service Worker，避免破坏 BFCache（`pagehide.persisted === true`
 * 的页面可能被原样恢复）。
 */
export function startPageLifecyclePersistence(environment: PageLifecycleEnvironment): () => void {
  const onVisibilityChange: EventListener = () => {
    if (environment.documentValue.visibilityState !== "hidden") return;
    flushReloadPersistence();
  };
  const onPageHide: EventListener = () => {
    flushReloadPersistence();
  };
  const onBeforeUnload: EventListener = (event) => {
    flushReloadPersistence();
    if (!getReloadBlockerSnapshot().length) return;
    event.preventDefault();
    (event as BeforeUnloadEvent).returnValue = "";
  };

  environment.documentValue.addEventListener("visibilitychange", onVisibilityChange);
  environment.windowValue.addEventListener("pagehide", onPageHide);
  environment.windowValue.addEventListener("beforeunload", onBeforeUnload);

  return () => {
    environment.documentValue.removeEventListener("visibilitychange", onVisibilityChange);
    environment.windowValue.removeEventListener("pagehide", onPageHide);
    environment.windowValue.removeEventListener("beforeunload", onBeforeUnload);
  };
}
