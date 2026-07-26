/**
 * 自动保存调度策略（纯函数，便于单测）：
 * - 普通编辑：300ms 尾随合并（同一窗口内的连续编辑只触发一次提交）；
 * - 流式进行中：锚定上次提交时刻的 1s 节流（尾随边界提交，尾部增量不丢）；
 * - 流式结算（done / error / 中断 ⇒ streaming→idle 跃迁）：立即提交，绕过待决定时器。
 */

export const EDIT_COALESCE_MS = 300;
export const STREAM_COMMIT_INTERVAL_MS = 1_000;

export interface CheckpointScheduleInput {
  /** 当前是否有流式请求在进行。 */
  streamingActive: boolean;
  /** 本次变化是否由流式结算（streaming→idle 跃迁）触发。 */
  justSettled: boolean;
  /** 上一次提交发生的时刻（Date.now() 口径；0 表示从未提交）。 */
  lastCommitAt: number;
  /** 当前时刻（Date.now() 口径）。 */
  now: number;
}

/**
 * 返回 "immediate"（立即提交，调用方取消任何待决定时器）或多少毫秒后提交。
 * 流式期间的延迟锚定 lastCommitAt 而非最近一次事件：高频事件重复调度只会
 * 收敛到同一尾随边界，绝不把提交无限推后；距上次提交已满一个间隔时不再
 * 额外等待（delay 0，随下一个宏任务提交）。
 */
export function decideCheckpointDelay(input: CheckpointScheduleInput): number | "immediate" {
  if (input.justSettled) return "immediate";
  if (!input.streamingActive) return EDIT_COALESCE_MS;
  const elapsed = Math.max(0, input.now - input.lastCommitAt);
  return elapsed >= STREAM_COMMIT_INTERVAL_MS ? 0 : STREAM_COMMIT_INTERVAL_MS - elapsed;
}
