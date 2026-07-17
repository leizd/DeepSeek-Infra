import { httpClient, type HttpClient } from "./httpClient";

interface TitleResponse {
  title?: string;
}

export interface GenerateTitleInput {
  apiKey: string;
  userMessage: string;
  assistantMessage: string;
  titleModel?: string;
}

export async function generateConversationTitle(
  input: GenerateTitleInput,
  client: HttpClient = httpClient,
): Promise<string> {
  const response = await client.postJson<TitleResponse>("/api/title", {
    ...(input.apiKey.trim() ? { apiKey: input.apiKey.trim() } : {}),
    titleModel: input.titleModel ?? "deepseek-v4-flash",
    userMessage: input.userMessage.trim(),
    assistantMessage: input.assistantMessage.slice(0, 600),
  });
  return String(response.title ?? "").replace(/\s+/g, " ").trim().slice(0, 32);
}
