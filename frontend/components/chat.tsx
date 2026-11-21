"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ChatHttpError, chatRequest, readChatStream, type ChatHistory, type ChatMessage, type Conversation,
} from "../lib/chat-api";

type ActiveModel = { provider: "ollama" | "gigachat"; name: string };

function activeModel(data: ChatHistory): ActiveModel | null {
  const { llm_provider: provider, llm_model: name } = data;
  if ((provider === "ollama" || provider === "gigachat") && typeof name === "string" && name.trim()) {
    return { provider, name };
  }
  return null;
}

export function PersonalChat() {
  const router = useRouter();
  const [dialogs, setDialogs] = useState<Conversation[]>([]);
  const [selected, setSelected] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [partial, setPartial] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [model, setModel] = useState<ActiveModel | null>(null);
  const [externalConsent, setExternalConsent] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const active = useRef(false);
  const report = useCallback((e: unknown, keepPrevious = false) => {
    const message = e instanceof Error ? e.message : "Ошибка чата";
    const status = e instanceof ChatHttpError ? e.status : null;
    if (status === 401) router.replace("/login");
    setError(previous => keepPrevious && previous ? previous : (
      status === 503 ? "Чат отключён или сервис недоступен." : message
    ));
  }, [router]);
  async function refresh(signal?: AbortSignal) {
    setDialogs(await (await chatRequest("", "GET", undefined, signal)).json());
  }
  async function loadHistory(id: string) {
    const data: ChatHistory = await (await chatRequest(`/${id}`)).json();
    setModel(activeModel(data));
    setMessages(data.messages);
    setPartial("");
  }
  async function open(dialog: Conversation) {
    await loadHistory(dialog.id);
    setExternalConsent(false);
    setSelected(dialog); setError("");
  }
  useEffect(() => {
    const load = new AbortController();
    async function loadDialogs() {
      try {
        const response = await chatRequest("", "GET", undefined, load.signal);
        const data: Conversation[] = await response.json();
        if (!load.signal.aborted) setDialogs(data);
      } catch (e) {
        if (!load.signal.aborted) report(e);
      }
    }
    void loadDialogs();
    return () => load.abort();
  }, [report]);
  useEffect(() => () => controller.current?.abort(), []);
  async function send() {
    if (!selected || !model || !input.trim() || active.current ||
        (model.provider === "gigachat" && !externalConsent)) {
      return;
    }
    const body = model.provider === "gigachat"
      ? { content: input, external_consent: "gigachat" } : { content: input };
    setExternalConsent(false);
    active.current = true; setBusy(true); setError(""); setPartial("");
    const abort = new AbortController(); controller.current = abort;
    try {
      const response = await chatRequest(`/${selected.id}/messages`, "POST", body, abort.signal);
      await readChatStream(response, event => {
        if (event.type === "delta") setPartial(old => old + (event.text ?? ""));
      });
      setInput("");
    } catch (e) {
      if (abort.signal.aborted) setError("Генерация остановлена. Неполный ответ не сохранён.");
      else report(e);
    } finally {
      try {
        await loadHistory(selected.id);
      } catch (e) {
        setModel(null);
        report(e, true);
      }
      setBusy(false); active.current = false; controller.current = null;
    }
  }
  return <main className="grid gap-6 md:grid-cols-[240px_1fr]">
    <aside className="space-y-3"><h1>Персональный чат</h1>
      <button disabled={busy} onClick={async () => {
        try { const d = await (await chatRequest("", "POST")).json();
          await refresh(); await open(d); } catch (e) { report(e); }
      }}>Новый диалог</button>
      {dialogs.map(d => <button className="block w-full rounded border p-2 text-left"
        disabled={busy} key={d.id} aria-pressed={selected?.id === d.id}
        onClick={() => open(d).catch(report)}>{d.title}</button>)}
    </aside>
    <section className="space-y-4">
      {error && <p role="alert">{error} Текст можно отправить повторно.</p>}
      {selected && <>
        <div className="flex gap-4"><h2>{selected.title}</h2>
          <button disabled={busy} onClick={async () => {
            const title = window.prompt("Название диалога", selected.title);
            if (!title?.trim()) return;
            try { await chatRequest(`/${selected.id}`, "PATCH", { title });
              setSelected({ ...selected, title }); await refresh(); } catch (e) { report(e); }
          }}>Переименовать</button>
          <button disabled={busy} onClick={async () => {
            if (!window.confirm("Удалить этот диалог и его сообщения?")) return;
            try { await chatRequest(`/${selected.id}`, "DELETE");
              setSelected(null); setMessages([]); await refresh(); } catch (e) { report(e); }
          }}>Удалить</button></div>
        {model
          ? <p>Активная модель — {model.provider === "ollama" ? `Ollama / ${model.name}` : model.name}</p>
          : <p role="status">Не удалось определить активную модель. Откройте диалог заново.</p>}
        <p>Модель может ошибаться. Сверяйте выводы с указанными источниками.</p>
        <div aria-live="polite" className="space-y-4">
          {messages.map(m => <article className="rounded border p-4" key={m.id}>
            <strong>{m.role === "user" ? "Вы" : "Помощник"}</strong>
            <p className="whitespace-pre-wrap">{m.content}</p>
            {m.status !== "complete" && <p>Попытка: {m.status}</p>}
            {m.sources.map((s, i) => <div key={i}>
              <a className="underline" href={s.source_url}>{s.source_id && `[${s.source_id}] `}{s.document_number !== undefined && `№ ${s.document_number} · `}{s.filename}</a>
              {s.sources.map((p, j) => <span key={j}> · {p.kind} {p.index}</span>)}
            </div>)}
          </article>)}
          {busy && <article><p role="status">Генерация…</p><p className="whitespace-pre-wrap">{partial}</p></article>}
        </div>
        <form onSubmit={e => { e.preventDefault(); void send(); }} className="space-y-2">
          <label className="block" htmlFor="chat-input">Сообщение</label>
          <textarea className="w-full rounded border p-3" id="chat-input" maxLength={2000}
            value={input} disabled={busy} onChange={e => setInput(e.target.value)} />
          {model?.provider === "gigachat" && <label className="flex items-start gap-2">
            <input type="checkbox" checked={externalConsent} disabled={busy}
              onChange={e => setExternalConsent(e.target.checked)} />
            <span>Разрешаю отправить в облачный GigaChat это сообщение, относящиеся к нему
              реплики этого диалога и данные выбранных инструментами документов.</span>
          </label>}
          <button disabled={!model || busy || !input.trim() ||
            (model.provider === "gigachat" && !externalConsent)}>
            Отправить
          </button>
          {busy && <button type="button" onClick={() => controller.current?.abort()}>Остановить</button>}
        </form>
      </>}
    </section>
  </main>;
}
