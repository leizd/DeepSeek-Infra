import { Fragment, type ReactNode } from "react";

export type MarkdownBlock =
  | { type: "heading"; level: number; text: string }
  | { type: "paragraph"; text: string }
  | { type: "quote"; text: string }
  | { type: "code"; language: string; text: string }
  | { type: "list"; ordered: boolean; items: string[] };

function specialLine(line: string): boolean {
  return /^```|^#{1,4}\s+|^>\s?|^\s*[-*]\s+|^\s*\d+\.\s+/.test(line);
}

export function parseMarkdownBlocks(markdown: string): MarkdownBlock[] {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const blocks: MarkdownBlock[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index] ?? "";
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const fence = line.match(/^```([^\s`]*)\s*$/);
    if (fence) {
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index] ?? "")) {
        code.push(lines[index] ?? "");
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push({ type: "code", language: fence[1] ?? "", text: code.join("\n") });
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1]?.length ?? 1, text: heading[2] ?? "" });
      index += 1;
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote: string[] = [];
      while (index < lines.length && /^>\s?/.test(lines[index] ?? "")) {
        quote.push((lines[index] ?? "").replace(/^>\s?/, ""));
        index += 1;
      }
      blocks.push({ type: "quote", text: quote.join("\n") });
      continue;
    }
    const list = line.match(/^\s*([-*]|\d+\.)\s+(.+)$/);
    if (list) {
      const ordered = /\d+\./.test(list[1] ?? "");
      const items: string[] = [];
      while (index < lines.length) {
        const item = (lines[index] ?? "").match(/^\s*([-*]|\d+\.)\s+(.+)$/);
        if (!item || /\d+\./.test(item[1] ?? "") !== ordered) break;
        items.push(item[2] ?? "");
        index += 1;
      }
      blocks.push({ type: "list", ordered, items });
      continue;
    }
    const paragraph = [line];
    index += 1;
    while (index < lines.length && (lines[index] ?? "").trim() && !specialLine(lines[index] ?? "")) {
      paragraph.push(lines[index] ?? "");
      index += 1;
    }
    blocks.push({ type: "paragraph", text: paragraph.join("\n") });
  }
  return blocks;
}

function safeUrl(value: string): string | null {
  try {
    const parsed = new URL(value, window.location.origin);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

function inlineMarkdown(value: string): ReactNode[] {
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^\s)]+\)|\*[^*]+\*)/g;
  const parts = value.split(pattern).filter(Boolean);
  return parts.map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={index}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={index}>{part.slice(1, -1)}</code>;
    if (part.startsWith("*") && part.endsWith("*")) return <em key={index}>{part.slice(1, -1)}</em>;
    const link = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (link) {
      const href = safeUrl(link[2] ?? "");
      return href ? <a key={index} href={href} target="_blank" rel="noreferrer">{link[1]}</a> : part;
    }
    return <Fragment key={index}>{part}</Fragment>;
  });
}

export function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="markdown-content">
      {parseMarkdownBlocks(content).map((block, index) => {
        if (block.type === "heading") {
          const Tag = `h${Math.min(block.level + 1, 6)}` as "h2" | "h3" | "h4" | "h5";
          return <Tag key={index}>{inlineMarkdown(block.text)}</Tag>;
        }
        if (block.type === "code") return <pre key={index}><code data-language={block.language}>{block.text}</code></pre>;
        if (block.type === "quote") return <blockquote key={index}>{inlineMarkdown(block.text)}</blockquote>;
        if (block.type === "list") {
          const Tag = block.ordered ? "ol" : "ul";
          return <Tag key={index}>{block.items.map((item, itemIndex) => <li key={itemIndex}>{inlineMarkdown(item)}</li>)}</Tag>;
        }
        return <p key={index}>{inlineMarkdown(block.text)}</p>;
      })}
    </div>
  );
}
