"""AI Runtime Evaluation Harness — pure regression-metric library.

A "high-end AI infra" project has to be not just *runnable* but *measurable*. This
module is the scoring core behind ``evals/`` (golden datasets + CLI runners): a set
of pure, individually unit-testable functions that turn predictions + golden labels
into the metric families an AI runtime is judged on::

    Golden Questions      Prompt Regression Test    RAG Recall@K
    Citation Accuracy     Tool Call Accuracy        Agent Success Rate
    Latency Benchmark     Cost Benchmark

Design constraints (mirroring the rest of ``infra``):

* **Pure.** No I/O, no globals, no network. Every metric is a function of its inputs,
  so the same scorer runs in CI on recorded predictions and live in a runner. The one
  external call is :func:`budget_manager.cost_from_usage` for USD cost (itself pure).
* **Composable.** Live retrieval (RAG Recall@K) is delegated to
  ``local_rag.evaluate_recall`` in the runner; this module never imports the sqlite
  RAG layer, so it stays light and import-cheap.
* **Self-describing reports.** :class:`EvalReport` renders both a machine-readable
  dict (for ``evals/reports/*.json``) and the human report text
  (``RAG Recall@5: 0.86`` …).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from deepseek_infra.infra.gateway.budget_manager import cost_from_usage

# --- Dataset loading ------------------------------------------------------------

def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Load a ``.jsonl`` golden / predictions file; skips blank and ``#`` comment lines."""
    rows: list[dict[str, Any]] = []
    text = Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL line in {path}: {exc}") from exc
        if isinstance(data, dict):
            rows.append(data)
    return rows


def index_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("id") or ""): row for row in rows if str(row.get("id") or "")}


# --- Keyword / text helpers -----------------------------------------------------

def keyword_coverage(text: str, keywords: list[str]) -> tuple[float, list[str]]:
    """Fraction of ``keywords`` present (case-insensitive substring) in ``text``.

    Returns ``(coverage, missing)``. No keywords means trivially covered (1.0).
    """
    hay = str(text or "").lower()
    if not keywords:
        return 1.0, []
    present = [kw for kw in keywords if str(kw).strip() and str(kw).lower() in hay]
    missing = [kw for kw in keywords if kw not in present]
    return round(len(present) / len(keywords), 4), missing


# --- RAG Recall@K (pure) --------------------------------------------------------

def recall_hit(ranked_sources: list[str], relevant: set[str], k: int) -> tuple[bool, int]:
    """Whether any ``relevant`` source appears in the top-``k`` ranking; with its rank."""
    for rank, source in enumerate(ranked_sources[: max(1, k)], start=1):
        if source in relevant:
            return True, rank
    return False, 0


def recall_at_k(rankings: list[list[str]], relevant_sets: list[set[str]], k: int) -> dict[str, Any]:
    """Recall@K + MRR over parallel ``(ranking, relevant)`` lists."""
    hits = 0
    reciprocal = 0.0
    evaluated = 0
    for ranking, relevant in zip(rankings, relevant_sets):
        if not relevant:
            continue
        evaluated += 1
        hit, rank = recall_hit(ranking, relevant, k)
        if hit:
            hits += 1
            reciprocal += 1.0 / rank
    return {
        "k": max(1, k),
        "cases": evaluated,
        "recallAtK": round(hits / evaluated, 4) if evaluated else 0.0,
        "mrr": round(reciprocal / evaluated, 4) if evaluated else 0.0,
    }


# --- Citation accuracy ----------------------------------------------------------

