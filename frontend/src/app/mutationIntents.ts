function normalizeIntentValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(normalizeIntentValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, entry]) => [key, normalizeIntentValue(entry)]),
    );
  }
  return value;
}

export function stableIntentKey(value: unknown): string {
  return JSON.stringify(normalizeIntentValue(value));
}

export function skillDraftIntent(draft: {
  name: string;
  description: string;
  systemPrompt: string;
}): string {
  return stableIntentKey({
    name: draft.name.trim(),
    description: draft.description.trim(),
    systemPrompt: draft.systemPrompt,
  });
}
