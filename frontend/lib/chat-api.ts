export type Citation = { document_id: string; filename: string; source_url: string;
  sources: { kind: string; index: number }[] };
export type ChatMessage = { id: string; role: string; content: string; status: string;
  sources: Citation[] };
export type Conversation = { id: string; title: string };
export type ChatEvent = { type: string; text?: string; code?: string; sources?: Citation[] };

export async function chatRequest(path: string, method = "GET", body?: unknown,
  signal?: AbortSignal): Promise<Response> {
  const headers: Record<string, string> = {};
  if (method !== "GET") {
    const csrf = await fetch("/api/auth/csrf", { cache: "no-store", signal });
    if (!csrf.ok) throw new Error(`HTTP ${csrf.status}`);
    headers["X-CSRF-Token"] = (await csrf.json()).csrf_token;
  }
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const result = await fetch(`/api/chat${path}`, { method, headers, signal,
    cache: "no-store", body: body === undefined ? undefined : JSON.stringify(body) });
  if (!result.ok) throw new Error(`HTTP ${result.status}`);
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
