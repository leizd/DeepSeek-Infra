import { useState } from "react";

import type { SearchSnapshot } from "../../domain/chat/types";
import { searchResults, searchRounds, type SearchRound } from "../citations/citations";

function roundStatusText(round: SearchRound): string {
  if (round.status === "searching") return "搜索中";
  if (round.status === "error") return "失败";
  return round.results.length ? `${round.results.length} 个网页` : "完成";
}

export function SearchBlock({ search, streaming }: { search: SearchSnapshot; streaming: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const results = searchResults(search);
  const rounds = searchRounds(search);
  const status = typeof search.status === "string" ? search.status : "done";
  const reason = typeof search.reason === "string" ? search.reason : "";
  const answer = typeof search.answer === "string" ? search.answer : "";
  const error = typeof search.error === "string" ? search.error : "";
  const cached = search.cached === true;
  const showRounds = rounds.length > 1 || rounds.some((round) => round.status === "searching" || round.status === "error");

  return (
    <section className="search-sources" aria-label="搜索来源">
      <div className="search-status-line">
        <span className="search-status-icon" aria-hidden="true">⌕</span>
        <strong>
          {status === "searching"
            ? `正在进行第 ${(rounds.find((round) => round.status === "searching")?.round ?? rounds.length) || 1} 轮搜索`
            : status === "error"
              ? "搜索失败，继续回答"
              : `搜索到 ${results.length} 个网页`}
        </strong>
        {rounds.length > 0 && <span className="search-round-count">已搜索 {rounds.length} 次</span>}
      </div>
      <div className="search-body">
        {reason && <p className="search-answer">{cached ? "已使用缓存 · " : ""}触发原因：{reason}</p>}
        {showRounds ? (
          <div className="search-rounds">
            {rounds.map((round) => (
              <div className={`search-round ${round.status || "done"}`} key={round.round}>
                <span className="search-round-label">第 {round.round} 轮</span>
                <span className="search-round-query">{round.query || "搜索网页"}</span>
                <span className="search-round-state">{roundStatusText(round)}</span>
                {round.error && <p className="search-error">{round.error}</p>}
              </div>
            ))}
          </div>
        ) : (
          typeof search.query === "string" && search.query && (streaming || status !== "done") && (
            <p className="search-query">搜索：{search.query}</p>
          )
        )}
        {error && <p className="search-error">{error}</p>}
        {status === "searching" && !results.length && !answer && (
          <p className="search-answer">正在获取网页来源，拿到结果后会继续整理回答。</p>
        )}
        {results.length > 0 && (
          <div className="search-browse-line">
            <span className="search-browse-prefix">浏览 {results.length} 个页面</span>
            <span className="search-inline-links">
              {results.slice(0, 4).map((result) => (
                <a key={result.url || result.title} href={result.url || "#"} target="_blank" rel="noopener noreferrer">
                  {result.title || result.url || "网页结果"}
                </a>
              ))}
            </span>
            {results.length > 4 && (
              <button className="search-view-all" type="button" onClick={() => setExpanded((value) => !value)}>
                {expanded ? "收起" : "查看全部"}
              </button>
            )}
          </div>
        )}
        {expanded && (
          <ul className="search-full-list">
            {results.map((result, index) => (
              <li key={`${result.url}-${index}`}>
                <a href={result.url || "#"} target="_blank" rel="noopener noreferrer">{result.title || result.url}</a>
                {result.snippet && <p>{result.snippet}</p>}
              </li>
            ))}
          </ul>
        )}
        {answer && <p className="search-answer">{answer}</p>}
      </div>
    </section>
  );
}