def citation_case(top_source: str, expected_source: str, snippet: str, expected_keywords: list[str], *, min_coverage: float = 0.5) -> dict[str, Any]:
    """Score one citation: right source *and* the expected keywords grounded in it.

    A citation is accurate when the top retrieved source matches the expected source
    and the cited snippet covers at least ``min_coverage`` of the expected keywords
    (so a correct doc that doesn't actually contain the answer still fails).
    """
    source_ok = bool(expected_source) and top_source == expected_source
    coverage, missing = keyword_coverage(snippet, expected_keywords)
    accurate = source_ok and coverage >= min_coverage
    return {
        "expectedSource": expected_source,
        "topSource": top_source,
        "sourceMatch": source_ok,
        "keywordCoverage": coverage,
        "missingKeywords": missing,
        "accurate": accurate,
    }


def aggregate_ratio(values: list[bool]) -> float:
    return round(sum(1 for v in values if v) / len(values), 4) if values else 0.0


# --- Tool call accuracy ---------------------------------------------------------

def tool_call_score(expected: list[str], actual: list[str]) -> dict[str, Any]:
    """Precision / recall / F1 / exact-set-match for one task's tool calls (order-free)."""
    expected_set = {str(t) for t in expected if str(t)}
    actual_set = {str(t) for t in actual if str(t)}
    true_positive = len(expected_set & actual_set)
    precision = true_positive / len(actual_set) if actual_set else (1.0 if not expected_set else 0.0)
    recall = true_positive / len(expected_set) if expected_set else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "expected": sorted(expected_set),
        "actual": sorted(actual_set),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "exact": expected_set == actual_set,
        "unexpected": sorted(actual_set - expected_set),
        "missing": sorted(expected_set - actual_set),
    }


