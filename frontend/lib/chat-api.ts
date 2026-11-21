export type Citation = { document_id: string; document_number?: number; filename: string; source_url: string;
  source_id?: string;
  sources: { kind: string; index: number }[] };
export type ChatMessage = { id: string; role: string; content: string; status: string;
  sources: Citation[] };
export type Conversation = { id: string; title: string };
export type ChatHistory = { messages: ChatMessage[]; llm_provider?: unknown; llm_model?: unknown };
export type ChatEvent = { type: string; text?: string; code?: string; sources?: Citation[] };

export class ChatHttpError extends Error {
  constructor(readonly status: number, message = `HTTP ${status}`) {
    super(message);
  }
}

async function requestError(response: Response): Promise<ChatHttpError> {
  if (response.status !== 403) return new ChatHttpError(response.status);
  const data = await response.json().catch(() => null);
  if (data?.detail?.code === "external_consent_required" ||
      data?.detail === "Explicit consent for GigaChat is required") {
    return new ChatHttpError(403, "Подтвердите согласие на облачную обработку в GigaChat.");
  }
  if (data?.detail === "CSRF verification failed") {
    return new ChatHttpError(403, "Проверка безопасности не пройдена. Обновите страницу и повторите.");
  }
  return new ChatHttpError(403, "Доступ к чату запрещён.");
}

export async function chatRequest(path: string, method = "GET", body?: unknown,
  signal?: AbortSignal): Promise<Response> {
  const headers: Record<string, string> = {};
  if (method !== "GET") {
    const csrf = await fetch("/api/auth/csrf", { cache: "no-store", signal });
    if (!csrf.ok) throw await requestError(csrf);
    headers["X-CSRF-Token"] = (await csrf.json()).csrf_token;
  }
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const result = await fetch(`/api/chat${path}`, { method, headers, signal,
    cache: "no-store", body: body === undefined ? undefined : JSON.stringify(body) });
  if (!result.ok) throw await requestError(result);
  return result;
}

export async function readChatStream(response: Response, onEvent: (event: ChatEvent) => void) {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Поток недоступен");
  const decoder = new TextDecoder();
  let buffer = "", done = false;
  try {
    while (true) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value, { stream: !chunk.done });
      let newline: number;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
        if (!line.trim()) continue;
        const event: ChatEvent = JSON.parse(line);
        if (event.type === "error") throw new Error(`Генерация прервана: ${event.code}`);
        if (event.type === "done") done = true;
        onEvent(event);
      }
      if (chunk.done) break;
    }
    if (!done) throw new Error("Соединение прервано. Ответ не завершён.");
  } finally { await reader.cancel(); reader.releaseLock(); }
}
