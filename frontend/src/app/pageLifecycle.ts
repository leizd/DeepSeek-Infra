import {
  flushReloadPersistence,
  getReloadBlockerSnapshot,
  type PersistenceFlushReport,
} from "./reloadBlockers";
import { recordFlushReport } from "./persistenceHealth";

export interface PageLifecycleEnvironment {
  windowValue: Pick<Window, "addEventListener" | "removeEventListener">;
  documentValue: Pick<Document, "visibilityState" | "addEventListener" | "removeEventListener">;
}

/**
 * 把刷新前持久化扩展到真实页面生命周期：手动刷新、关闭标签页和
 * 移动端后台回收都不保证 React 会正常卸载组件，所以这里直接监听
 * 页面事件并同步调用已注册的 Flusher。
 *
 * 每次 flush 的报告都会写入 persistenceHealth：pagehide 时即使页面
 * 随即被销毁，最后成功 revision 与失败状态也已记录在案。beforeunload
 * 在 flush 失败时同样阻止离开——刚刚写丢本地状态时不允许静默退出。
 *
 * 故意不注册 `unload`，也不在这里清理 Store、BroadcastChannel 或
 * Service Worker，避免破坏 BFCache（`pagehide.persisted === true`
 * 的页面可能被原样恢复）。
 */
export function startPageLifecyclePersistence(environment: PageLifecycleEnvironment): () => void {
  const flushAndRecord = (): PersistenceFlushReport => {
    const report = flushReloadPersistence();
    recordFlushReport(report);
    return report;
  };
  const onVisibilityChange: EventListener = () => {
    if (environment.documentValue.visibilityState !== "hidden") return;
    flushAndRecord();
  };
  const onPageHide: EventListener = () => {
    flushAndRecord();
  };
  const onBeforeUnload: EventListener = (event) => {
    const report = flushAndRecord();
    if (report.ok && !getReloadBlockerSnapshot().length) return;
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