def tool_call_accuracy(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate exact-match accuracy and mean F1 over per-task ``tool_call_score`` rows."""
    if not cases:
        return {"accuracy": 0.0, "avgF1": 0.0, "cases": 0}
    return {
        "accuracy": aggregate_ratio([bool(c.get("exact")) for c in cases]),
        "avgF1": round(fmean(float(c.get("f1") or 0.0) for c in cases), 4),
        "cases": len(cases),
    }


# --- Agent success rate ---------------------------------------------------------

def agent_success(prediction: dict[str, Any], expected_keywords: list[str], *, min_coverage: float = 0.6) -> dict[str, Any]:
    """One agent task succeeds when it did not fail and its answer covers the goal keywords."""
    failed = bool(prediction.get("failed"))
    answer = str(prediction.get("final") or prediction.get("answer") or prediction.get("content") or "")
    coverage, missing = keyword_coverage(answer, expected_keywords)
    succeeded = (not failed) and coverage >= min_coverage
    return {"succeeded": succeeded, "failed": failed, "keywordCoverage": coverage, "missingKeywords": missing}


# --- Latency / cost benchmarks --------------------------------------------------

def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * frac


def latency_benchmark(latencies_ms: list[float]) -> dict[str, Any]:
    values = sorted(float(x) for x in latencies_ms if x is not None)
    if not values:
        return {"count": 0, "avgMs": 0.0, "p50Ms": 0.0, "p95Ms": 0.0, "maxMs": 0.0}
    return {
        "count": len(values),
        "avgMs": round(fmean(values), 2),
        "p50Ms": round(_percentile(values, 50), 2),
        "p95Ms": round(_percentile(values, 95), 2),
        "maxMs": round(values[-1], 2),
    }


def cost_benchmark(usages: list[tuple[dict[str, Any], str]]) -> dict[str, Any]:
    """Average token + USD cost over ``(usage, model)`` pairs (USD via budget_manager)."""
    if not usages:
        return {"count": 0, "avgPromptTokens": 0.0, "avgCompletionTokens": 0.0, "avgTokens": 0.0, "avgCostUsd": 0.0, "totalCostUsd": 0.0}
    prompt_tokens: list[int] = []
    completion_tokens: list[int] = []
    costs: list[float] = []
    for usage, model in usages:
        data = usage if isinstance(usage, dict) else {}
        prompt = int(data.get("prompt_tokens") or data.get("promptTokens") or data.get("inputTokens") or data.get("input_tokens") or 0)
        completion = int(
            data.get("completion_tokens") or data.get("completionTokens") or data.get("outputTokens") or data.get("output_tokens") or 0
        )
        prompt_tokens.append(prompt)
        completion_tokens.append(completion)
        recorded_cost = data.get("estimatedCostUsd") or data.get("estimated_cost_usd") or data.get("costUsd")
        if recorded_cost in (None, ""):
            costs.append(cost_from_usage(data, model))
        else:
            try:
                costs.append(round(float(str(recorded_cost)), 6))
            except (TypeError, ValueError):
                costs.append(cost_from_usage(data, model))
    avg_prompt = fmean(prompt_tokens)
    avg_completion = fmean(completion_tokens)
    return {
        "count": len(usages),
        "avgPromptTokens": round(avg_prompt, 1),
        "avgCompletionTokens": round(avg_completion, 1),
        "avgTokens": round(avg_prompt + avg_completion, 1),
        "avgCostUsd": round(fmean(costs), 6),
        "totalCostUsd": round(sum(costs), 6),
    }


# --- Prompt regression ----------------------------------------------------------

def keyword_regression(coverages: list[float], *, threshold: float = 0.6) -> dict[str, Any]:
    """Pass rate of keyword coverages against a regression threshold."""
    if not coverages:
        return {"passRate": 0.0, "passed": 0, "cases": 0, "threshold": threshold}
    passed = sum(1 for c in coverages if c >= threshold)
    return {
        "passRate": round(passed / len(coverages), 4),
        "passed": passed,
        "cases": len(coverages),
        "threshold": threshold,
    }


# --- Report ---------------------------------------------------------------------

# (key, label, kind) in display order; only keys present in `metrics` are rendered.
_METRIC_SPEC: tuple[tuple[str, str, str], ...] = (
    ("ragRecallAtK", "RAG Recall@{k}", "ratio"),
    ("ragMrr", "RAG MRR", "ratio"),
    ("citationAccuracy", "Citation Accuracy", "ratio"),
    ("keywordCoverage", "Keyword Coverage", "ratio"),
    ("toolCallAccuracy", "Tool Call Accuracy", "ratio"),
    ("toolCallF1", "Tool Call F1", "ratio"),
    ("toolPolicyPassRate", "Tool Policy Pass Rate", "ratio"),
    ("injectionDefensePassRate", "Prompt Injection Defense Pass", "ratio"),
    ("blockRate", "Injection Block Rate", "ratio"),
    ("falsePositiveRate", "False Positive Rate", "ratio"),
    ("bypassRate", "Bypass Rate", "ratio"),
    ("agentSuccessRate", "Agent Success Rate", "ratio"),
    ("promptRegressionPassRate", "Prompt Regression Pass", "ratio"),
    ("avgLatencyMs", "Avg Latency", "latency"),
    ("p95LatencyMs", "P95 Latency", "latency"),
    ("avgTokens", "Avg Token Cost", "tokens"),
    ("avgCostUsd", "Avg Cost", "usd"),
)


def format_latency(ms: float) -> str:
    value = float(ms)
    return f"{value / 1000:.2f}s" if value >= 1000 else f"{value:.1f}ms"


def format_tokens(tokens: float) -> str:
    value = float(tokens)
    return f"{value / 1000:.1f}k" if value >= 1000 else f"{value:.0f}"


def format_metric(value: float, kind: str) -> str:
    if kind == "latency":
        return format_latency(value)
    if kind == "tokens":
        return format_tokens(value)
    if kind == "usd":
        return f"${float(value):.6f}"
    return f"{float(value):.3f}"


@dataclass(frozen=True, slots=True)
class EvalReport:
    suite: str
    cases: int
    metrics: dict[str, float]
    benchmarks: dict[str, Any] = field(default_factory=dict)
    details: list[dict[str, Any]] = field(default_factory=list)
    k: int = 5
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "cases": self.cases,
            "k": self.k,
            "metrics": self.metrics,
            "benchmarks": self.benchmarks,
            "generatedAt": self.generated_at,
            "details": self.details,
        }

    def to_text(self) -> str:
        lines = [f"=== Eval Report · {self.suite} ===", f"Cases: {self.cases}"]
        for key, label, kind in _METRIC_SPEC:
            if key not in self.metrics:
                continue
            display = label.format(k=self.k)
            lines.append(f"{display}: {format_metric(self.metrics[key], kind)}")
        return "\n".join(lines)

    def write(self, report_dir: str | Path) -> Path:
        """Persist the report as ``<suite>-<timestamp>.json`` and return its path."""
        directory = Path(report_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = self.generated_at.replace(":", "").replace("-", "")
        path = directory / f"{self.suite}-{stamp}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def build_rag_report(case_rows: list[dict[str, Any]], *, k: int, suite: str = "rag") -> EvalReport:
    """Aggregate per-question RAG rows (each from :func:`citation_case` + recall) into a report.

    Each row: ``{id, hit, rank, topSource, expectedSource, sourceMatch, keywordCoverage,
    accurate, latencyMs}``.
    """
    recall = aggregate_ratio([bool(r.get("hit")) for r in case_rows])
    citation = aggregate_ratio([bool(r.get("accurate")) for r in case_rows])
    coverages = [float(r.get("keywordCoverage") or 0.0) for r in case_rows]
    avg_coverage = round(fmean(coverages), 4) if coverages else 0.0
    regression = keyword_regression(coverages)
    latency = latency_benchmark([float(r.get("latencyMs") or 0.0) for r in case_rows])
    ranks = [float(1.0 / r["rank"]) for r in case_rows if r.get("rank")]
    metrics = {
        "ragRecallAtK": recall,
        "ragMrr": round(fmean(ranks), 4) if ranks else 0.0,
        "citationAccuracy": citation,
        "keywordCoverage": avg_coverage,
        "promptRegressionPassRate": regression["passRate"],
        "avgLatencyMs": latency["avgMs"],
        "p95LatencyMs": latency["p95Ms"],
    }
    return EvalReport(
        suite=suite,
        cases=len(case_rows),
        metrics=metrics,
        benchmarks={"latency": latency, "regression": regression},
        details=case_rows,
        k=k,
    )


def build_agent_report(case_rows: list[dict[str, Any]], *, suite: str = "agent") -> EvalReport:
    """Aggregate per-task agent rows (tool-call score + success + usage) into a report.

    Each row: ``{id, tool:{...tool_call_score}, success:{...agent_success}, latencyMs,
    usage, model}``.
    """
    tool_rows = [r["tool"] for r in case_rows if isinstance(r.get("tool"), dict)]
    tool_summary = tool_call_accuracy(tool_rows)
    success_rate = aggregate_ratio([bool((r.get("success") or {}).get("succeeded")) for r in case_rows])
    coverages = [float((r.get("success") or {}).get("keywordCoverage") or 0.0) for r in case_rows]
    regression = keyword_regression(coverages)
    latency = latency_benchmark([float(r.get("latencyMs") or 0.0) for r in case_rows])
    cost = cost_benchmark([(r.get("usage") or {}, str(r.get("model") or "")) for r in case_rows])
    metrics = {
        "toolCallAccuracy": tool_summary["accuracy"],
        "toolCallF1": tool_summary["avgF1"],
        "agentSuccessRate": success_rate,
        "promptRegressionPassRate": regression["passRate"],
        "avgLatencyMs": latency["avgMs"],
        "p95LatencyMs": latency["p95Ms"],
        "avgTokens": cost["avgTokens"],
        "avgCostUsd": cost["avgCostUsd"],
    }
    return EvalReport(
        suite=suite,
        cases=len(case_rows),
        metrics=metrics,
        benchmarks={"latency": latency, "cost": cost, "toolCalls": tool_summary, "regression": regression},
        details=case_rows,
    )
