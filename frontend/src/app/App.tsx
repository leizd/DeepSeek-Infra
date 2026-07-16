import { appRoutes } from "./routes";

const boundaries = [
  ["api/", "HTTP 与 NDJSON 流通信"],
  ["domain/", "纯类型与状态转换"],
  ["features/", "后续迁移的交互流程"],
  ["shared/", "无业务耦合的界面基础"],
] as const;

const foundation = [
  "严格的聊天消息与流事件联合类型",
  "纯函数流式状态 reducer",
  "会话持久化对象契约",
  "统一 HTTP 错误与鉴权边界",
  "可取消的 NDJSON async iterator",
] as const;

export function App() {
  return (
    <main className="migration-shell">
      <section className="hero" aria-labelledby="migration-title">
        <div className="eyebrow">DEEPSEEK INFRA · 4.0.2</div>
        <h1 id="migration-title">React 迁移基础已经独立运行</h1>
        <p>
          这是与旧前端隔离的 <code>/ui/</code> 预览入口。当前版本先固定协议、类型和流式状态机，
          不让 React 与 <code>chat.js</code> 同时控制一棵 DOM。
        </p>
        <div className="hero-actions">
          <a className="primary-action" href={appRoutes.legacy}>
            返回稳定版工作区
          </a>
          <span className="status-pill" role="status">
            默认入口仍为 Legacy
          </span>
        </div>
      </section>

      <section className="boundary-grid" aria-label="前端架构边界">
        {boundaries.map(([name, description]) => (
          <article className="boundary-card" key={name}>
            <code>{name}</code>
            <p>{description}</p>
          </article>
        ))}
      </section>

      <section className="foundation" aria-labelledby="foundation-title">
        <div>
          <div className="eyebrow">MIGRATION FOUNDATION</div>
          <h2 id="foundation-title">先拆协议，再迁移界面</h2>
        </div>
        <ol>
          {foundation.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ol>
      </section>
    </main>
  );
}
