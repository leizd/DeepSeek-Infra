export function normalizeVoiceLanguage(value: string): string {
  const lang = value.trim();
  return lang || "zh-CN";
}

function mermaidSpeechText(source: string): string {
  const labels: string[] = [];
  for (const match of source.matchAll(/"([^"]+)"|\[([^\]]+)\]/g)) {
    const label = String(match[1] || match[2] || "").replace(/[[\]{}()|]/g, " ").trim();
    if (label && !/^[\w\s-]+$/.test(label)) labels.push(label);
    if (labels.length >= 8) break;
  }
  return labels.length ? ` ${labels.join("。")} ` : " ";
}

export function speechTextFromContent(content: string): string {
  return String(content || "")
    .replace(/```(?:mermaid|mmd)\s+([\s\S]*?)```/gi, (_, body: string) => mermaidSpeechText(body))
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/\$\$[\s\S]*?\$\$/g, " 公式略 ")
    .replace(/\\\[[\s\S]*?\\\]/g, " 公式略 ")
    .replace(/\\\([\s\S]*?\\\)/g, " 公式略 ")
    .replace(/\$[^$\n]{1,500}\$/g, " 公式略 ")
    .replace(/\[\^[^\]]+\]/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/\|/g, " ")
    .replace(/[#>*_~-]+/g, " ")
    .replace(/(?:公式略\s*){2,}/g, "公式略 ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 8_000);
}

export function splitLongSpeechSegment(segment: string, maxLength: number): string[] {
  const value = segment.trim();
  if (value.length <= maxLength) return value ? [value] : [];
  const words = value.split(/(\s+)/).filter(Boolean);
  if (words.length > 1) {
    const pieces: string[] = [];
    let current = "";
    for (const word of words) {
      if (!current) {
        current = word.trim();
      } else if (`${current}${word}`.length <= maxLength) {
        current += word;
      } else {
        if (current.trim()) pieces.push(current.trim());
        current = word.trim();
      }
    }
    if (current.trim()) pieces.push(current.trim());
    return pieces.flatMap((piece) => splitLongSpeechSegment(piece, maxLength));
  }
  const pieces: string[] = [];
  for (let index = 0; index < value.length; index += maxLength) {
    pieces.push(value.slice(index, index + maxLength));
  }
  return pieces;
}

export function speechChunks(text: string, maxLength = 180): string[] {
  const source = String(text || "").trim();
  if (!source) return [];
  const sentences = source.match(/[^。！？!?；;\n]+[。！？!?；;]?|\n+/g) ?? [source];
  const chunks: string[] = [];
  let current = "";
  for (const sentence of sentences) {
    const segment = sentence.trim();
    if (!segment) continue;
    for (const piece of splitLongSpeechSegment(segment, maxLength)) {
      if (!current) {
        current = piece;
      } else if (`${current} ${piece}`.length <= maxLength) {
        current = `${current} ${piece}`;
      } else {
        chunks.push(current);
        current = piece;
      }
    }
  }
  if (current) chunks.push(current);
  return chunks.slice(0, 80);
}

export interface SpeechVoiceLike {
  lang: string;
}

export function preferredSpeechVoice<T extends SpeechVoiceLike>(lang: string, voices: readonly T[]): T | null {
  if (!voices.length) return null;
  const normalized = normalizeVoiceLanguage(lang).toLowerCase();
  const base = normalized.split("-")[0];
  return (
    voices.find((voice) => voice.lang.toLowerCase() === normalized)
    ?? voices.find((voice) => voice.lang.toLowerCase().startsWith(`${base}-`))
    ?? null
  );
}

export function speechSynthesisSupported(): boolean {
  return typeof window !== "undefined" && Boolean(window.speechSynthesis && window.SpeechSynthesisUtterance);
}
