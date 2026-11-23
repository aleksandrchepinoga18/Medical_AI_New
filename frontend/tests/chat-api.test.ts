import { afterEach, expect, it, vi } from "vitest";
import { chatRequest, readChatStream } from "../lib/chat-api";
import nextConfig from "../next.config";

afterEach(() => vi.unstubAllGlobals());
it("gives proxied chat time to return its own Ollama timeout", () => {
  expect(nextConfig.experimental?.proxyTimeout).toBeGreaterThan(600_000);
});
it("reads fragmented UTF-8 stream", async () => {
  const bytes = new TextEncoder().encode('{"type":"delta","text":"ответ"}\n{"type":"done"}\n');
  const stream = new ReadableStream({ start(c) {
    for (let i = 0; i < bytes.length; i += 3) c.enqueue(bytes.slice(i, i + 3)); c.close();
  } });
  const events: string[] = [];
  await readChatStream(new Response(stream), e => events.push(e.text ?? e.type));
  expect(events).toEqual(["ответ", "done"]);
});
it("rejects incomplete streams and safe errors", async () => {
  await expect(readChatStream(new Response('{"type":"delta","text":"partial"}\n'), () => {})).rejects.toThrow("прервано");
  await expect(readChatStream(new Response('{"type":"error","code":"timeout"}\n'), () => {})).rejects.toThrow("timeout");
});
it("does not treat HTTP 200 without done as a completed response", async () => {
  const events: string[] = [];
  const response = new Response(
    '{"type":"start"}\n{"type":"delta","text":"partial"}\n{"type":"error","code":"output_limit"}\n',
    { status: 200 },
  );
  await expect(readChatStream(response, event => events.push(event.type))).rejects.toThrow("output_limit");
  expect(events).toEqual(["start", "delta"]);
});
it.each([
  { content: "synthetic" },
  { content: "synthetic", external_consent: "gigachat" },
])("sends %j with CSRF and passes cancellation signal", async (body) => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ csrf_token: "synthetic" }))
    .mockResolvedValueOnce(new Response(""));
  vi.stubGlobal("fetch", fetcher);
  const abort = new AbortController();
  await chatRequest("/synthetic/messages", "POST", body, abort.signal);
  expect(fetcher.mock.calls[1][1].headers["X-CSRF-Token"]).toBe("synthetic");
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual(body);
  expect(fetcher.mock.calls[1][1].signal).toBe(abort.signal);
});

it.each([
  {
    detail: { code: "external_consent_required" },
    explanation: "Подтвердите согласие на облачную обработку в GigaChat.",
  },
  { detail: "CSRF verification failed", explanation: "Проверка безопасности не пройдена." },
  { detail: "unexpected detail", explanation: "Доступ к чату запрещён." },
])("explains a forbidden chat request for $detail", async ({ detail, explanation }) => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(Response.json({ csrf_token: "synthetic" }))
    .mockResolvedValueOnce(Response.json({ detail }, { status: 403 }));
  vi.stubGlobal("fetch", fetcher);
  await expect(chatRequest("/synthetic/messages", "POST", { content: "synthetic" }))
    .rejects.toMatchObject({ status: 403, message: expect.stringContaining(explanation) });
});

it("explains provider configuration and streaming outages", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(Response.json({
    detail: { code: "gigachat_not_configured" },
  }, { status: 503 })));
  await expect(chatRequest("/synthetic"))
    .rejects.toMatchObject({ status: 503, message: "GigaChat не настроен на сервере." });
  const response = new Response('{"type":"error","code":"gigachat_unavailable"}\n');
  await expect(readChatStream(response, () => {})).rejects.toThrow("GigaChat недоступен");
});
