import type { ChatMessage, JsonRecord } from "../../domain/chat/types";
import { searchResults, searchRounds } from "../citations/citations";

export interface DiagnosticsRow {
  label: string;
  value: string;
}

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function numberOrZero(value: unknown): number {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : 0;
}

function formatCostUsd(value: unknown): string | undefined {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? `$${value.toFixed(4)}` : undefined;
}

function formatCacheRate(value: unknown): string | undefined {
  if (typeof value !== "number" || !Number.isFinite(value)) return undefined;
  return `${Math.round(value * (value <= 1 ? 100 : 1))}%`;
}

export function traceIdForMessage(message: ChatMessage): string {
  const diagnostics = isRecord(message.diagnostics) ? message.diagnostics : {};
  return typeof diagnostics.traceId === "string" ? diagnostics.traceId : "";
}

export function buildDiagnosticsRows(message: ChatMessage): DiagnosticsRow[] {
  const diagnostics = isRecord(message.diagnostics) ? message.diagnostics : {};
  const usage = isRecord(message.usage) ? message.usage : {};
  const agentCache = isRecord(diagnostics.agentCache) ? diagnostics.agentCache : null;
  const modelCascade = isRecord(diagnostics.modelCascade) ? diagnostics.modelCascade : null;
  const modelRouter = isRecord(diagnostics.modelRouter) ? diagnostics.modelRouter : null;
  const toolPolicy = isRecord(diagnostics.toolPolicy) ? diagnostics.toolPolicy : null;

  const candidates: Array<[string, unknown]> = [
    ["请求消息数", diagnostics.requestMessageCount],
    ["压缩摘要字符", diagnostics.contextSummaryChars],
    ["摘要代数", diagnostics.contextSummaryGeneration],
    ["已压缩消息数", diagnostics.contextSummaryMessageCount],
    ["本轮新增压缩消息数", diagnostics.contextCompressionDeltaCount],
    ["长期记忆", diagnostics.memoryEnabled === false ? "关闭" : `开启 · 命中 ${numberOrZero(diagnostics.memoryHitCount)} 条`],
    ["附件数量", diagnostics.attachmentCount],
    ["搜索轮数", diagnostics.searchRoundCount ?? (searchRounds(message.search).length || undefined)],
    ["搜索来源数", diagnostics.searchResultCount ?? (searchResults(message.search).length || undefined)],
    ["搜索缓存", isRecord(message.search) && message.search.cached === true ? "是" : undefined],
    ["Prompt tokens", usage.prompt_tokens ?? usage.promptTokens],
    ["Completion tokens", usage.completion_tokens ?? usage.completionTokens],
    ["Total tokens", usage.total_tokens ?? usage.totalTokens],
    ["本轮成本", formatCostUsd(diagnostics.costUsd)],
    ["Agent 估算成本", formatCostUsd(diagnostics.agentCostUsd)],
    ["路由模型", modelRouter?.model],
    [
      "级联推理",
      modelCascade
        ? modelCascade.escalated
          ? `已升级 · ${String(modelCascade.refineModel ?? "")}`
          : `草稿通过 · ${String(modelCascade.draftModel ?? "")}`
        : undefined,
    ],
    ["预算降级", diagnostics.budgetDowngraded === true ? "是（已降级到 flash）" : undefined],
    ["注入清洗", toolPolicy && numberOrZero(toolPolicy.sanitizedInjections) ? `${numberOrZero(toolPolicy.sanitizedInjections)} 处` : undefined],
    ["Cache hit tokens", diagnostics.cacheHitTokens ?? usage.prompt_cache_hit_tokens ?? usage.promptCacheHitTokens],
    ["Cache miss tokens", diagnostics.cacheMissTokens ?? usage.prompt_cache_miss_tokens ?? usage.promptCacheMissTokens],
    ["Cache hit rate", diagnostics.cacheHitRate === undefined ? undefined : `${String(diagnostics.cacheHitRate)}%`],
    ["Trace ID", diagnostics.traceId],
    ["Agent 缓存总 tokens", agentCache ? numberOrZero(agentCache.hitTokens) + numberOrZero(agentCache.missTokens) || undefined : undefined],
    ["Agent 缓存命中 tokens", agentCache ? agentCache.hitTokens : undefined],
    ["Agent 缓存命中率", agentCache ? formatCacheRate(agentCache.hitRate) : undefined],
  ];

  return candidates
    .filter((entry): entry is [string, string | number] => {
      const value = entry[1];
      return value !== undefined && value !== null && value !== "" && value !== 0;
    })
    .map(([label, value]) => ({ label, value: String(value) }));
}

export function hasDiagnostics(message: ChatMessage): boolean {
  return Boolean(
    (isRecord(message.diagnostics) && Object.keys(message.diagnostics).length)
    || (isRecord(message.usage) && Object.keys(message.usage).length)
    || message.search,
  );
}
