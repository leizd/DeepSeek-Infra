"""Tavily search triggering, multi-round search aggregation, caching, and prompt context formatting."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from deepseek_infra.core.config import (
    SEARCH_CACHE_DIR,
    SEARCH_CACHE_MAX_AGE_SECONDS,
    SEARCH_CONTENT_CHARS,
    SEARCH_CONTEXT_RESULT_LIMIT,
    SEARCH_RAW_CONTENT_CHARS,
    SEARCH_RESULT_LIMIT,
    SEARCH_ROUND_LIMIT,
    SEARCH_TOTAL_RESULT_LIMIT,
    TAVILY_TIMEOUT_SECONDS,
    TAVILY_API_KEY,
    TAVILY_URL,
    TRUSTED_DOMAIN_HINTS,
)
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import format_upstream_error, query_tokens

logger = logging.getLogger("deepseek_infra.search")


def normalize_search_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url.strip().lower()

    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def should_search_for_query(query: str, payload: dict[str, Any]) -> bool:
    mode = str(payload.get("searchMode") or "").strip().lower()
    if mode in {"off", "false", "0"}:
        return False
    if mode in {"on", "force", "true", "1"}:
        return True

    text = str(query or "").strip()
    if not text:
        return False

    if re.search(
        r"(今天|今日|昨天|明天|本周|本月|最新|现在|近期|刚刚|实时|新闻|价格|票房|天气|汇率|股价|today|latest|current|recent|now|news|price|weather|schedule|release|version)",
        text,
        flags=re.IGNORECASE,
    ):
        return True

    if re.search(
        r"(查一下|搜索|联网|网上|来源|引用|网址|官网|文档|政策|法规|标准|榜单|排名|评测|search|browse|look up|source|citation|official docs)",
        text,
        flags=re.IGNORECASE,
    ):
        return True

    return re.search(r"https?://|www\.|[a-z0-9-]+\.(com|org|net|io|dev|cn|edu|gov)", text, flags=re.IGNORECASE) is not None


def search_intent(query: str) -> str:
    text = str(query or "").lower()
    if re.search(r"(今天|今日|最新|新闻|现在|近期|刚刚|实时|today|latest|news|current|recent|now)", text):
        return "fresh"
    if re.search(r"(价格|多少钱|报价|购买|推荐|评测|排行|price|buy|review|best|deal)", text):
        return "shopping"
    if re.search(r"(文档|api|sdk|报错|错误|版本|安装|配置|docs|documentation|error|exception|version|install)", text):
        return "technical"
    if re.search(r"(政策|法规|法律|标准|条例|policy|law|regulation|standard|official|官网|官方)", text):
        return "official"
    if re.search(r"(对比|区别|比较|优缺点|compare|difference|vs)", text):
        return "compare"
    return "general"


def search_reason_for_query(query: str) -> str:
    if re.search(r"(最新|现在|近期|新闻|实时|today|latest|current|news)", query, flags=re.IGNORECASE):
        return "检测到时效性问题"
    if re.search(r"(官网|文档|来源|引用|official|docs|source)", query, flags=re.IGNORECASE):
        return "需要外部来源验证"
    if re.search(r"(价格|报价|评测|排名|price|review|ranking)", query, flags=re.IGNORECASE):
        return "需要查询当前市场信息"
    return "自动判断需要联网补充资料"


def search_multiple(
    query: str,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    *,
    tavily_api_key: str = "",
) -> dict[str, Any]:
    cached = load_search_cache(query)
    if cached:
        cached = {**cached, "cached": True}
        if progress_callback:
            progress_callback(cached)
        return cached

    queries = search_queries_for(query)
    rounds_by_index: dict[int, dict[str, Any]] = {}
    max_workers = max(1, min(len(queries), SEARCH_ROUND_LIMIT))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for round_index, search_query in enumerate(queries, start=1):
            rounds_by_index[round_index] = search_round_status(search_query, round_index, "searching")
            if progress_callback:
                progress_callback(aggregate_search_rounds(query, rounds_in_order(rounds_by_index), status="searching"))
            futures[pool.submit(search_tavily_with_retry, search_query, tavily_api_key=tavily_api_key)] = (round_index, search_query)

        for future in as_completed(futures):
            round_index, search_query = futures[future]
            try:
                round_data = future.result()
                round_data["round"] = round_index
                round_data["status"] = str(round_data.get("status") or "done")
            except AppError as exc:
                round_data = search_round_status(search_query, round_index, "error", str(exc))
            except Exception as exc:  # pragma: no cover - defensive boundary for worker failures
                logger.exception("search_round_error", extra={"round": round_index, "query": search_query})
                round_data = search_round_status(search_query, round_index, "error", str(exc))
            rounds_by_index[round_index] = round_data
            if progress_callback:
                progress_callback(aggregate_search_rounds(query, rounds_in_order(rounds_by_index)))

    result = aggregate_search_rounds(query, rounds_in_order(rounds_by_index))
    if result.get("results"):
        save_search_cache(query, result)
    return result


def search_single_round(
    query: str,
    *,
    intent: str = "general",
    round_index: int,
    citation_offset: int = 0,
    tavily_api_key: str = "",
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    use_cache: bool = False,
) -> dict[str, Any]:
    cleaned = normalize_search_query_text(query)
    if not cleaned:
        return {"ok": False, "error": "Empty query", "query": "", "round": round_index, "intent": intent, "results": []}

    if use_cache:
        cached = load_search_cache(cleaned)
        if cached and cached.get("results"):
            round_data = search_round_from_cache(cleaned, cached, round_index)
            if progress_callback:
                progress_callback(round_data)
            return compact_search_tool_result(round_data, intent=intent, citation_offset=citation_offset)

    if progress_callback:
        progress_callback(search_round_status(cleaned, round_index, "searching"))

    try:
        round_data = search_tavily_with_retry(cleaned, tavily_api_key=tavily_api_key)
        round_data["round"] = round_index
        round_data["status"] = str(round_data.get("status") or "done")
    except AppError as exc:
        round_data = search_round_status(cleaned, round_index, "error", str(exc))
    except Exception as exc:  # pragma: no cover - defensive boundary for worker failures
        logger.exception("web_search_tool_error", extra={"round": round_index, "query": cleaned})
        round_data = search_round_status(cleaned, round_index, "error", str(exc))

    if use_cache and round_data.get("results"):
        save_search_cache(cleaned, aggregate_search_rounds(cleaned, [round_data]))

    if progress_callback:
        progress_callback(round_data)
    return compact_search_tool_result(round_data, intent=intent, citation_offset=citation_offset)


def search_round_from_cache(query: str, cached: dict[str, Any], round_index: int) -> dict[str, Any]:
    return {
        "query": str(cached.get("query") or query),
        "round": round_index,
        "status": str(cached.get("status") or "done"),
        "answer": str(cached.get("answer") or ""),
        "results": [dict(item) for item in cached.get("results") or [] if isinstance(item, dict)],
        "response_time": cached.get("response_time"),
        "cached": True,
    }


def compact_search_tool_result(round_data: dict[str, Any], *, intent: str = "general", citation_offset: int = 0) -> dict[str, Any]:
    results_for_model = []
    for index, item in enumerate((round_data.get("results") or [])[:SEARCH_RESULT_LIMIT], start=1):
        if not isinstance(item, dict):
            continue
        snippet = str(item.get("content") or item.get("raw_content") or "")[:600]
        citation_id = str(item.get("citation_id") or f"W{citation_offset + index}")
        results_for_model.append(
            {
                "cite": f"[^{citation_id}]",
                "citation_id": citation_id,
                "title": str(item.get("title") or "")[:180],
                "url": str(item.get("url") or ""),
                "snippet": snippet,
            }
        )
    return {
        "query": str(round_data.get("query") or ""),
        "round": int(round_data.get("round") or 0),
        "intent": str(intent or "general"),
        "answer": str(round_data.get("answer") or "")[:600],
        "results": results_for_model,
        "status": str(round_data.get("status") or "done"),
        "error": round_data.get("error"),
        "retried": bool(round_data.get("retried")),
        "retryQuery": round_data.get("retryQuery") or "",
        "retryError": round_data.get("retryError") or "",
        "cached": bool(round_data.get("cached")),
    }


def normalize_search_query_text(query: str) -> str:
    return re.sub(r"\s+", " ", str(query or "")).strip()[:500]


def simplified_retry_query(query: str) -> str:
    cleaned = normalize_search_query_text(query)
    simplified = re.sub(r"[^\w\s\u4e00-\u9fff]+", " ", cleaned, flags=re.UNICODE)
    parts = [part for part in simplified.split() if part]
    if len(parts) > 1:
        simplified = " ".join(parts[:8])
    else:
        simplified = simplified.strip()[:120]
    return normalize_search_query_text(simplified)


def should_retry_tavily_error(exc: AppError) -> bool:
    if exc.code == ErrorCode.MISSING_API_KEY or exc.code == ErrorCode.INVALID_PAYLOAD:
        return False
    return exc.code == ErrorCode.UPSTREAM_TIMEOUT or exc.status in {408, 429, 500, 502, 503, 504}


def search_tavily_with_retry(query: str, *, tavily_api_key: str = "") -> dict[str, Any]:
    try:
        return search_tavily(query, tavily_api_key=tavily_api_key)
    except AppError as exc:
        retry_query = simplified_retry_query(query)
        if not retry_query or retry_query.lower() == normalize_search_query_text(query).lower() or not should_retry_tavily_error(exc):
            raise
        try:
            retried = search_tavily(retry_query, tavily_api_key=tavily_api_key)
        except AppError as retry_exc:
            error = search_round_status(query, 0, "error", f"{exc}; retry failed: {retry_exc}")
            error["retried"] = True
            error["retryQuery"] = retry_query
            error["retryError"] = str(retry_exc)
            return error
        retried["query"] = normalize_search_query_text(query)
        retried["retried"] = True
        retried["retryQuery"] = retry_query
        retried["originalError"] = str(exc)
        return retried


def rounds_in_order(rounds_by_index: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [rounds_by_index[index] for index in sorted(rounds_by_index)]


def search_queries_for(query: str) -> list[str]:
    normalized = normalize_search_query_text(query)
    if not normalized:
        return []

    variants_by_intent = {
        "fresh": ["最新进展", "官方回应"],
        "shopping": ["评测 对比", "价格 购买"],
        "technical": ["官方文档", "常见问题 解决方案"],
        "official": ["官方来源", "政策 解读"],
        "compare": ["对比 分析", "评论 观点"],
        "general": ["背景 信息", "评论 观点"],
    }
    limit = max(1, int(SEARCH_ROUND_LIMIT or 1))
    queries: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        cleaned = normalize_search_query_text(candidate)
        key = cleaned.lower()
        if not cleaned or key in seen:
            return
        seen.add(key)
        queries.append(cleaned)

    add(normalized)
    for suffix in variants_by_intent.get(search_intent(normalized), variants_by_intent["general"]):
        if len(queries) >= limit:
            break
        add(f"{normalized} {suffix}")
    return queries[:limit]


def tavily_options_for_query(query: str) -> dict[str, Any]:
    intent = search_intent(query)
    options: dict[str, Any] = {
        "topic": "general",
        "search_depth": "basic",
        "max_results": SEARCH_RESULT_LIMIT,
        "include_answer": "basic",
        "include_raw_content": False,
        "include_images": False,
        "include_favicon": True,
    }
    if intent in {"technical", "official", "compare", "fresh"}:
        options["search_depth"] = "advanced"
    if intent in {"technical", "official"}:
        options["include_raw_content"] = True
    if intent == "fresh":
        options["include_answer"] = "advanced"
    return options


def search_domain_filters(query: str) -> dict[str, Any]:
    text = query.lower()
    if re.search(r"(政策|法规|签证|税|法律|government|law|regulation)", text):
        return {"include_domains": ["gov.cn", "mfa.gov.cn", "ica.gov.sg", "mom.gov.sg", "gov.sg"]}
    if re.search(r"(官方文档|官网文档|official docs|official documentation)", text):
        return {
            "include_domains": [
                "docs.python.org",
                "developer.mozilla.org",
                "react.dev",
                "nodejs.org",
                "docs.tavily.com",
                "api-docs.deepseek.com",
                "github.com",
            ]
        }
    return {}


def search_tavily(query: str, *, tavily_api_key: str = "") -> dict[str, Any]:
    api_key = str(tavily_api_key or TAVILY_API_KEY).strip()
    if not api_key:
        raise AppError(
            "Tavily search is not configured. Set TAVILY_API_KEY or provide tavilyApiKey in the request.",
            code=ErrorCode.MISSING_API_KEY,
            status=503,
        )

    request_body = {
        "query": query[:500],
        **tavily_options_for_query(query),
        **search_domain_filters(query),
    }
    request = urllib.request.Request(
        TAVILY_URL,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TAVILY_TIMEOUT_SECONDS) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AppError(f"Tavily search failed: {format_upstream_error(detail)}", code=ErrorCode.UPSTREAM_FAILURE, status=min(exc.code, 502)) from exc
    except urllib.error.URLError as exc:
        code = ErrorCode.UPSTREAM_TIMEOUT if "timed out" in str(exc.reason).lower() else ErrorCode.UPSTREAM_FAILURE
        raise AppError(f"Cannot reach Tavily API: {exc.reason}", code=code, status=502) from exc

    return normalize_search_response(query, response_json)


def normalize_search_response(query: str, data: dict[str, Any]) -> dict[str, Any]:
    results = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled").strip()
        url = str(item.get("url") or "").strip()
        content = str(item.get("content") or "").strip()
        raw_content = str(item.get("raw_content") or "").strip()
        if not url:
            continue
        results.append(
            {
                "title": title[:180],
                "url": url,
                "content": content[:SEARCH_CONTENT_CHARS],
                "raw_content": raw_content[:SEARCH_RAW_CONTENT_CHARS],
                "score": item.get("score"),
                "favicon": item.get("favicon"),
            }
        )

    return {
        "query": str(data.get("query") or query),
        "answer": str(data.get("answer") or "").strip(),
        "results": results[:SEARCH_RESULT_LIMIT],
        "response_time": data.get("response_time"),
        "request_id": data.get("request_id"),
    }


def aggregate_search_rounds(query: str, rounds: list[dict[str, Any]], status: str | None = None) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    answers: list[str] = []
    normalized_rounds: list[dict[str, Any]] = []
    citation_counter = 0

    for index, round_data in enumerate(rounds, start=1):
        round_results = []
        for result in round_data.get("results") or []:
            if not isinstance(result, dict):
                continue
            url = str(result.get("url") or "").strip()
            if not url:
                continue
            citation_counter += 1
            citation_id = str(result.get("citation_id") or f"W{citation_counter}")
            client_result = {
                "cite": str(result.get("cite") or f"[^{citation_id}]"),
                "citation_id": citation_id,
                "title": str(result.get("title") or "").strip(),
                "url": url,
                "content": str(result.get("content") or "").strip(),
                "raw_content": str(result.get("raw_content") or "").strip(),
                "score": result.get("score"),
                "favicon": result.get("favicon"),
            }
            round_results.append(client_result)
            key = normalize_search_url(url)
            if key and key not in seen_urls:
                seen_urls.add(key)
                results.append({**client_result, "round": round_data.get("round") or index})

        answer = str(round_data.get("answer") or "").strip()
        if answer:
            answers.append(answer)

        normalized_round = {
            "round": int(round_data.get("round") or index),
            "status": str(round_data.get("status") or "done"),
            "query": str(round_data.get("query") or ""),
            "answer": answer,
            "results": round_results,
            "response_time": round_data.get("response_time"),
        }
        if round_data.get("error"):
            normalized_round["error"] = str(round_data.get("error"))
        if round_data.get("retried"):
            normalized_round["retried"] = True
            normalized_round["retryQuery"] = str(round_data.get("retryQuery") or "")
        if round_data.get("retryError"):
            normalized_round["retryError"] = str(round_data.get("retryError"))
        normalized_rounds.append(normalized_round)

    if status is None:
        if any(item.get("status") == "searching" for item in normalized_rounds):
            status = "searching"
        elif normalized_rounds and all(item.get("status") == "error" for item in normalized_rounds):
            status = "error"
        else:
            status = "done"

    return {
        "status": status,
        "query": query,
        "reason": search_reason_for_query(query),
        "answer": "\n\n".join(dict.fromkeys(answers)),
        "results": rerank_search_results(results, query, limit=SEARCH_TOTAL_RESULT_LIMIT),
        "rounds": normalized_rounds,
        "response_time": None,
        "cached": False,
    }


def domain_from_url(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def search_result_score(result: dict[str, Any], query: str) -> float:
    title = str(result.get("title") or "")
    content = str(result.get("content") or "")
    url = str(result.get("url") or "")
    domain = domain_from_url(url)
    try:
        score = float(result.get("score") or 0) * 20
    except (TypeError, ValueError):
        score = 0

    combined = f"{title}\n{content}".lower()
    title_lower = title.lower()
    for token in query_tokens(query):
        if token in title_lower:
            score += 8
        if token in combined:
            score += 3

    if any(hint in domain for hint in TRUSTED_DOMAIN_HINTS):
        score += 10
    if re.search(r"(official|docs|documentation|developer|官方|文档)", title + " " + url, flags=re.IGNORECASE):
        score += 6
    if not content.strip():
        score -= 8
    return score


def rerank_search_results(results: list[dict[str, Any]], query: str, *, limit: int) -> list[dict[str, Any]]:
    ranked = sorted(results, key=lambda item: search_result_score(item, query), reverse=True)
    selected = []
    domain_counts: dict[str, int] = {}
    for result in ranked:
        domain = domain_from_url(str(result.get("url") or ""))
        count = domain_counts.get(domain, 0)
        if domain and count >= 2:
            continue
        selected.append(result)
        if domain:
            domain_counts[domain] = count + 1
        if len(selected) >= limit:
            break
    return selected


def format_search_context(search_data: dict[str, Any]) -> str:
    lines = [
        "When citing these web sources, use the exact [^Wn] markers shown below.",
        "你可以使用以下联网搜索结果回答用户问题。",
        f"搜索问题: {search_data.get('query', '')}",
        "要求:",
        "1. 只在搜索结果支持时给出时效性结论。",
        "2. 引用来源时在论断后追加对应的 [^Wn] 标记，不要写 [来源]/[Source] 或 Markdown 链接。",
        "3. 具体日期、价格、版本号、政策、新闻结论后必须给出来源链接。",
        "4. 不要引用未出现在搜索来源里的网页。",
        "5. 如果结果不足或互相矛盾，请明确说明不确定。",
        "6. 优先使用官方、原始、权威来源。",
        "7. 如已有结果足以回答，不要继续搜索；只有缺少关键事实时最多再补充 1 次 web_search。",
    ]

    answer = str(search_data.get("answer") or "").strip()
    if answer:
        lines.extend(["", f"Tavily 摘要: {answer}"])

    results = search_data.get("results") or []
    if results:
        lines.append("")
        lines.append("搜索来源:")

    for index, result in enumerate(results[:SEARCH_CONTEXT_RESULT_LIMIT], start=1):
        title = str(result.get("title") or f"来源 {index}")
        citation_id = str(result.get("citation_id") or f"W{index}")
        url = str(result.get("url") or "")
        content = str(result.get("raw_content") or result.get("content") or "").strip()
        lines.append("")
        lines.append(f"[^{citation_id}] {title}")
        lines.append(f"URL: {url}")
        if content:
            lines.append(f"内容摘录: {content[:SEARCH_RAW_CONTENT_CHARS]}")
    return "\n".join(lines)


def format_search_failure_context(search_data: dict[str, Any]) -> str:
    rounds = search_data.get("rounds") or []
    errors = [
        str(round_item.get("error") or "")
        for round_item in rounds
        if isinstance(round_item, dict) and round_item.get("error")
    ]
    return "\n".join(
        [
            "本轮尝试联网搜索，但搜索没有得到可用来源。",
            "回答时不要声称已经查到最新资料。",
            "如果问题依赖实时信息，请明确说明无法确认最新状态。",
            "",
            "搜索错误:",
            "\n".join(f"- {error}" for error in errors[:3]) or "- 未知错误",
        ]
    )


def search_cache_key(query: str) -> str:
    normalized = re.sub(r"\s+", " ", query.lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()[:32]


def load_search_cache(query: str) -> dict[str, Any] | None:
    path = SEARCH_CACHE_DIR / f"{search_cache_key(query)}.json"
    if not path.exists():
        return None
    try:
        age = datetime.now().timestamp() - path.stat().st_mtime
        if age > SEARCH_CACHE_MAX_AGE_SECONDS:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def cleanup_search_cache() -> None:
    if not SEARCH_CACHE_DIR.exists():
        return

    now = datetime.now().timestamp()
    for path in SEARCH_CACHE_DIR.glob("*.json"):
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age <= SEARCH_CACHE_MAX_AGE_SECONDS:
            continue
        try:
            path.unlink()
        except OSError:
            pass


def save_search_cache(query: str, data: dict[str, Any]) -> None:
    SEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_search_cache()
    path = SEARCH_CACHE_DIR / f"{search_cache_key(query)}.json"
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)


def search_for_client(search_data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not search_data:
        return None
    return {
        "status": search_data.get("status") or "done",
        "query": search_data.get("query", ""),
        "reason": search_data.get("reason", ""),
        "answer": search_data.get("answer", ""),
        "cached": bool(search_data.get("cached")),
        "results": [
            {
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "content": result.get("content", ""),
                "favicon": result.get("favicon"),
                "round": result.get("round"),
                "cite": result.get("cite", ""),
                "citation_id": result.get("citation_id", ""),
            }
            for result in search_data.get("results") or []
        ],
        "rounds": [
            {
                "round": round_data.get("round"),
                "status": round_data.get("status") or "done",
                "query": round_data.get("query", ""),
                "answer": round_data.get("answer", ""),
                "error": round_data.get("error", ""),
                "retried": bool(round_data.get("retried")),
                "retryQuery": round_data.get("retryQuery", ""),
                "retryError": round_data.get("retryError", ""),
                "results": [
                    {
                        "title": result.get("title", ""),
                        "url": result.get("url", ""),
                        "content": result.get("content", ""),
                        "favicon": result.get("favicon"),
                        "cite": result.get("cite", ""),
                        "citation_id": result.get("citation_id", ""),
                    }
                    for result in round_data.get("results") or []
                ],
                "response_time": round_data.get("response_time"),
            }
            for round_data in search_data.get("rounds") or []
        ],
        "response_time": search_data.get("response_time"),
    }


def diagnostics_with_search(diagnostics: dict[str, Any], search_data: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(diagnostics)
    if search_data:
        rounds = search_data.get("rounds") or []
        results = search_data.get("results") or []
        result["searchRoundCount"] = len(rounds) if isinstance(rounds, list) else 0
        result["searchResultCount"] = len(results) if isinstance(results, list) else 0
    return result


def search_round_status(query: str, round_index: int, status: str, error: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {
        "round": round_index,
        "status": status,
        "query": query,
        "answer": "",
        "results": [],
        "response_time": None,
    }
    if error:
        result["error"] = error
    return result
